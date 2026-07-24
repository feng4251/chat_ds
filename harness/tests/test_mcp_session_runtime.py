import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import _session_mcp_definitions_for_tools
from tools import mcp_client, mcp_contract
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

    def test_complex_input_schema_is_preserved_and_content_addressed(self):
        state = self._state("complex")
        schema = {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "pattern": "^[A-Z]{2}[0-9]{2}$",
                    "minLength": 4,
                    "maxLength": 4,
                },
                "choice": {
                    "oneOf": [
                        {"type": "integer", "minimum": 1, "maximum": 3},
                        {"type": "string", "enum": ["auto"]},
                    ],
                },
                "payload": {
                    "anyOf": [
                        {"type": "null"},
                        {
                            "type": "array",
                            "items": {"type": "boolean"},
                            "minItems": 1,
                            "maxItems": 2,
                        },
                    ],
                },
            },
            "required": ["code", "choice"],
            "additionalProperties": False,
            "minProperties": 2,
            "maxProperties": 3,
            "description": "All validation keywords remain in the contract.",
        }
        state.tools[0]["inputSchema"] = schema
        mcp_client._mcp_states[("user", "session")] = {"same-name": state}

        catalog = mcp_client.freeze_session_mcp_catalog("user", "session")
        definitions = mcp_client.get_session_tool_definitions("user", "session")
        descriptor = catalog.get("mcp_same-name_echo")

        self.assertEqual(definitions[0]["function"]["parameters"], schema)
        self.assertIsNotNone(descriptor)
        self.assertEqual(descriptor.input_schema, schema)
        self.assertEqual(len(descriptor.schema_sha256), 64)
        self.assertEqual(len(catalog.catalog_revision), 64)
        self.assertEqual(descriptor.catalog_revision, catalog.catalog_revision)

        reordered = {
            "description": schema["description"],
            "maxProperties": 3,
            "minProperties": 2,
            "additionalProperties": False,
            "required": ["code", "choice"],
            "properties": schema["properties"],
            "type": "object",
        }
        state.tools[0]["inputSchema"] = reordered
        same_contract = mcp_client.freeze_session_mcp_catalog("user", "session")
        self.assertEqual(
            catalog.catalog_revision,
            same_contract.catalog_revision,
        )

    def test_mcp_schema_preflight_enforces_complete_contract(self):
        state = self._state("bounded")
        state.tools[0]["inputSchema"] = {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "pattern": "^[A-Z]{2}$",
                    "minLength": 2,
                    "maxLength": 2,
                },
                "score": {
                    "oneOf": [
                        {"type": "integer", "minimum": 1, "maximum": 3},
                        {"type": "string", "const": "unknown"},
                    ],
                },
            },
            "required": ["code", "score"],
            "additionalProperties": False,
        }
        mcp_client._mcp_states[("user", "session")] = {"same-name": state}
        public_name = "mcp_same-name_echo"

        accepted = mcp_client.preflight_session_mcp_tool_call(
            public_name,
            {"code": "AD", "score": 2},
            "user",
            "session",
        )
        unexpected = mcp_client.preflight_session_mcp_tool_call(
            public_name,
            {"code": "AD", "score": 2, "hidden": True},
            "user",
            "session",
        )
        bad_pattern = mcp_client.preflight_session_mcp_tool_call(
            public_name,
            {"code": "ad", "score": 2},
            "user",
            "session",
        )
        bad_limit = mcp_client.preflight_session_mcp_tool_call(
            public_name,
            {"code": "AD", "score": 9},
            "user",
            "session",
        )

        self.assertTrue(accepted.ok)
        for rejected in (unexpected, bad_pattern, bad_limit):
            self.assertFalse(rejected.ok)
            self.assertEqual(
                rejected.reason,
                "mcp_tool_schema_validation_failed",
            )

    def test_mcp_schema_and_argument_payloads_are_bounded(self):
        state = self._state("bounded")
        mcp_client._mcp_states[("user", "session")] = {"same-name": state}
        descriptor = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
        ).get("mcp_same-name_echo")
        self.assertIsNotNone(descriptor)

        with patch.object(mcp_contract, "MCP_MAX_ARGUMENT_BYTES", 64):
            args_result = mcp_contract.preflight_mcp_tool_call(
                descriptor,
                {"value": "x" * 200},
            )
        self.assertFalse(args_result.ok)
        self.assertEqual(
            args_result.reason,
            "mcp_contract_limit_exceeded",
        )

        with patch.object(mcp_contract, "MCP_MAX_SCHEMA_BYTES", 32):
            bounded_catalog = mcp_client.freeze_session_mcp_catalog(
                "user",
                "session",
            )
        self.assertEqual(bounded_catalog.descriptors, ())
        self.assertEqual(len(bounded_catalog.rejected_tools), 1)
        self.assertIn("byte limit", bounded_catalog.rejected_tools[0].reason)

    async def test_dispatch_rejects_invalid_args_before_transport(self):
        state = self._named_state("evidence", "lookup", "declared")
        state.tools[0]["inputSchema"]["additionalProperties"] = False
        mcp_client._mcp_states[("user", "session")] = {"evidence": state}

        with (
            patch.object(
                mcp_client,
                "connect_all_for_user",
                AsyncMock(return_value={"evidence": True}),
            ),
            patch.object(
                mcp_client,
                "_call_mcp_state_tool",
                AsyncMock(return_value="unexpected"),
            ) as call,
        ):
            result = json.loads(await mcp_client.dispatch_mcp_tool(
                "mcp_evidence_lookup",
                {"query": "term", "unexpected": "not allowed"},
                "user",
                "session",
            ))

        self.assertEqual(result["reason"], "mcp_tool_schema_validation_failed")
        call.assert_not_awaited()

    async def test_default_non_idempotent_policy_suppresses_transport_replay(self):
        state = self._named_state("evidence", "lookup", "declared")
        mcp_client._mcp_states[("user", "session")] = {"evidence": state}

        with (
            patch.object(
                mcp_client,
                "connect_all_for_user",
                AsyncMock(return_value={"evidence": True}),
            ),
            patch.object(
                mcp_client,
                "_call_mcp_state_tool",
                AsyncMock(side_effect=RuntimeError("connection lost")),
            ) as call,
            patch.object(
                mcp_client,
                "disconnect_server",
                AsyncMock(),
            ) as disconnect,
        ):
            result = json.loads(await mcp_client.dispatch_mcp_tool(
                "mcp_evidence_lookup",
                {"query": "term"},
                "user",
                "session",
            ))

        self.assertEqual(
            result["reason"],
            "mcp_non_idempotent_retry_suppressed",
        )
        self.assertTrue(result["retry_suppressed"])
        self.assertEqual(call.await_count, 1)
        disconnect.assert_awaited_once()

    async def test_frozen_descriptor_blocks_schema_drift_before_transport(self):
        state = self._named_state("evidence", "lookup", "declared")
        mcp_client._mcp_states[("user", "session")] = {"evidence": state}
        frozen = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
        ).get("mcp_evidence_lookup")
        self.assertIsNotNone(frozen)

        state.tools[0]["inputSchema"]["properties"]["query"]["maxLength"] = 8
        drift = mcp_client.check_session_mcp_tool_schema_drift(
            frozen,
            "user",
            "session",
        )
        self.assertFalse(drift.ok)
        self.assertEqual(drift.reason, "mcp_capability_changed")
        self.assertIn("schema_sha256", drift.changed_fields)

        with (
            patch.object(
                mcp_client,
                "connect_all_for_user",
                AsyncMock(return_value={"evidence": True}),
            ),
            patch.object(
                mcp_client,
                "_call_mcp_state_tool",
                AsyncMock(return_value="unexpected"),
            ) as call,
        ):
            result = json.loads(await mcp_client.dispatch_mcp_tool(
                "mcp_evidence_lookup",
                {"query": "term"},
                "user",
                "session",
                expected_descriptor=frozen,
            ))

        self.assertEqual(result["reason"], "mcp_capability_changed")
        call.assert_not_awaited()

    async def test_parent_frozen_schema_cannot_expand_in_delegated_child(self):
        state = self._named_state("evidence", "lookup", "declared")
        state.tools[0]["inputSchema"]["additionalProperties"] = False
        mcp_client._mcp_states[("user", "session")] = {"evidence": state}
        parent_catalog = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
        )
        tool_name = "mcp_evidence_lookup"

        # Simulate tools/list_changed after the parent model received its
        # surface: the same public capability now accepts a new privileged
        # argument. A child must not rediscover and adopt that wider schema.
        state.tools[0]["inputSchema"] = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "include_private": {"type": "boolean"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
        context = ToolContext(
            user_id="user",
            session_id="session",
            model_id="model",
            provider_config={"base_url": "http://example", "api_model": "model"},
            enabled_tools=(tool_name,),
            frozen_mcp_catalog=parent_catalog,
            run_id="parent",
            root_run_id="root",
        )

        with patch("agent_loop.run_stream") as run_stream:
            result = await _run_child(
                {
                    "goal": "query evidence under the parent contract",
                    "step_type": "knowledge_bootstrap",
                    "tools": [tool_name],
                },
                context,
                0,
                parallel_child=False,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rejected_tools"], [tool_name])
        self.assertIn("frozen MCP contract", result["error"])
        self.assertIn("drifted before child execution", result["error"])
        self.assertIn("schema_sha256", result["error"])
        self.assertEqual(
            result["terminal_reason"],
            "mcp_capability_contract_violation",
        )
        self.assertIn(tool_name, result["mcp_contract_rejections"])
        run_stream.assert_not_called()

    async def test_child_runtime_scope_excludes_post_parent_new_mcp_tool(self):
        state = self._named_state("evidence", "lookup", "declared")
        mcp_client._mcp_states[("user", "session")] = {"evidence": state}
        parent_catalog = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
        )
        tool_name = "mcp_evidence_lookup"

        state.tools.append({
            "name": "new-admin-tool",
            "description": "arrived after parent freeze",
            "inputSchema": {
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
            },
        })
        observed: dict[str, object] = {}

        async def fake_run_stream(model_id, messages, tools, **kwargs):
            scoped = mcp_client.freeze_session_mcp_catalog(
                "user",
                "session",
            )
            observed["names"] = [
                descriptor.public_name
                for descriptor in scoped.descriptors
            ]
            observed["parent_revision"] = scoped.parent_catalog_revision
            yield {"type": "delta", "content": "retrieved evidence " * 30}
            yield {"type": "done", "finish_reason": "stop"}

        context = ToolContext(
            user_id="user",
            session_id="session",
            model_id="model",
            provider_config={"base_url": "http://example", "api_model": "model"},
            enabled_tools=(tool_name,),
            frozen_mcp_catalog=parent_catalog,
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
                    "goal": "query only the parent-granted evidence tool",
                    "step_type": "knowledge_bootstrap",
                    "tools": [tool_name],
                },
                context,
                0,
                parallel_child=False,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed["names"], [tool_name])
        self.assertEqual(
            observed["parent_revision"],
            parent_catalog.catalog_revision,
        )
        self.assertIn(
            "mcp_evidence_new-admin-tool",
            [
                descriptor.public_name
                for descriptor in mcp_client.freeze_session_mcp_catalog(
                    "user",
                    "session",
                ).descriptors
            ],
        )

    def test_mcp_policy_is_conservative_unless_explicitly_trusted(self):
        state = self._named_state("evidence", "lookup", "declared")
        state.tools[0]["annotations"] = {
            "readOnlyHint": True,
            "idempotentHint": True,
        }
        # A Skill-owned config cannot opt itself into trusted policy.
        state.config["_trusted_tool_policy"] = {
            "mcp_evidence_lookup": {
                "read_only": True,
                "idempotent": True,
                "parallel_child_safe": True,
            },
        }
        mcp_client._mcp_states[("user", "session")] = {"evidence": state}

        default = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
        ).get("mcp_evidence_lookup")
        trusted_annotations = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
            trusted_annotation_servers={"evidence"},
        ).get("mcp_evidence_lookup")
        trusted_control_plane = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
            trusted_policy_overrides={
                "mcp_evidence_lookup": {
                    "read_only": True,
                    "idempotent": True,
                    "external": False,
                    "parallel_child_safe": True,
                },
            },
        ).get("mcp_evidence_lookup")

        self.assertTrue(default.policy.mutating)
        self.assertFalse(default.policy.idempotent)
        self.assertTrue(default.policy.external)
        self.assertFalse(default.policy.parallel_child_safe)
        self.assertEqual(default.policy.authority, "conservative_default")

        self.assertFalse(trusted_annotations.policy.mutating)
        self.assertTrue(trusted_annotations.policy.idempotent)
        self.assertTrue(trusted_annotations.policy.external)
        self.assertFalse(trusted_annotations.policy.parallel_child_safe)

        self.assertFalse(trusted_control_plane.policy.mutating)
        self.assertTrue(trusted_control_plane.policy.idempotent)
        self.assertFalse(trusted_control_plane.policy.external)
        self.assertTrue(trusted_control_plane.policy.parallel_child_safe)
        self.assertEqual(
            trusted_control_plane.policy.authority,
            "trusted_control_plane",
        )

    def test_unsupported_schema_is_rejected_instead_of_silently_weakened(self):
        state = self._state("unsupported")
        state.tools[0]["inputSchema"] = {
            "type": "object",
            "properties": {
                "email": {"type": "string", "format": "email"},
            },
        }
        mcp_client._mcp_states[("user", "session")] = {"same-name": state}

        catalog = mcp_client.freeze_session_mcp_catalog("user", "session")

        self.assertEqual(catalog.descriptors, ())
        self.assertEqual(len(catalog.rejected_tools), 1)
        self.assertIn("inputSchema.properties.email.format", catalog.rejected_tools[0].reason)
        self.assertEqual(
            mcp_client.get_session_tool_definitions("user", "session"),
            [],
        )

    async def test_failed_run_freeze_stays_closed_after_live_catalog_recovers(self):
        state = self._named_state("evidence", "lookup", "recovered")
        mcp_client._mcp_states[("user", "session")] = {
            "evidence": state,
        }
        tool_name = "mcp_evidence_lookup"

        # The run's initial catalog freeze fails.  AgentLoop converts this
        # exception into the same sealed-empty boundary exercised below.
        with patch.object(
            mcp_client,
            "_freeze_live_session_mcp_catalog",
            side_effect=RuntimeError("discovery unavailable"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "discovery unavailable",
            ):
                mcp_client.freeze_session_mcp_catalog("user", "session")
        failed_catalog = mcp_contract.sealed_empty_mcp_catalog(
            "freeze_failed"
        )

        # Live state is now healthy.  Neither schema construction nor strict
        # preflight/dispatch may reinterpret that recovery as run authority.
        with patch.object(
            mcp_client,
            "get_session_tool_definitions",
            side_effect=AssertionError("live definition fallback"),
        ) as live_definitions:
            definitions = _session_mcp_definitions_for_tools(
                "user",
                "session",
                [tool_name],
                allow_all=True,
                frozen_catalog=failed_catalog,
            )
        self.assertEqual(definitions, [])
        live_definitions.assert_not_called()

        with patch.object(
            mcp_client,
            "freeze_session_mcp_catalog",
            side_effect=AssertionError("live catalog rediscovery"),
        ) as refreeze:
            preflight = (
                mcp_client.preflight_frozen_session_mcp_tool_call(
                    tool_name,
                    {"query": "term"},
                    "user",
                    "session",
                    frozen_catalog=failed_catalog,
                )
            )
        self.assertFalse(preflight.ok)
        self.assertEqual(preflight.reason, "mcp_catalog_freeze_failed")
        refreeze.assert_not_called()

        with (
            patch.object(
                mcp_client,
                "connect_all_for_user",
                AsyncMock(return_value={"evidence": True}),
            ) as connect,
            patch.object(
                mcp_client,
                "_call_mcp_state_tool",
                AsyncMock(return_value="unexpected"),
            ) as transport,
        ):
            result = json.loads(await mcp_client.dispatch_mcp_tool(
                tool_name,
                {"query": "term"},
                "user",
                "session",
                frozen_catalog=failed_catalog,
            ))
        self.assertEqual(result["reason"], "mcp_catalog_freeze_failed")
        self.assertEqual(
            result["catalog_resolution_status"],
            "freeze_failed",
        )
        connect.assert_not_awaited()
        transport.assert_not_awaited()

    def test_not_enabled_and_failed_catalogs_are_distinct_closed_boundaries(self):
        not_enabled = mcp_contract.sealed_empty_mcp_catalog("not_enabled")
        freeze_failed = mcp_contract.sealed_empty_mcp_catalog(
            "freeze_failed"
        )

        self.assertTrue(not_enabled.sealed_closed)
        self.assertTrue(freeze_failed.sealed_closed)
        self.assertEqual(not_enabled.resolution_status, "not_enabled")
        self.assertEqual(freeze_failed.resolution_status, "freeze_failed")
        self.assertNotEqual(
            not_enabled.catalog_revision,
            freeze_failed.catalog_revision,
        )

    def test_inherited_failed_catalog_does_not_read_recovered_live_state(self):
        failed_catalog = mcp_contract.sealed_empty_mcp_catalog(
            "freeze_failed"
        )
        with (
            mcp_client.bind_inherited_frozen_mcp_catalog(
                "user",
                "session",
                failed_catalog,
            ),
            patch.object(
                mcp_client,
                "_freeze_live_session_mcp_catalog",
                side_effect=AssertionError("live catalog read"),
            ) as live_freeze,
        ):
            inherited = mcp_client.freeze_session_mcp_catalog(
                "user",
                "session",
            )
            child = mcp_client.freeze_child_session_mcp_catalog(
                failed_catalog,
                "user",
                "session",
                allowed_tool_names=["mcp_recovered_admin"],
            )

        self.assertIs(inherited, failed_catalog)
        self.assertEqual(child.resolution_status, "freeze_failed")
        self.assertEqual(child.descriptors, ())
        live_freeze.assert_not_called()

    async def test_serial_child_mcp_allowlist_includes_only_exact_declared_tool(self):
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
        parent_catalog = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
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
            frozen_mcp_catalog=parent_catalog,
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
                parallel_child=False,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed["tools"], [declared_name])
        self.assertFalse(observed["allow_session_mcp"])
        self.assertNotIn(hidden_name, observed["tools"])

    async def test_legacy_child_without_parent_frozen_catalog_cannot_acquire_live_mcp(self):
        state = self._named_state("evidence", "lookup", "live-only")
        mcp_client._mcp_states[("user", "session")] = {"evidence": state}
        tool_name = "mcp_evidence_lookup"
        context = ToolContext(
            user_id="user",
            session_id="session",
            model_id="model",
            provider_config={"base_url": "http://example", "api_model": "model"},
            enabled_tools=(tool_name,),
            run_id="parent",
            root_run_id="root",
        )

        with patch("agent_loop.run_stream") as run_stream:
            result = await _run_child(
                {
                    "goal": "attempt to acquire a live-only MCP capability",
                    "step_type": "knowledge_bootstrap",
                    "tools": [tool_name],
                },
                context,
                0,
                parallel_child=False,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            "mcp_capability_contract_violation",
            result["terminal_reason"],
        )
        self.assertEqual(
            "parent_frozen_mcp_catalog_missing",
            result["mcp_contract_rejections"][tool_name],
        )
        run_stream.assert_not_called()

    async def test_default_mcp_policy_is_rejected_for_parallel_child(self):
        declared_state = self._named_state("evidence", "lookup", "declared")
        mcp_client._mcp_states[("user", "session")] = {
            "evidence": declared_state,
        }
        tool_name = "mcp_evidence_lookup"
        parent_catalog = mcp_client.freeze_session_mcp_catalog(
            "user",
            "session",
        )
        context = ToolContext(
            user_id="user",
            session_id="session",
            model_id="model",
            provider_config={"base_url": "http://example", "api_model": "model"},
            enabled_tools=(tool_name,),
            frozen_mcp_catalog=parent_catalog,
            run_id="parent",
            root_run_id="root",
        )

        with patch("agent_loop.run_stream") as run_stream:
            result = await _run_child(
                {
                    "goal": "query evidence in a parallel worker",
                    "step_type": "knowledge_bootstrap",
                    "tools": [tool_name],
                },
                context,
                0,
                parallel_child=True,
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rejected_tools"], [tool_name])
        self.assertIn("safe for the child execution mode", result["error"])
        run_stream.assert_not_called()

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
                parallel_child=False,
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
