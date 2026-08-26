import asyncio
import unittest
from unittest.mock import patch


class _FakeExitStack:
    def __init__(self, opened_by):
        self.opened_by = opened_by
        self.closed_by = None

    async def aclose(self):
        self.closed_by = asyncio.current_task()


class TestMcpLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_connections_open_and_close_on_one_owner_task(self):
        from core import mcp_client

        manager = object.__new__(mcp_client.MCPManager)
        manager._initialized = False
        manager.__init__()
        connect_tasks = []
        stacks = []

        async def fake_connect(name, cfg):
            task = asyncio.current_task()
            connect_tasks.append(task)
            stack = _FakeExitStack(task)
            stacks.append(stack)
            manager.sessions[name] = object()
            manager.exit_stacks[name] = stack

        async def fake_refresh_tools():
            return []

        manager._load_config = lambda: {
            "mcpServers": {"demo": {"command": "demo"}}
        }
        manager._connect_server = fake_connect
        manager.refresh_tools = fake_refresh_tools

        with patch.object(mcp_client, "MCP_AVAILABLE", True):
            caller_task = asyncio.current_task()
            await manager.initialize()
            await manager.shutdown()

        self.assertEqual(len(connect_tasks), 1)
        self.assertIsNot(connect_tasks[0], caller_task)
        self.assertEqual(len(stacks), 1)
        self.assertIs(stacks[0].opened_by, connect_tasks[0])
        self.assertIs(stacks[0].closed_by, connect_tasks[0])
        self.assertFalse(manager.exit_stacks)
        self.assertFalse(manager.sessions)

    async def test_cancelled_owner_closes_stacks_before_loop_ends(self):
        from core import mcp_client

        manager = object.__new__(mcp_client.MCPManager)
        manager._initialized = False
        manager.__init__()
        entered = asyncio.Event()
        stack_holder = []

        async def fake_connect(name, cfg):
            stack = _FakeExitStack(asyncio.current_task())
            stack_holder.append(stack)
            manager.sessions[name] = object()
            manager.exit_stacks[name] = stack
            entered.set()

        async def fake_refresh_tools():
            return []

        manager._load_config = lambda: {
            "mcpServers": {"demo": {"command": "demo"}}
        }
        manager._connect_server = fake_connect
        manager.refresh_tools = fake_refresh_tools

        with patch.object(mcp_client, "MCP_AVAILABLE", True):
            initialize_task = asyncio.create_task(manager.initialize())
            await entered.wait()
            # initialize is still waiting for the owner to finish the queued
            # operation; cancelling it must not strand the transport stack.
            initialize_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await initialize_task

            owner = manager._lifecycle_task
            self.assertIsNotNone(owner)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner

        self.assertIsNotNone(stack_holder[0].closed_by)
        self.assertFalse(manager.exit_stacks)
        self.assertFalse(manager.sessions)


if __name__ == "__main__":
    unittest.main()
