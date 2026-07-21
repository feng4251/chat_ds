import json
import unittest
from unittest.mock import patch

import tools.omission_guard as omission_guard
from tools.omission_guard import compacted_history_omission_error
from tools.context import ToolContext
from tools.registry import (
    ToolRegistry,
    _find_omission_path,
    json_schema_shape_error,
)


async def _dummy_handler(value: str = "") -> str:
    return value


class RegistryOmissionPathTests(unittest.TestCase):
    def test_returns_precise_nested_dict_path(self):
        args = {
            "filepath": "report.md",
            "content_omitted": {
                "_chatds_argument_omitted": True,
                "kind": "large_file_content",
            },
        }

        self.assertEqual(
            _find_omission_path(args),
            "args.content_omitted",
        )

    def test_returns_precise_path_for_json_string_marker_in_list(self):
        args = {
            "items": [
                {"content": "safe"},
                {
                    "content": json.dumps({
                        "_chatds_argument_omitted": True,
                        "kind": "large_argument",
                    }),
                },
            ],
        }

        self.assertEqual(
            _find_omission_path(args),
            "args.items[1].content",
        )

    def test_detects_string_true_marker_embedded_in_markdown(self):
        args = {
            "filepath": "report.md",
            "content": (
                "# Report\n\n```json\n"
                '{"_chatds_argument_omitted": "true", "chars": 5000}'
                "\n```"
            ),
        }

        self.assertEqual(_find_omission_path(args), "args.content")

    def test_detects_reserved_marker_inside_escaped_json_string(self):
        args = {
            "code": json.dumps(
                '{"_chatds_arguments_omitted": true, "kind": "large_argument"}'
            ),
        }

        self.assertEqual(_find_omission_path(args), "args.code")

    def test_does_not_match_ordinary_omitted_text_or_false_marker(self):
        safe_values = [
            "The author omitted an optional appendix.",
            '{"_chatds_argument_omitted": false}',
            '{"_chatds_argument_omitted": "false"}',
            "_chatds_argument_omitted: trueish",
            "Documentation mentions _chatds_argument_omitted without assigning it.",
            '{"not_chatds_argument_omitted": true}',
        ]

        for value in safe_values:
            with self.subTest(value=value):
                self.assertIsNone(_find_omission_path({"content": value}))

    def test_deep_python_value_fails_closed_without_recursion(self):
        value: object = "safe"
        for _ in range(2_000):
            value = [value]

        found = _find_omission_path({"content": value})

        self.assertTrue(found.endswith(".__chatds_omission_guard_depth_limit__"))
        error = compacted_history_omission_error(found)
        self.assertEqual(error["reason"], "omission_guard_limit_exceeded")
        self.assertEqual(error["limit_kind"], "depth")
        self.assertEqual(error["field"], "args")
        self.assertIn("request rejected without execution", error["error"])

    def test_deep_json_string_decoder_limit_fails_closed(self):
        deeply_nested_json = "[" * 2_000 + "0" + "]" * 2_000

        found = _find_omission_path({"content": deeply_nested_json})

        self.assertTrue(found.endswith(".__chatds_omission_guard_depth_limit__"))

    def test_node_budget_is_bounded_and_reports_stable_reason(self):
        with patch.object(omission_guard, "MAX_OMISSION_SCAN_NODES", 3):
            found = _find_omission_path({"items": ["one", "two", "three"]})

        self.assertTrue(found.endswith(".__chatds_omission_guard_node_limit__"))
        error = compacted_history_omission_error(found)
        self.assertEqual(error["reason"], "omission_guard_limit_exceeded")
        self.assertEqual(error["limit_kind"], "nodes")


class RegistryDefinitionTests(unittest.TestCase):
    def test_definitions_default_to_rejecting_additional_properties(self):
        registry = ToolRegistry()
        schema = {
            "description": "Demo tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                },
                "required": ["value"],
            },
        }
        registry.register(
            name="demo",
            toolset="test",
            schema=schema,
            handler=_dummy_handler,
        )

        definition = registry.get_definitions(["demo"])[0]["function"]

        self.assertFalse(definition["parameters"]["additionalProperties"])
        self.assertNotIn("additionalProperties", schema["parameters"])

    def test_definitions_preserve_explicit_additional_properties(self):
        registry = ToolRegistry()
        schema = {
            "description": "Extensible demo tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                },
                "additionalProperties": True,
            },
        }
        registry.register(
            name="extensible_demo",
            toolset="test",
            schema=schema,
            handler=_dummy_handler,
        )

        definition = registry.get_definitions(["extensible_demo"])[0]["function"]

        self.assertTrue(definition["parameters"]["additionalProperties"])

    def test_pure_argument_preflight_can_reject_without_dispatch(self):
        registry = ToolRegistry()
        observed: list[tuple[dict, ToolContext | None]] = []

        def validate(args: dict, context: ToolContext | None):
            observed.append((args, context))
            if args.get("value") == "blocked":
                return {
                    "error": "fixture deterministic denial",
                    "reason": "fixture_argument_denied",
                }
            return None

        registry.register(
            name="preflight_demo",
            toolset="test",
            schema={
                "description": "Pure preflight fixture.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
            handler=_dummy_handler,
            args_preflight_fn=validate,
        )
        context = ToolContext(user_id="u", session_id="s")

        denied = registry.preflight(
            "preflight_demo",
            {"value": "blocked"},
            context,
        )
        allowed = registry.preflight(
            "preflight_demo",
            {"value": "ok"},
            context,
        )

        self.assertFalse(denied.ok)
        self.assertEqual(denied.reason, "fixture_argument_denied")
        self.assertTrue(allowed.ok, allowed.error_payload)
        self.assertEqual(len(observed), 2)


class RegistryNestedSchemaValidationTests(unittest.TestCase):
    def _registry(self, parameters: dict) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            name="structured_demo",
            toolset="test",
            schema={
                "description": "Generic structured-data tool.",
                "parameters": parameters,
            },
            handler=_dummy_handler,
        )
        return registry

    def _parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "batch": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "minLength": 2,
                                "maxLength": 5,
                                "pattern": "^[A-Z][0-9]{1,3}$",
                            },
                            "mode": {"const": "sync"},
                            "target": {
                                "anyOf": [
                                    {"type": "string", "minLength": 1},
                                    {"type": "integer", "minimum": 1},
                                ],
                            },
                            "channel": {
                                "oneOf": [
                                    {"const": "email"},
                                    {"const": "queue"},
                                ],
                            },
                            "tags": {
                                "type": "array",
                                "maxItems": 3,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "metadata": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "string",
                                    "maxLength": 8,
                                },
                            },
                        },
                        "required": ["id", "mode", "target", "channel", "tags"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["batch"],
            "additionalProperties": False,
        }

    def _valid_args(self) -> dict:
        return {
            "batch": [{
                "id": "A12",
                "mode": "sync",
                "target": 7,
                "channel": "email",
                "tags": ["ready", "safe"],
                "metadata": {"owner": "ops"},
            }],
        }

    def test_nested_object_array_and_composition_are_validated(self):
        registry = self._registry(self._parameters())

        result = registry.preflight("structured_demo", self._valid_args())

        self.assertTrue(result.ok, result.error_payload)
        self.assertEqual(self._valid_args(), result.args)

    def test_nested_required_item_and_extra_errors_include_argument_path(self):
        registry = self._registry(self._parameters())
        cases = []

        missing = self._valid_args()
        del missing["batch"][0]["id"]
        cases.append((missing, "args.batch[0].id", "required"))

        extra = self._valid_args()
        extra["batch"][0]["undeclared"] = True
        cases.append((extra, "args.batch[0].undeclared", "unexpected"))

        wrong_item = self._valid_args()
        wrong_item["batch"][0]["tags"] = ["ok", 3]
        cases.append((wrong_item, "args.batch[0].tags[1]", "string"))

        bad_additional_value = self._valid_args()
        bad_additional_value["batch"][0]["metadata"]["owner"] = 3
        cases.append((
            bad_additional_value,
            "args.batch[0].metadata.owner",
            "string",
        ))

        too_many = self._valid_args()
        too_many["batch"] = too_many["batch"] * 3
        cases.append((too_many, "args.batch", "at most 2 items"))

        for args, field_path, fragment in cases:
            with self.subTest(field_path=field_path):
                result = registry.preflight("structured_demo", args)
                self.assertFalse(result.ok)
                self.assertEqual("tool_schema_validation_failed", result.reason)
                error = str((result.error_payload or {}).get("error") or "")
                self.assertIn(field_path, error)
                self.assertIn(fragment, error)

    def test_const_pattern_anyof_and_oneof_fail_deterministically(self):
        registry = self._registry(self._parameters())
        cases = []

        wrong_const = self._valid_args()
        wrong_const["batch"][0]["mode"] = "async"
        cases.append((wrong_const, "args.batch[0].mode", "const"))

        wrong_pattern = self._valid_args()
        wrong_pattern["batch"][0]["id"] = "lower"
        cases.append((wrong_pattern, "args.batch[0].id", "pattern"))

        wrong_anyof = self._valid_args()
        wrong_anyof["batch"][0]["target"] = False
        cases.append((wrong_anyof, "args.batch[0].target", "anyOf"))

        wrong_oneof = self._valid_args()
        wrong_oneof["batch"][0]["channel"] = "other"
        cases.append((wrong_oneof, "args.batch[0].channel", "oneOf"))

        for args, field_path, fragment in cases:
            with self.subTest(fragment=fragment):
                result = registry.preflight("structured_demo", args)
                error = str((result.error_payload or {}).get("error") or "")
                self.assertFalse(result.ok)
                self.assertIn(field_path, error)
                self.assertIn(fragment, error)

    def test_malformed_unused_nested_schema_fails_closed(self):
        parameters = self._parameters()
        parameters["properties"]["unused"] = {
            "type": "array",
            "items": [],
        }
        registry = self._registry(parameters)

        result = registry.preflight("structured_demo", self._valid_args())

        self.assertFalse(result.ok)
        self.assertEqual("tool_schema_validation_failed", result.reason)
        error = str((result.error_payload or {}).get("error") or "")
        self.assertIn("schema.parameters.properties.unused.items", error)
        self.assertIn("invalid argument schema", error)

    def test_schema_depth_and_node_limits_fail_closed(self):
        registry = self._registry(self._parameters())

        with patch("tools.registry._MAX_SCHEMA_VALIDATION_DEPTH", 2):
            depth = registry.preflight("structured_demo", self._valid_args())
        with patch("tools.registry._MAX_SCHEMA_VALIDATION_NODES", 5):
            nodes = registry.preflight("structured_demo", self._valid_args())

        self.assertFalse(depth.ok)
        self.assertIn(
            "bounded depth limit",
            str((depth.error_payload or {}).get("error") or ""),
        )
        self.assertFalse(nodes.ok)
        self.assertIn(
            "bounded node limit",
            str((nodes.error_payload or {}).get("error") or ""),
        )

    def test_contract_mode_rejects_keywords_the_registry_cannot_enforce(self):
        schema = {
            "type": "string",
            "format": "email",
            "description": "annotation remains lossless",
        }

        # Native registry schemas retain their historical compatibility mode;
        # exact external contracts can opt into fail-closed semantics.
        self.assertIsNone(json_schema_shape_error(schema))
        error = json_schema_shape_error(
            schema,
            reject_unsupported_keywords=True,
        )

        self.assertIn("schema.format", error or "")
        self.assertIn("silently weaken", error or "")


class RegistryOmissionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_placeholder_is_rejected_before_native_handler(self):
        calls = []

        async def handler(filepath: str, content: str) -> str:
            calls.append((filepath, content))
            return "unexpected"

        registry = ToolRegistry()
        registry.register(
            name="write_file",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["filepath", "content"],
                },
            },
            handler=handler,
        )

        result = json.loads(await registry.dispatch("write_file", {
            "filepath": "report.md",
            "content": (
                "# Report\n"
                '{"_chatds_argument_omitted": "true", "chars": 1000}'
            ),
        }))

        self.assertEqual(result["reason"], "invalid_placeholder_content")
        self.assertEqual(result["field"], "args.content")
        self.assertEqual(calls, [])

    async def test_scan_limit_is_rejected_before_native_handler(self):
        calls = []

        async def handler(value) -> str:
            calls.append(value)
            return "unexpected"

        registry = ToolRegistry()
        registry.register(
            name="bounded_demo",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {"value": {}},
                },
            },
            handler=handler,
        )
        with patch.object(omission_guard, "MAX_OMISSION_SCAN_NODES", 2):
            result = json.loads(await registry.dispatch(
                "bounded_demo",
                {"value": ["one", "two"]},
            ))

        self.assertEqual(result["reason"], "omission_guard_limit_exceeded")
        self.assertEqual(result["limit_kind"], "nodes")
        self.assertEqual(result["field"], "args")
        self.assertEqual(calls, [])


class RegistryPurePreflightTests(unittest.TestCase):
    def _registry(self):
        calls = []

        async def handler(
            filepath: str,
            content: str,
            user_id: str = "default",
        ) -> str:
            calls.append((filepath, content, user_id))
            return "unexpected"

        registry = ToolRegistry()
        registry.register(
            name="write_file",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filepath": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["filepath", "content"],
                },
            },
            handler=handler,
        )
        return registry, calls

    def test_preflight_is_pure_and_returns_exact_normalized_dispatch_args(self):
        registry, calls = self._registry()

        result = registry.preflight(
            "write_file",
            {
                "path": "report.md",
                "content": "real content",
                "user_id": "model-controlled",
                "extra": "ignored",
            },
            context=ToolContext(user_id="runtime-owned"),
            allowed_tool_names={"write_file"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.args, {
            "filepath": "report.md",
            "content": "real content",
        })
        self.assertEqual(result.semantic_args, result.args)
        self.assertEqual(result.ignored_args, ("extra",))
        self.assertEqual(calls, [])

    def test_preflight_rejects_parse_schema_depth_and_capability_states(self):
        registry, calls = self._registry()
        deeply_nested: object = "safe"
        for _ in range(100):
            deeply_nested = [deeply_nested]

        cases = (
            (
                {"__tool_arg_parse_error": "unterminated", "_raw_args": "x"},
                {"write_file"},
                "malformed_tool_arguments",
            ),
            (
                {"filepath": "report.md"},
                {"write_file"},
                "tool_schema_validation_failed",
            ),
            (
                {"filepath": "report.md", "content": deeply_nested},
                {"write_file"},
                "omission_guard_limit_exceeded",
            ),
            (
                {"filepath": "report.md", "content": "safe"},
                {"read_file"},
                "tool_capability_boundary_violation",
            ),
        )
        for args, allowed, reason in cases:
            with self.subTest(reason=reason):
                result = registry.preflight(
                    "write_file",
                    args,
                    context=ToolContext(),
                    allowed_tool_names=allowed,
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, reason)
        self.assertEqual(calls, [])

    def test_preflight_enforces_delegated_resource_boundary_without_handler(self):
        calls = []

        async def handler(filepath: str) -> str:
            calls.append(filepath)
            return "unexpected"

        registry = ToolRegistry()
        registry.register(
            name="read_file",
            toolset="test",
            schema={
                "parameters": {
                    "type": "object",
                    "properties": {"filepath": {"type": "string"}},
                    "required": ["filepath"],
                },
            },
            handler=handler,
        )
        context = ToolContext(
            delegated_resource_boundary=True,
            allowed_read_paths=("allowed.md",),
        )

        rejected = registry.preflight(
            "read_file",
            {"filepath": "undeclared.md", "invalid_noise": "alpha"},
            context=context,
            allowed_tool_names={"read_file"},
        )
        accepted = registry.preflight(
            "read_file",
            {"filepath": "allowed.md"},
            context=context,
            allowed_tool_names={"read_file"},
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(
            rejected.reason,
            "delegated_resource_boundary_violation",
        )
        # Boundary checks retain the original normalized payload, while the
        # convergence-only projection removes schema-unknown fields so a model
        # cannot vary irrelevant extras to reset a denial fingerprint.
        self.assertEqual(rejected.args, {
            "filepath": "undeclared.md",
            "invalid_noise": "alpha",
        })
        self.assertEqual(
            rejected.semantic_args,
            {"filepath": "undeclared.md"},
        )
        self.assertTrue(accepted.ok)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
