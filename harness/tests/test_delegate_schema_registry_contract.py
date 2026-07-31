from __future__ import annotations

import copy
import hashlib
import json
import unittest
from typing import Any

from tools.context import ToolContext
from tools.delegation import (
    DELEGATE_TASK_SCHEMA,
    _EXACT_CAPABILITY_BINDING_FIELDS,
    _strict_exact_capability_bindings,
    _strict_knowledge_gate_plan,
    _strict_unconditional_capability_plan,
)
from tools.registry import ToolRegistry


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _networked_candidates() -> list[dict[str, Any]]:
    """Return cross-domain exact candidates without performing network I/O."""

    return [
        {
            "candidate_id": "catalog-script",
            "kind": "skill_script",
            "tool_name": "run_skill_python",
            "tool_names": ["run_skill_python"],
            "skill_name": "archive-catalog-adapter",
            "resource_path": "scripts/query_catalog.py",
            "sha256": "1" * 64,
            "skill_md_sha256": "2" * 64,
            "package_sha256": "3" * 64,
            "runtime_profile": "base-v1",
            "required_cwd": "script",
            "sandbox_egress_url_prefixes": [
                "https://catalog.example.test/data/",
            ],
            "sandbox_egress_rules": [{
                "methods": ["GET", "HEAD"],
                "url_prefix": "https://catalog.example.test:443/data/",
            }],
        },
        {
            "candidate_id": "archive-browser",
            "kind": "native_tool",
            "tool_name": "browser_navigate",
            "tool_names": ["browser_navigate"],
            "browser_egress_rules": [{
                "methods": ["GET", "HEAD"],
                "url_prefix": "https://news.example.test:443/archive/",
            }],
        },
    ]


def _unconditional_plan() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "worker_id": "records-worker",
        "owner_skill": "archive-evidence-board",
        "selectors": [
            "skill:archive-catalog-adapter",
            "browser_navigate",
        ],
        "candidates": _networked_candidates(),
    }


def _knowledge_gate_plan() -> dict[str, Any]:
    candidates = _networked_candidates()
    return {
        "schema_version": 1,
        "worker_id": "records-worker",
        "owner_skill": "archive-evidence-board",
        "checks": [{
            "id": "source-check",
            "question": "Is one external catalog source required?",
            "legacy_ambiguous": False,
            "branches": [{
                "outcome": "yes",
                "action": "Acquire one exact archive source.",
                "group_ids": ["source-group"],
            }],
        }],
        "groups": [{
            "id": "source-group",
            "check_id": "source-check",
            "outcome": "yes",
            "mode": "one_of",
            "candidate_ids": [
                candidate["candidate_id"] for candidate in candidates
            ],
            "selectors": [
                "skill:archive-catalog-adapter",
                "browser_navigate",
            ],
            "unresolved_selectors": [],
        }],
        "candidates": candidates,
    }


def _delegated_task(location: str) -> dict[str, Any]:
    task: dict[str, Any] = {
        "goal": "Produce one bounded archive evidence record.",
        "tools": ["run_skill_python", "browser_navigate"],
        "skill_name": "archive-evidence-board",
        "worker_id": "records-worker",
        "worker_file": "workers/records.yaml",
        "workflow_stage": "evidence-wave",
        "step_type": "worker",
        "step_id": "records-worker",
        "parallel_stage": True,
    }
    if location == "capability_bindings":
        bindings = _networked_candidates()
        task["capability_bindings"] = bindings
        task["capability_bindings_sha256"] = _canonical_digest(bindings)
    elif location == "unconditional_capability_plan":
        plan = _unconditional_plan()
        task["unconditional_capability_plan"] = plan
        task["unconditional_capability_plan_sha256"] = _canonical_digest(plan)
    elif location == "knowledge_gate_plan":
        plan = _knowledge_gate_plan()
        task["knowledge_gate_plan"] = plan
        task["knowledge_gate_plan_sha256"] = _canonical_digest(plan)
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(f"unsupported location: {location}")
    return task


def _candidate_schema_locations() -> dict[str, dict[str, Any]]:
    properties = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_properties = properties["tasks"]["items"]["properties"]
    return {
        "single.capability_bindings": properties[
            "capability_bindings"
        ]["items"],
        "single.unconditional_capability_plan": properties[
            "unconditional_capability_plan"
        ]["properties"]["candidates"]["items"],
        "single.knowledge_gate_plan": properties[
            "knowledge_gate_plan"
        ]["properties"]["candidates"]["items"],
        "batch.capability_bindings": task_properties[
            "capability_bindings"
        ]["items"],
        "batch.unconditional_capability_plan": task_properties[
            "unconditional_capability_plan"
        ]["properties"]["candidates"]["items"],
        "batch.knowledge_gate_plan": task_properties[
            "knowledge_gate_plan"
        ]["properties"]["candidates"]["items"],
    }


class DelegatePublicSchemaContractTests(unittest.TestCase):
    def test_every_public_candidate_schema_matches_handler_field_contract(
        self,
    ) -> None:
        expected_fields = set(_EXACT_CAPABILITY_BINDING_FIELDS)

        for location, candidate_schema in _candidate_schema_locations().items():
            with self.subTest(location=location):
                self.assertIs(candidate_schema["additionalProperties"], False)
                self.assertEqual(
                    expected_fields,
                    set(candidate_schema["properties"]),
                )
                self.assertEqual(
                    {"candidate_id", "kind", "tool_names"},
                    set(candidate_schema["required"]),
                )

                for field in (
                    "sandbox_egress_rules",
                    "browser_egress_rules",
                ):
                    rule_schema = candidate_schema["properties"][field]["items"]
                    self.assertIs(rule_schema["additionalProperties"], False)
                    self.assertEqual(
                        {"methods", "url_prefix"},
                        set(rule_schema["properties"]),
                    )
                    self.assertEqual(
                        {"methods", "url_prefix"},
                        set(rule_schema["required"]),
                    )


class DelegateRegistryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_candidate_locations_cross_real_registry_dispatch(
        self,
    ) -> None:
        handler_calls: list[dict[str, Any]] = []

        async def handler(
            context: ToolContext | None = None,
            **payload: Any,
        ) -> str:
            handler_calls.append({
                "context": context,
                "payload": copy.deepcopy(payload),
            })
            task_count = len(payload.get("tasks") or [payload])
            return json.dumps({
                "status": "completed",
                "task_count": task_count,
                "completed_count": task_count,
                "degraded_completed_count": 0,
                "results": [
                    {"status": "completed"} for _ in range(task_count)
                ],
            })

        registry = ToolRegistry()
        registry.register(
            name="delegate_task",
            toolset="contract-test",
            schema=DELEGATE_TASK_SCHEMA,
            handler=handler,
        )
        context = ToolContext(
            user_id="contract-user",
            session_id="contract-session",
            model_id="contract-model",
            enabled_tools=("delegate_task",),
            run_id="contract-root-run",
            root_run_id="contract-root-run",
        )

        for batch in (False, True):
            for location in (
                "capability_bindings",
                "unconditional_capability_plan",
                "knowledge_gate_plan",
            ):
                with self.subTest(batch=batch, location=location):
                    task = _delegated_task(location)
                    if location == "capability_bindings":
                        _normalized, _digest, error = (
                            _strict_exact_capability_bindings(task)
                        )
                    elif location == "unconditional_capability_plan":
                        _normalized, _digest, error = (
                            _strict_unconditional_capability_plan(task)
                        )
                    else:
                        _normalized, _digest, error = (
                            _strict_knowledge_gate_plan(task)
                        )
                    self.assertIsNone(error)

                    args = {"tasks": [task]} if batch else task
                    preflight = registry.preflight(
                        "delegate_task",
                        args,
                        context,
                        allowed_tool_names={"delegate_task"},
                    )
                    self.assertTrue(
                        preflight.ok,
                        json.dumps(
                            preflight.error_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                    self.assertEqual(args, preflight.args)
                    self.assertEqual((), preflight.ignored_args)

                    call_count = len(handler_calls)
                    result = json.loads(await registry.dispatch(
                        "delegate_task",
                        args,
                        context,
                    ))
                    self.assertEqual("completed", result["status"])
                    self.assertEqual(call_count + 1, len(handler_calls))
                    self.assertIs(context, handler_calls[-1]["context"])
                    self.assertEqual(args, handler_calls[-1]["payload"])

        rejected_args = {
            "tasks": [_delegated_task("knowledge_gate_plan")],
        }
        rejected_args["tasks"][0]["knowledge_gate_plan"]["candidates"][0][
            "undeclared_network_field"
        ] = "must-not-cross-handler"
        rejected = registry.preflight(
            "delegate_task",
            rejected_args,
            context,
            allowed_tool_names={"delegate_task"},
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(
            "tool_schema_validation_failed",
            rejected.reason,
        )
        self.assertIs(
            (rejected.error_payload or {}).get("actual_dispatch_attempted"),
            False,
        )
        self.assertEqual(
            "not_dispatched",
            (rejected.error_payload or {}).get("dispatch_state"),
        )

        call_count = len(handler_calls)
        dispatch_error = json.loads(await registry.dispatch(
            "delegate_task",
            rejected_args,
            context,
        ))
        self.assertIs(dispatch_error["actual_dispatch_attempted"], False)
        self.assertEqual("not_dispatched", dispatch_error["dispatch_state"])
        self.assertEqual(call_count, len(handler_calls))
