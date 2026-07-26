from contextvars import ContextVar
from typing import Any, Dict


tool_context: ContextVar[Dict[str, Any]] = ContextVar("tool_context", default={})
workspace_context: ContextVar[Dict[str, Any]] = ContextVar(
    "limebot_workspace_context", default={}
)
