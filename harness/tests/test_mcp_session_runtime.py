import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import _session_mcp_definitions_for_tools
from tools import mcp_client
from tools.context import ToolContext
from tools.delegation import _run_child


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

    def _named_state(self, server_name: str, tool_name: str, marker: str):
        state = mcp_client.MCPServerState(
            server_name,
            {"command": "python", "args": ["server.py"], "marker": marker},
        )
        state.connected = True
        state.tools = [{
            "name": tool_name,
            "description": marker,
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
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

    async def test_child_mcp_allowlist_and_schema_include_only_exact_declared_tool(self):
        declared_state = self._named_state("evidence", "lookup", "declared")
        hidden_state = self._named_state("admin", "mutate", "hidden")
        mcp_client._mcp_states[("user", "session")] = {
            "evidence": declared_state,
            "admin": hidden_state,
        }
        declared_name = "mcp_evidence_lookup"
        hidden_name = "mcp_admin_mutate"

        definitions = _session_mcp_definitions_for_tools(
            "user",
            "session",
            [declared_name],
            allow_all=False,
        )
        self.assertEqual(
            [item["function"]["name"] for item in definitions],
            [declared_name],
        )
        self.assertEqual(
            definitions[0]["function"]["parameters"],
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )

        observed: dict[str, object] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            observed["tools"] = list(tools)
            observed["allow_session_mcp"] = kwargs.get("allow_session_mcp")
            yield {"type": "delta", "content": "retrieved evidence " * 30}
            yield {"type": "done", "finish_reason": "stop"}

        context = ToolContext(
            user_id="user",
            session_id="session",
            model_id="model",
            provider_config={"base_url": "http://example", "api_model": "model"},
            enabled_tools=(declared_name, hidden_name),
            run_id="parent",
            root_run_id="root",
        )
        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_mcp.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "query the declared evidence capability",
                    "step_type": "knowledge_bootstrap",
                    "tools": [declared_name],
                },
                context,
                0,
                parallel_child=True,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed["tools"], [declared_name])
        self.assertFalse(observed["allow_session_mcp"])
        self.assertNotIn(hidden_name, observed["tools"])

    async def test_child_cannot_receive_mcp_tool_from_another_session(self):
        state_a = self._named_state("evidence", "lookup", "session-a")
        mcp_client._mcp_states[("user", "a")] = {"evidence": state_a}
        tool_name = "mcp_evidence_lookup"

        context = ToolContext(
            user_id="user",
            session_id="b",
            model_id="model",
            provider_config={"base_url": "http://example", "api_model": "model"},
            enabled_tools=(tool_name,),
            run_id="parent",
            root_run_id="root",
        )
        with (
            patch("agent_loop.run_stream") as run_stream,
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_mcp.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "attempt a cross-session capability",
                    "step_type": "knowledge_bootstrap",
                    "tools": [tool_name],
                },
                context,
                0,
                parallel_child=True,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rejected_tools"], [tool_name])
        self.assertIn("exact session", result["error"])
        run_stream.assert_not_called()
        self.assertEqual(
            _session_mcp_definitions_for_tools(
                "user", "b", [tool_name], allow_all=False,
            ),
            [],
        )

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
                self.assertEqual(
                    0o600,
                    (
                        Path(temp_dir)
                        / "user"
                        / "session"
                        / mcp_client.MCP_CONFIG_FILE
                    ).stat().st_mode & 0o777,
                )
            finally:
                mcp_client.MCP_CONFIG_BASE = old_base

    def test_config_scope_rejects_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "user scope"):
            mcp_client._session_config_path("../other-user", "session")
        with self.assertRaisesRegex(ValueError, "session scope"):
            mcp_client._session_config_path("user", "../../other-session")

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
