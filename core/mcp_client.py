import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    logger.warning("MCP library not found. MCP features will be disabled.")
    MCP_AVAILABLE = False

CONFIG_PATH = Path("mcp/mcp_config.json")
_BACKOFF_BASE_S = 5.0
_BACKOFF_MAX_S = 300.0
_ONLINE_TTL_S = 120.0
_ENV_REF = re.compile(r"\$\{([^}]+)\}")


def _expand_env_refs(value: str, env: Dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return env.get(key, os.environ.get(key, ""))

    return _ENV_REF.sub(repl, value)


def build_server_env(cfg_env: Optional[Dict[str, str]], base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Merge MCP config env onto the process environment.

    Empty strings in mcp_config.json do not override values loaded from .env.
    ${VAR} placeholders are expanded after merge.
    """
    merged = dict(base_env or os.environ)
    for key, value in (cfg_env or {}).items():
        if not isinstance(value, str):
            continue
        if value.strip() == "":
            continue
        merged[key] = value

    for key, value in list(merged.items()):
        if isinstance(value, str) and "${" in value:
            merged[key] = _expand_env_refs(value, merged)
    return merged


def expand_server_args(args: List[str], env: Dict[str, str]) -> List[str]:
    expanded: List[str] = []
    for arg in args:
        if isinstance(arg, str) and "${" in arg:
            expanded.append(_expand_env_refs(arg, env))
        else:
            expanded.append(arg)
    return expanded


def validate_mcp_config(data: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "Config must be a JSON object."
    servers = data.get("mcpServers")
    if servers is None:
        return False, "Missing 'mcpServers' field."
    if not isinstance(servers, dict):
        return False, "'mcpServers' must be a JSON object."

    for name, cfg in servers.items():
        if not isinstance(name, str) or not name.strip():
            return False, "Server names must be non-empty strings."
        if not isinstance(cfg, dict):
            return False, f"Config for '{name}' must be an object."
        command = cfg.get("command")
        url = cfg.get("url")
        transport = str(cfg.get("type") or "").strip().lower()
        is_http = transport in {"http", "sse"} or (
            isinstance(url, str) and bool(url.strip())
        )
        if is_http:
            if not isinstance(url, str) or not url.strip():
                return False, f"'{name}.url' must be a non-empty string for HTTP MCP."
        elif not isinstance(command, str) or not command.strip():
            return False, f"'{name}.command' must be a non-empty string."
        args = cfg.get("args", [])
        if args is None:
            args = []
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            return False, f"'{name}.args' must be an array of strings."
        if len(args) > 64:
            return False, f"'{name}.args' is too long (max 64)."
        env = cfg.get("env", {})
        if env is None:
            env = {}
        if not isinstance(env, dict) or any(
            (not isinstance(k, str) or not isinstance(v, str)) for k, v in env.items()
        ):
            return False, f"'{name}.env' must be an object of string->string."
        if len(env) > 64:
            return False, f"'{name}.env' is too large (max 64)."

    return True, ""

class MCPManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MCPManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.sessions: Dict[str, ClientSession] = {}
        self.exit_stacks: Dict[str, Any] = {} # Store exit stacks per server
        self._tool_cache: List[Dict[str, Any]] = []
        self._status: Dict[str, Dict[str, Any]] = {}
        self._backoff: Dict[str, Dict[str, Any]] = {}
        self._initialized = True
        # MCP's stdio transport owns an AnyIO task group.  AsyncExitStack must
        # be entered and closed by the same asyncio task; using a normal lock
        # around initialize/shutdown is not enough because the caller task can
        # change between those operations.  Keep all connection lifecycle work
        # on one long-lived owner task instead.
        self._lock = asyncio.Lock()
        self._lifecycle_queue: Optional[asyncio.Queue] = None
        self._lifecycle_task: Optional[asyncio.Task] = None
        self._lifecycle_loop = None

    async def initialize(self):
        if not MCP_AVAILABLE:
            return

        await self._submit_lifecycle("initialize")

    async def _submit_lifecycle(self, operation: str):
        """Run a lifecycle operation on the task that owns MCP transports."""

        loop = asyncio.get_running_loop()
        lifecycle_task = self._lifecycle_task
        if (
            lifecycle_task is not None
            and not lifecycle_task.done()
            and self._lifecycle_loop is not None
            and self._lifecycle_loop is not loop
        ):
            raise RuntimeError(
                "MCP lifecycle is already owned by a different event loop."
            )

        if lifecycle_task is None or lifecycle_task.done():
            self._lifecycle_loop = loop
            self._lifecycle_queue = asyncio.Queue()
            self._lifecycle_task = loop.create_task(
                self._lifecycle_worker(),
                name="limebot-mcp-lifecycle",
            )

        queue = self._lifecycle_queue
        if queue is None:  # pragma: no cover - defensive invariant guard
            raise RuntimeError("MCP lifecycle queue was not initialized.")
        result = loop.create_future()
        await queue.put((operation, result))
        return await result

    async def _lifecycle_worker(self):
        """Serialize MCP connect/reconnect/close operations in one task."""

        queue = self._lifecycle_queue
        if queue is None:  # pragma: no cover - defensive invariant guard
            return

        try:
            while True:
                operation, result = await queue.get()
                try:
                    if operation == "initialize":
                        value = await self._initialize_impl()
                    elif operation == "shutdown":
                        value = await self._shutdown_impl()
                    else:  # pragma: no cover - private callers only use known ops
                        raise ValueError(f"Unknown MCP lifecycle operation: {operation}")
                except asyncio.CancelledError:
                    if not result.done():
                        result.cancel()
                    raise
                except Exception as exc:
                    if not result.done():
                        result.set_exception(exc)
                    logger.error(
                        "MCP lifecycle operation '{}' failed: {}",
                        operation,
                        exc,
                    )
                else:
                    if not result.done():
                        result.set_result(value)

                # A shutdown closes every stack owned by this task.  Ending
                # the worker after that operation prevents a later caller
                # from trying to reuse a queue whose task has already ended.
                if operation == "shutdown":
                    return
        finally:
            # asyncio.run() and application shutdown cancel pending workers.
            # Close the stacks before the owner task exits so a later event
            # loop never attempts to close an AnyIO cancel scope from the wrong
            # task.
            try:
                await self._shutdown_impl()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error closing MCP connections during worker shutdown")

    async def _initialize_impl(self):
        """Connect configured servers; called only by the lifecycle owner."""

        await self._shutdown_impl()
        config = self._load_config()
        servers = config.get("mcpServers", {})

        for name, cfg in servers.items():
            try:
                if self._is_in_backoff(name):
                    logger.warning(f"Skipping MCP server '{name}' (backoff active).")
                    continue
                await self._connect_server(name, cfg)
            except Exception as e:
                self._mark_error(name, str(e))
                logger.error(f"Failed to connect to MCP server '{name}': {e}")

        await self.refresh_tools()

    def _load_config(self) -> Dict[str, Any]:
        if not CONFIG_PATH.exists():
            return {"mcpServers": {}}
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error loading MCP config: {e}")
            return {"mcpServers": {}}

    async def _connect_server(self, name: str, cfg: Dict[str, Any]):
        command = cfg.get("command")
        url = str(cfg.get("url") or "").strip()
        transport = str(cfg.get("type") or "").strip().lower()
        if transport in {"http", "sse"} or (url and not command):
            self._mark_error(
                name,
                "HTTP/SSE MCP transports are recorded but not connected by the stdio client.",
            )
            logger.info(
                "Recorded HTTP MCP server '%s' at %s without opening a stdio session.",
                name,
                url or "(missing url)",
            )
            return

        args = cfg.get("args", [])
        env = build_server_env(cfg.get("env", {}))
        args = expand_server_args(args, env)

        if not command:
            logger.warning(f"No command specified for MCP server '{name}'")
            return

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env
        )

        logger.info(f"Connecting to MCP server '{name}' via stdio...")
        
        # We need to maintain the context manager for the duration of the session
        
        from contextlib import AsyncExitStack
        exit_stack = AsyncExitStack()

        try:
            read, write = await exit_stack.enter_async_context(stdio_client(server_params))
            session = await exit_stack.enter_async_context(ClientSession(read, write))
            
            await asyncio.wait_for(session.initialize(), timeout=30.0)
            self.sessions[name] = session
            self.exit_stacks[name] = exit_stack
            self._mark_ok(name)
            logger.info(f"✅ Connected to MCP server '{name}'")
        except asyncio.TimeoutError:
            await exit_stack.aclose()
            self._mark_error(name, "Timeout (30s)")
            logger.error(f"Timeout starting MCP server '{name}' (30s)")
        except Exception as e:
            await exit_stack.aclose()
            self._mark_error(name, str(e))
            logger.error(f"Error starting MCP server '{name}': {e}")

    async def refresh_tools(self) -> List[Dict[str, Any]]:
        """Fetch tools from all active MCP servers and translate to OpenAI format."""
        all_tools = []
        if not MCP_AVAILABLE:
            return []

        for server_name, session in self.sessions.items():
            try:
                response = await session.list_tools()
                for tool in response.tools:
                    # Prefix tool name to avoid collisions and route easily
                    openai_tool = {
                        "type": "function",
                        "function": {
                            "name": f"mcp_{server_name}_{tool.name}",
                            "description": tool.description or f"MCP tool from {server_name}",
                            "parameters": tool.inputSchema
                        }
                    }
                    all_tools.append(openai_tool)
                self._mark_ok(server_name)
            except Exception as e:
                self._mark_error(server_name, str(e))
                logger.error(f"Error listing tools for MCP server '{server_name}': {e}")

        self._tool_cache = all_tools
        return all_tools

    def get_tools(self) -> List[Dict[str, Any]]:
        return self._tool_cache

    async def execute_tool(self, full_name: str, arguments: Dict[str, Any]) -> str:
        """Execute an MCP tool by routing to the correct server."""
        if not full_name.startswith("mcp_"):
            return f"Error: '{full_name}' is not an MCP tool."

        # Parse mcp_{server}_{tool}
        parts = full_name.split("_", 2)
        if len(parts) < 3:
            return f"Error: Invalid MCP tool name format '{full_name}'."

        server_name = parts[1]
        tool_name = parts[2]

        session = self.sessions.get(server_name)
        if not session:
            return f"Error: MCP server '{server_name}' is not connected."

        try:
            logger.info(f"Executing MCP tool: {server_name}.{tool_name}")
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=60.0
            )
            self._mark_ok(server_name)
            
            # format the result content
            output = []
            for item in result.content:
                if hasattr(item, "text"):
                    output.append(item.text)
                elif hasattr(item, "data"):
                    output.append(str(item.data))
            
            return "\n".join(output) if output else "Success (no output)"
        except Exception as e:
            self._mark_error(server_name, str(e))
            logger.error(f"Error executing MCP tool '{full_name}': {e}")
            return f"Error executing MCP tool: {str(e)}"

    async def shutdown(self):
        """Shutdown all active MCP server connections."""
        if not MCP_AVAILABLE:
            return
        await self._submit_lifecycle("shutdown")

    async def _shutdown_impl(self):
        """Close stacks; called only by the lifecycle owner task."""
        for name, stack in list(self.exit_stacks.items()):
            try:
                await stack.aclose()
            except Exception as e:
                logger.error(f"Error closing MCP server '{name}': {e}")
        
        self.exit_stacks.clear()
        self.sessions.clear()
        self._tool_cache.clear()
        logger.info("All MCP connections closed.")

    def get_status(self) -> Dict[str, str]:
        now = time.monotonic()
        status = {}
        for name, info in self._status.items():
            online = info.get("online", False)
            last_ok = info.get("last_ok", 0.0)
            if online and (now - last_ok) <= _ONLINE_TTL_S:
                status[name] = "Online"
            elif info.get("last_error"):
                status[name] = "Error"
            else:
                status[name] = "Offline"
        for name in self.sessions:
            status.setdefault(name, "Online")
        return status

    def _mark_ok(self, name: str):
        self._status.setdefault(name, {})
        self._status[name]["online"] = True
        self._status[name]["last_ok"] = time.monotonic()
        self._status[name]["last_error"] = ""
        if name in self._backoff:
            self._backoff.pop(name, None)

    def _mark_error(self, name: str, err: str):
        self._status.setdefault(name, {})
        self._status[name]["online"] = False
        self._status[name]["last_error"] = err
        entry = self._backoff.get(name, {"attempts": 0, "next_retry": 0.0})
        entry["attempts"] += 1
        delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2 ** (entry["attempts"] - 1)))
        entry["next_retry"] = time.monotonic() + delay
        self._backoff[name] = entry

    def _is_in_backoff(self, name: str) -> bool:
        entry = self._backoff.get(name)
        if not entry:
            return False
        return time.monotonic() < entry.get("next_retry", 0.0)

_manager = None

def get_mcp_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager
