import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tools import mcp_client


class MCPSessionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mcp_client._mcp_states.clear()
        mcp_client._mcp_connect_locks.clear()

    def tearDown(self):
        mcp_client._mcp_states.clear()
        mcp_client._mcp_connect_locks.clear()

    def _state(self, marker: str):
        state = mcp_client.MCPServerState(
            "same-name",
            {"command": "python", "args": ["server.py"], "marker": marker},
        )
        state.connected = True
        state.tools = [{
            "name": "echo",
            "description": marker,
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        }]
        return state

    async def test_catalog_and_dispatch_are_session_local(self):
        state_a = self._state("session-a")
        state_b = self._state("session-b")
        mcp_client._mcp_states[("user", "a")] = {"same-name": state_a}
        mcp_client._mcp_states[("user", "b")] = {"same-name": state_b}

        defs_a = mcp_client.get_session_tool_definitions("user", "a")
        defs_b = mcp_client.get_session_tool_definitions("user", "b")
        self.assertEqual(defs_a[0]["function"]["description"], "[MCP:same-name] session-a")
        self.assertEqual(defs_b[0]["function"]["description"], "[MCP:same-name] session-b")

        async def fake_call(state, tool_name, params):
            return json.dumps({
                "marker": state.config["marker"],
                "tool": tool_name,
                "params": params,
            })

        with patch.object(
            mcp_client, "connect_all_for_user", AsyncMock(return_value={"same-name": True})
        ), patch.object(mcp_client, "_call_mcp_state_tool", side_effect=fake_call):
            result = json.loads(await mcp_client.dispatch_mcp_tool(
                "mcp_same-name_echo", {"value": "x"}, "user", "b"
            ))
        self.assertEqual(result["marker"], "session-b")

    def test_config_layers_do_not_copy_inherited_servers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_base = mcp_client.MCP_CONFIG_BASE
            mcp_client.MCP_CONFIG_BASE = Path(temp_dir)
            try:
                mcp_client._save_config(
                    "user", {"servers": {"shared": {"url": "https://shared"}}}, "default"
                )
                mcp_client._save_config(
                    "user", {"servers": {"local": {"url": "https://local"}}}, "session"
                )
                self.assertEqual(
                    set(mcp_client._load_scope_config("user", "session")["servers"]),
                    {"local"},
                )
                self.assertEqual(
                    set(mcp_client._load_config("user", "session")["servers"]),
                    {"shared", "local"},
                )
            finally:
                mcp_client.MCP_CONFIG_BASE = old_base

    async def test_persistent_connect_failure_signals_ready(self):
        state = mcp_client.MCPServerState(
            "broken", {"command": "python", "transport": "stdio"}
        )
        with patch.object(mcp_client, "_MCP_STDIO_AVAILABLE", False):
            connected = await mcp_client._connect(state)
        self.assertFalse(connected)
        self.assertTrue(state._ready_event.is_set())


if __name__ == "__main__":
    unittest.main()
