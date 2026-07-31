import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_loop import (
    _declared_workflow_batch_preflight,
    _workflow_gate_call_error,
)
from tools.context import ToolContext
from knowledge_gate_runtime import (
    activated_knowledge_gate_candidate_authority,
    build_knowledge_gate_decision_receipt,
    validate_knowledge_gate_candidate_authority,
    validate_knowledge_gate_decisions,
)
from tools.delegation import (
    DELEGATE_TASK_SCHEMA,
    _exact_knowledge_gate_candidate_grants,
    _exact_knowledge_gate_gap_ledger_error,
    _knowledge_gate_receipt_audit,
    _run_child,
    _strict_knowledge_gate_plan,
    _strict_unconditional_capability_plan,
)
from tools.isolated_skill_executor import snapshot_skill_package
from skill_capability_plan import (
    build_callable_skill_result_receipt,
    build_skill_process_evidence_receipt,
)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _native_plan(*, two_groups: bool = False) -> dict:
    group_ids = ["gate-group-1"]
    groups = [{
        "id": "gate-group-1",
        "check_id": "KG-1",
        "outcome": "yes",
        "mode": "one_of",
        "candidate_ids": ["candidate-search"],
        "selectors": ["web_search"],
        "unresolved_selectors": [],
    }]
    if two_groups:
        group_ids.append("gate-group-2")
        groups.append({
            "id": "gate-group-2",
            "check_id": "KG-1",
            "outcome": "yes",
            "mode": "one_of",
            "candidate_ids": ["candidate-search"],
            "selectors": ["web_search"],
            "unresolved_selectors": [],
        })
    return {
        "schema_version": 1,
        "worker_id": "worker-a",
        "owner_skill": "generic-skill",
        "checks": [{
            "id": "KG-1",
            "question": "Is external evidence required?",
            "branches": [{
                "outcome": "yes",
                "action": "Retrieve one exact source.",
                "group_ids": group_ids,
            }],
            "legacy_ambiguous": False,
        }],
        "groups": groups,
        "candidates": [{
            "candidate_id": "candidate-search",
            "kind": "native_tool",
            "tool_name": "web_search",
            "tool_names": ["web_search"],
        }],
    }


def _static_native_plan(tool_name: str = "web_extract") -> dict:
    return {
        "schema_version": 1,
        "worker_id": "worker-a",
        "owner_skill": "generic-skill",
        "selectors": ["WebFetch"],
        "candidates": [{
            "candidate_id": "candidate-static-fetch",
            "kind": "native_tool",
            "tool_name": tool_name,
            "tool_names": [tool_name],
        }],
    }


def _resource_plan(
    *,
    skill_md_sha256: str,
    package_sha256: str,
    resource_sha256: str,
) -> dict:
    return {
        "schema_version": 1,
        "worker_id": "worker-a",
        "owner_skill": "generic-skill",
        "checks": [{
            "id": "KG-1",
            "question": "Is the declared local evidence required?",
            "branches": [{
                "outcome": "yes",
                "action": "Read the exact supporting resource.",
                "group_ids": ["gate-group-resource"],
            }],
            "legacy_ambiguous": False,
        }],
        "groups": [{
            "id": "gate-group-resource",
            "check_id": "KG-1",
            "outcome": "yes",
            "mode": "one_of",
            "candidate_ids": ["candidate-resource"],
            "selectors": ["skill:generic-skill/references/gate.md"],
            "unresolved_selectors": [],
        }],
        "candidates": [{
            "candidate_id": "candidate-resource",
            "kind": "skill_resource",
            "skill_name": "generic-skill",
            "resource_path": "references/gate.md",
            "sha256": resource_sha256,
            "skill_md_sha256": skill_md_sha256,
            "package_sha256": package_sha256,
            "tool_names": [],
        }],
    }


def _process_plan(*, two_groups: bool = False) -> dict:
    group_ids = ["gate-group-process-1"]
    groups = [{
        "id": "gate-group-process-1",
        "check_id": "KG-1",
        "outcome": "yes",
        "mode": "one_of",
        "candidate_ids": ["candidate-process"],
        "selectors": ["skill:generic-skill/scripts/query.py"],
        "unresolved_selectors": [],
    }]
    if two_groups:
        group_ids.append("gate-group-process-2")
        groups.append({
            **groups[0],
            "id": "gate-group-process-2",
        })
    return {
        "schema_version": 1,
        "worker_id": "worker-a",
        "owner_skill": "generic-skill",
        "checks": [{
            "id": "KG-1",
            "question": "Is the exact process evidence required?",
            "branches": [{
                "outcome": "yes",
                "action": "Run the exact persistent entrypoint.",
                "group_ids": group_ids,
            }],
            "legacy_ambiguous": False,
        }],
        "groups": groups,
        "candidates": [{
            "candidate_id": "candidate-process",
            "kind": "skill_script",
            "skill_name": "generic-skill",
            "resource_path": "scripts/query.py",
            "sha256": "a" * 64,
            "package_sha256": "b" * 64,
            "tool_names": ["run_skill_process"],
        }],
    }


def _context(*tools: str) -> ToolContext:
    return ToolContext(
        user_id="u",
        session_id="s",
        model_id="model",
        provider_config={
            "base_url": "http://example",
            "api_model": "model",
            "context_length": 303_872,
        },
        enabled_tools=tools,
        run_id="parent",
        root_run_id="root",
        skill_execution_resource_boundary=True,
    )


def _decision_call(plan: dict, outcome: str = "yes") -> dict:
    return {
        "tool_name": "submit_knowledge_gate_decisions",
        "args": {
            "plan_sha256": _digest(plan),
            "decisions": [{
                "check_id": "KG-1",
                "outcome": outcome,
                "reason": "The exact task evidence requirement was assessed.",
            }],
        },
        "outcome": "success",
        "artifacts": [],
        "result_data": {},
        "skill_resource_complete": None,
    }


def _search_call(outcome: str = "success") -> dict:
    return {
        "tool_name": "web_search",
        "args": {"query": "bounded evidence"},
        "outcome": outcome,
        "artifacts": [],
        "result_data": {},
        "skill_resource_complete": None,
    }


def _http_call(
    prefix: str,
    outcome: str = "success",
    *,
    request_sent: bool = True,
    error_code: str | None = None,
    request_number: int | None = None,
) -> dict:
    result_data = {
        "request_sent": request_sent,
        "matched_skill": "generic-skill",
        "matched_prefix_sha256": hashlib.sha256(
            prefix.encode("utf-8"),
        ).hexdigest(),
    }
    if error_code is not None:
        result_data["error_code"] = error_code
    if request_number is not None:
        result_data["request_number"] = request_number
    return {
        "tool_name": "skill_http_get",
        # Delegation receipts deliberately do not retain the raw request URL.
        "args": {},
        "outcome": outcome,
        "artifacts": [],
        "result_data": result_data,
        "skill_resource_complete": None,
    }


class KnowledgeGatePlanContractTests(unittest.TestCase):
    def test_decision_phase_rejects_multi_call_batches_atomically(self):
        plan = _native_plan()
        digest = _digest(plan)
        context = replace(
            _context(
                "submit_knowledge_gate_decisions",
                "web_search",
            ),
            knowledge_gate_plan=plan,
            knowledge_gate_plan_sha256=digest,
        )
        decision_args = json.dumps(
            _decision_call(plan)["args"],
            ensure_ascii=False,
        )
        policy = {
            "tools": ["submit_knowledge_gate_decisions"],
            "max_calls": 1,
            "reason": "knowledge-gate exact decision phase",
        }

        one = [SimpleNamespace(
            id="decision-1",
            name="submit_knowledge_gate_decisions",
            arguments=decision_args,
        )]
        _prepared, failures, audit = (
            _declared_workflow_batch_preflight(
                one,
                policy=policy,
                exposed_tool_names={
                    "submit_knowledge_gate_decisions",
                },
                tool_context=context,
            )
        )
        self.assertEqual({}, failures)
        self.assertTrue(audit["accepted"])

        two_decisions = [
            *one,
            SimpleNamespace(
                id="decision-2",
                name="submit_knowledge_gate_decisions",
                arguments=decision_args,
            ),
        ]
        _prepared, failures, audit = (
            _declared_workflow_batch_preflight(
                two_decisions,
                policy=policy,
                exposed_tool_names={
                    "submit_knowledge_gate_decisions",
                },
                tool_context=context,
            )
        )
        self.assertEqual(
            {"decision-1", "decision-2"},
            set(failures),
        )
        self.assertFalse(audit["accepted"])
        self.assertTrue(all(
            item["dispatched"] is False
            for item in failures.values()
        ))

        decision_and_candidate = [
            *one,
            SimpleNamespace(
                id="candidate-1",
                name="web_search",
                arguments='{"query":"must not dispatch"}',
            ),
        ]
        _prepared, failures, audit = (
            _declared_workflow_batch_preflight(
                decision_and_candidate,
                policy=policy,
                exposed_tool_names={
                    "submit_knowledge_gate_decisions",
                    "web_search",
                },
                tool_context=context,
            )
        )
        self.assertEqual(
            {"decision-1", "candidate-1"},
            set(failures),
        )
        self.assertFalse(audit["accepted"])
        self.assertTrue(all(
            item["dispatched"] is False
            for item in failures.values()
        ))

    def test_delegate_schema_exposes_plan_at_single_and_batch_levels(self):
        properties = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("knowledge_gate_plan", properties)
        self.assertIn("knowledge_gate_plan_sha256", properties)
        task_properties = properties["tasks"]["items"]["properties"]
        self.assertIn("knowledge_gate_plan", task_properties)
        self.assertIn("knowledge_gate_plan_sha256", task_properties)
        self.assertIn("unconditional_capability_plan", properties)
        self.assertIn(
            "unconditional_capability_plan_sha256",
            properties,
        )
        self.assertIn(
            "unconditional_capability_plan",
            task_properties,
        )
        self.assertIn(
            "unconditional_capability_plan_sha256",
            task_properties,
        )

    def test_static_plan_and_digest_are_paired_and_identity_bound(self):
        plan = _static_native_plan()
        task = {
            "skill_name": "generic-skill",
            "worker_id": "worker-a",
            "unconditional_capability_plan": plan,
            "unconditional_capability_plan_sha256": _digest(plan),
        }
        normalized, digest, error = (
            _strict_unconditional_capability_plan(task)
        )
        self.assertIsNone(error)
        self.assertEqual(plan, normalized)
        self.assertEqual(_digest(plan), digest)

        forged = dict(task)
        forged["unconditional_capability_plan_sha256"] = "0" * 64
        normalized, _digest_value, error = (
            _strict_unconditional_capability_plan(forged)
        )
        self.assertIsNone(normalized)
        self.assertIn("does not match", error)

    def test_forced_workflow_freezes_static_plan_and_digest(self):
        plan = _static_native_plan()
        digest = _digest(plan)
        policy = {
            "tools": ["delegate_task"],
            "max_calls": 1,
            "delegate_step_type": "worker",
            "expected_step_ids": ["worker-a"],
            "expected_unconditional_capability_plans": {
                "worker-a": plan,
            },
            "expected_unconditional_capability_plan_sha256": {
                "worker-a": digest,
            },
        }
        task = {
            "goal": "Run the exact worker.",
            "skill_name": "generic-skill",
            "step_type": "worker",
            "step_id": "worker-a",
            "worker_id": "worker-a",
            "worker_file": "workers/worker-a.yaml",
            "tools": ["web_extract"],
            "unconditional_capability_plan": plan,
            "unconditional_capability_plan_sha256": digest,
        }

        self.assertEqual(
            "",
            _workflow_gate_call_error(
                policy,
                "delegate_task",
                {"tasks": [task]},
                prior_call_count=0,
            ),
        )
        forged = {
            **task,
            "unconditional_capability_plan_sha256": "0" * 64,
        }
        self.assertIn(
            "exact Harness-compiled",
            _workflow_gate_call_error(
                policy,
                "delegate_task",
                {"tasks": [forged]},
                prior_call_count=0,
            ),
        )

    def test_plan_and_digest_are_paired_and_identity_bound(self):
        plan = _native_plan()
        task = {
            "skill_name": "generic-skill",
            "worker_id": "worker-a",
            "knowledge_gate_plan": plan,
            "knowledge_gate_plan_sha256": _digest(plan),
        }
        normalized, digest, error = _strict_knowledge_gate_plan(task)
        self.assertIsNone(error)
        self.assertEqual(plan, normalized)
        self.assertEqual(_digest(plan), digest)

        forged = dict(task)
        forged["knowledge_gate_plan_sha256"] = "0" * 64
        normalized, _digest_value, error = _strict_knowledge_gate_plan(forged)
        self.assertIsNone(normalized)
        self.assertIn("does not match", error)

        wrong_worker = dict(task)
        wrong_worker["worker_id"] = "worker-b"
        normalized, _digest_value, error = _strict_knowledge_gate_plan(
            wrong_worker
        )
        self.assertIsNone(normalized)
        self.assertIn("does not match the delegated worker", error)

    def test_strict_boundary_uses_shared_unicode_id_contract(self):
        plan = _native_plan()
        plan["worker_id"] = "_研究员"
        plan["checks"][0]["id"] = "_检查"
        plan["checks"][0]["branches"][0]["group_ids"] = ["_组"]
        plan["groups"][0]["id"] = "_组"
        plan["groups"][0]["check_id"] = "_检查"
        plan["groups"][0]["candidate_ids"] = ["_候选"]
        plan["candidates"][0]["candidate_id"] = "_候选"
        normalized, _digest_value, error = _strict_knowledge_gate_plan({
            "skill_name": "generic-skill",
            "worker_id": "_研究员",
            "knowledge_gate_plan": plan,
            "knowledge_gate_plan_sha256": _digest(plan),
        })
        self.assertIsNone(error)
        self.assertEqual(plan, normalized)

        noncanonical = json.loads(json.dumps(plan))
        noncanonical["checks"][0]["id"] = "x" * 161
        noncanonical["groups"][0]["check_id"] = "x" * 161
        _normalized, _digest_value, error = _strict_knowledge_gate_plan({
            "skill_name": "generic-skill",
            "worker_id": "_研究员",
            "knowledge_gate_plan": noncanonical,
            "knowledge_gate_plan_sha256": _digest(noncanonical),
        })
        self.assertIn("at most 160 characters", error)

    def test_plan_candidate_is_not_authority(self):
        plan = _native_plan()
        grants, error = _exact_knowledge_gate_candidate_grants(
            plan,
            context=_context("submit_knowledge_gate_decisions"),
        )
        self.assertEqual([], grants["receipt_bindings"])
        self.assertIn("outside the parent grant", error)

        grants, error = _exact_knowledge_gate_candidate_grants(
            plan,
            context=_context(
                "submit_knowledge_gate_decisions",
                "web_search",
            ),
        )
        self.assertIsNone(error)
        self.assertEqual(
            ["candidate-search"],
            [
                item["candidate_id"]
                for item in grants["receipt_bindings"]
            ],
        )
        self.assertEqual(
            grants,
            validate_knowledge_gate_candidate_authority(plan, grants),
        )

    def test_skill_resource_plan_is_strict_and_revalidated_for_toctou(self):
        from tools.isolated_skill_executor import compute_skill_package_digest

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "references").mkdir()
            skill_md = root / "SKILL.md"
            resource = root / "references" / "gate.md"
            skill_md.write_text("# Generic Skill\n", encoding="utf-8")
            resource.write_text("bounded evidence\n", encoding="utf-8")
            skill_digest = hashlib.sha256(skill_md.read_bytes()).hexdigest()
            resource_digest = hashlib.sha256(resource.read_bytes()).hexdigest()
            package_digest = compute_skill_package_digest(root)
            plan = _resource_plan(
                skill_md_sha256=skill_digest,
                package_sha256=package_digest,
                resource_sha256=resource_digest,
            )
            normalized, _plan_digest, error = _strict_knowledge_gate_plan({
                "skill_name": "generic-skill",
                "worker_id": "worker-a",
                "knowledge_gate_plan": plan,
                "knowledge_gate_plan_sha256": _digest(plan),
            })
            self.assertIsNone(error)
            self.assertEqual([], normalized["candidates"][0]["tool_names"])
            incomplete_plan = json.loads(json.dumps(plan))
            incomplete_plan["candidates"][0].pop("package_sha256")
            _normalized, _digest_value, identity_error = (
                _strict_knowledge_gate_plan({
                    "skill_name": "generic-skill",
                    "worker_id": "worker-a",
                    "knowledge_gate_plan": incomplete_plan,
                    "knowledge_gate_plan_sha256": _digest(incomplete_plan),
                })
            )
            self.assertIn(
                "must bind both skill_md_sha256 and package_sha256",
                identity_error,
            )

            context = ToolContext(
                user_id="u",
                session_id="s",
                enabled_tools=("skill_view",),
                enabled_user_skills=("generic-skill",),
                skill_execution_resource_boundary=True,
                allowed_skill_resources=(
                    ("generic-skill", "SKILL.md"),
                    ("generic-skill", "references/gate.md"),
                ),
                allowed_skill_package_digests=(
                    ("generic-skill", package_digest),
                ),
            )
            with patch(
                "skills.scanner.resolve_skill_path",
                return_value=skill_md,
            ):
                grants, error = _exact_knowledge_gate_candidate_grants(
                    plan,
                    context=context,
                )
                self.assertIsNone(error)
                self.assertEqual(["skill_view"], grants["tool_names"])
                self.assertEqual(
                    grants,
                    validate_knowledge_gate_candidate_authority(
                        plan,
                        grants,
                    ),
                )

                skill_md.write_text(
                    "# Changed Generic Skill\n",
                    encoding="utf-8",
                )
                _grants, main_error = (
                    _exact_knowledge_gate_candidate_grants(
                        plan,
                        context=context,
                    )
                )
                self.assertIn(
                    "supporting SKILL.md changed",
                    main_error,
                )
                skill_md.write_text(
                    "# Generic Skill\n",
                    encoding="utf-8",
                )
                resource.write_text(
                    "changed evidence\n",
                    encoding="utf-8",
                )
                _grants, error = _exact_knowledge_gate_candidate_grants(
                    plan,
                    context=context,
                )
            self.assertIn("changed after knowledge-gate compilation", error)

    def test_activation_filters_same_bridge_http_and_command_coordinates(self):
        main_digest = "a" * 64
        package_digest = "b" * 64
        identity = {
            "skill_name": "generic-skill",
            "skill_md_sha256": main_digest,
            "package_sha256": package_digest,
        }
        candidates = [
            {
                "candidate_id": "http-yes",
                "kind": "skill_http_prefix",
                "tool_name": "skill_http_get",
                "tool_names": ["skill_http_get"],
                "url_prefix": "https://example.test/yes/",
                "http_method": "GET",
                **identity,
            },
            {
                "candidate_id": "command-yes",
                "kind": "declared_command",
                "tool_name": "run_declared_command",
                "tool_names": ["run_declared_command"],
                "command_id": "command-yes",
                "executable": "tool",
                "fixed_argv": ["--mode", "yes"],
                "additional_argv": False,
                "sandbox_egress_url_prefixes": [
                    "https://yes-command.example.test/v1/",
                ],
                **identity,
            },
            {
                "candidate_id": "http-no",
                "kind": "skill_http_prefix",
                "tool_name": "skill_http_get",
                "tool_names": ["skill_http_get"],
                "url_prefix": "https://example.test/no/",
                "http_method": "GET",
                **identity,
            },
            {
                "candidate_id": "command-no",
                "kind": "declared_command",
                "tool_name": "run_declared_command",
                "tool_names": ["run_declared_command"],
                "command_id": "command-no",
                "executable": "tool",
                "fixed_argv": ["--mode", "no"],
                "additional_argv": False,
                "sandbox_egress_url_prefixes": [
                    "https://no-command.example.test/v1/",
                ],
                **identity,
            },
        ]
        groups = [
            {
                "id": f"group-{outcome}-{kind}",
                "check_id": "KG-1",
                "outcome": outcome,
                "mode": "one_of",
                "candidate_ids": [f"{kind}-{outcome}"],
                "selectors": [f"skill:generic-skill/{kind}-{outcome}"],
                "unresolved_selectors": [],
            }
            for outcome in ("yes", "no")
            for kind in ("http", "command")
        ]
        plan = {
            "schema_version": 1,
            "worker_id": "worker-a",
            "owner_skill": "generic-skill",
            "checks": [{
                "id": "KG-1",
                "question": "Which exact coordinate set is applicable?",
                "branches": [
                    {
                        "outcome": outcome,
                        "action": f"Use only {outcome} coordinates.",
                        "group_ids": [
                            f"group-{outcome}-http",
                            f"group-{outcome}-command",
                        ],
                    }
                    for outcome in ("yes", "no")
                ],
                "legacy_ambiguous": False,
            }],
            "groups": groups,
            "candidates": candidates,
        }
        normalized, plan_digest, error = _strict_knowledge_gate_plan({
            "skill_name": "generic-skill",
            "worker_id": "worker-a",
            "knowledge_gate_plan": plan,
            "knowledge_gate_plan_sha256": _digest(plan),
        })
        self.assertIsNone(error)
        full_authority = {
            "resource_grants": [],
            "script_grants": [],
            "process_only_script_grants": [],
            "script_authority_grants": [],
            "package_grants": [("generic-skill", package_digest)],
            "command_grants": [
                (
                    "generic-skill",
                    "command-yes",
                    "tool",
                    ("--mode", "yes"),
                ),
                (
                    "generic-skill",
                    "command-no",
                    "tool",
                    ("--mode", "no"),
                ),
            ],
            "http_get_grants": [
                ("generic-skill", "https://example.test/yes/"),
                ("generic-skill", "https://example.test/no/"),
            ],
            "http_post_grants": [],
            "sandbox_egress_grants": [
                (
                    "generic-skill",
                    "https://yes-command.example.test/v1/",
                ),
                (
                    "generic-skill",
                    "https://no-command.example.test/v1/",
                ),
            ],
            "tool_names": [
                "skill_http_get",
                "run_declared_command",
            ],
            "receipt_bindings": candidates,
        }
        validated_authority = validate_knowledge_gate_candidate_authority(
            normalized,
            full_authority,
        )
        decision = validate_knowledge_gate_decisions(
            normalized,
            expected_sha256=plan_digest,
            supplied_sha256=plan_digest,
            decisions=[{
                "check_id": "KG-1",
                "outcome": "yes",
                "reason": "The yes branch matches the bounded task.",
            }],
        )
        self.assertEqual("accepted", decision["status"])
        activated = activated_knowledge_gate_candidate_authority(
            normalized,
            decision,
            validated_authority,
        )
        self.assertEqual(
            [("generic-skill", "https://example.test/yes/")],
            activated["http_get_grants"],
        )
        self.assertEqual(
            [(
                "generic-skill",
                "command-yes",
                "tool",
                ("--mode", "yes"),
            )],
            activated["command_grants"],
        )
        self.assertEqual(
            [(
                "generic-skill",
                "https://yes-command.example.test/v1/",
            )],
            activated["sandbox_egress_grants"],
        )
        self.assertNotIn(
            "http-no",
            {
                candidate["candidate_id"]
                for candidate in activated["receipt_bindings"]
            },
        )
        self.assertNotIn(
            "command-no",
            {
                candidate["candidate_id"]
                for candidate in activated["receipt_bindings"]
            },
        )

    def test_unactivated_branch_has_no_receipt_obligation(self):
        plan = _native_plan()
        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [_decision_call(plan, "no")],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIsNone(error)
        self.assertEqual([], audit["activated_group_ids"])
        self.assertEqual(["gate-group-1"], audit["unactivated_group_ids"])
        self.assertEqual([], audit["receipts"])

    def test_distinct_groups_cannot_reuse_one_dispatch(self):
        plan = _native_plan(two_groups=True)
        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [_decision_call(plan), _search_call()],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIn("distinct actual dispatch receipt", error)
        self.assertEqual(1, len(audit["successful_group_ids"]))
        self.assertEqual(1, len(audit["missing_receipt_group_ids"]))

    def test_overlapping_http_prefix_receipts_require_exact_safe_identity(self):
        broad_prefix = "https://example.test/api/"
        narrow_prefix = "https://example.test/api/v2/"
        identity = {
            "skill_name": "generic-skill",
            "skill_md_sha256": "a" * 64,
            "package_sha256": "b" * 64,
        }
        plan = {
            "schema_version": 1,
            "worker_id": "worker-a",
            "owner_skill": "generic-skill",
            "checks": [{
                "id": "KG-1",
                "question": "Are both exact HTTP evidence sources required?",
                "branches": [{
                    "outcome": "yes",
                    "action": "Dispatch both exact bounded HTTP sources.",
                    "group_ids": ["group-broad", "group-narrow"],
                }],
                "legacy_ambiguous": False,
            }],
            "groups": [
                {
                    "id": "group-broad",
                    "check_id": "KG-1",
                    "outcome": "yes",
                    "mode": "one_of",
                    "candidate_ids": ["candidate-broad"],
                    "selectors": [
                        "skill:generic-skill/https://example.test/api/",
                    ],
                    "unresolved_selectors": [],
                },
                {
                    "id": "group-narrow",
                    "check_id": "KG-1",
                    "outcome": "yes",
                    "mode": "one_of",
                    "candidate_ids": ["candidate-narrow"],
                    "selectors": [
                        "skill:generic-skill/https://example.test/api/v2/",
                    ],
                    "unresolved_selectors": [],
                },
            ],
            "candidates": [
                {
                    "candidate_id": "candidate-broad",
                    "kind": "skill_http_prefix",
                    "tool_name": "skill_http_get",
                    "tool_names": ["skill_http_get"],
                    "url_prefix": broad_prefix,
                    "http_method": "GET",
                    **identity,
                },
                {
                    "candidate_id": "candidate-narrow",
                    "kind": "skill_http_prefix",
                    "tool_name": "skill_http_get",
                    "tool_names": ["skill_http_get"],
                    "url_prefix": narrow_prefix,
                    "http_method": "GET",
                    **identity,
                },
            ],
        }
        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [
                _decision_call(plan),
                _http_call(narrow_prefix),
                _http_call(narrow_prefix),
            ],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[
                ("generic-skill", broad_prefix),
                ("generic-skill", narrow_prefix),
            ],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIn("distinct actual dispatch receipt", error)
        self.assertEqual(["group-narrow"], audit["successful_group_ids"])
        self.assertEqual(["group-broad"], audit["missing_receipt_group_ids"])
        self.assertEqual(
            ["candidate-narrow"],
            [receipt["candidate_id"] for receipt in audit["receipts"]],
        )

    def test_authenticated_pre_submit_transport_failure_is_a_failed_gate_receipt(self):
        prefix = "https://telemetry.example.test/archive/"
        identity = {
            "skill_name": "generic-skill",
            "skill_md_sha256": "a" * 64,
            "package_sha256": "b" * 64,
        }
        plan = {
            "schema_version": 1,
            "worker_id": "worker-a",
            "owner_skill": "generic-skill",
            "checks": [{
                "id": "KG-1",
                "question": "Is the exact telemetry archive required?",
                "branches": [{
                    "outcome": "yes",
                    "action": "Retrieve the exact bounded archive source.",
                    "group_ids": ["group-archive"],
                }],
                "legacy_ambiguous": False,
            }],
            "groups": [{
                "id": "group-archive",
                "check_id": "KG-1",
                "outcome": "yes",
                "mode": "one_of",
                "candidate_ids": ["candidate-archive"],
                "selectors": ["skill:generic-skill/archive"],
                "unresolved_selectors": [],
            }],
            "candidates": [{
                "candidate_id": "candidate-archive",
                "kind": "skill_http_prefix",
                "tool_name": "skill_http_get",
                "tool_names": ["skill_http_get"],
                "url_prefix": prefix,
                "http_method": "GET",
                **identity,
            }],
        }

        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [
                _decision_call(plan),
                _http_call(
                    prefix,
                    "error",
                    request_sent=False,
                    error_code="transport_error",
                    request_number=17,
                ),
            ],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[("generic-skill", prefix)],
            allowed_skill_http_post_prefixes=[],
        )

        self.assertIsNone(error)
        self.assertEqual([], audit["missing_receipt_group_ids"])
        self.assertEqual(["group-archive"], audit["failed_group_ids"])
        self.assertEqual(
            ["group:group-archive:failed"],
            audit["gap_ids"],
        )

    def test_dispatch_before_decision_cannot_satisfy_activated_group(self):
        plan = _native_plan()
        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [_search_call(), _decision_call(plan)],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIn("distinct actual dispatch receipt", error)
        self.assertEqual(
            ["gate-group-1"],
            audit["missing_receipt_group_ids"],
        )

    def test_unknown_and_failed_group_have_exact_gap_ids(self):
        plan = _native_plan()
        unknown_audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [_decision_call(plan, "unknown")],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIsNone(error)
        self.assertEqual(
            ["check:KG-1:unknown"],
            unknown_audit["gap_ids"],
        )

        failed_audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [_decision_call(plan), _search_call("error")],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIsNone(error)
        self.assertEqual(
            ["group:gate-group-1:failed"],
            failed_audit["gap_ids"],
        )
        content = (
            "DEGRADED: the exact evidence dispatch failed.\n"
            "KNOWLEDGE_GATE_GAPS_JSON: "
            '{"status":"degraded","gap_ids":'
            '["group:gate-group-1:failed"]}'
        )
        self.assertIsNone(_exact_knowledge_gate_gap_ledger_error(
            content,
            failed_audit["gap_ids"],
        ))
        self.assertIn(
            "exactly cover",
            _exact_knowledge_gate_gap_ledger_error(
                content,
                ["group:another:failed"],
            ),
        )

    def test_process_start_and_enqueue_do_not_satisfy_gate(self):
        plan = _process_plan()
        process_id = "sp_" + "c" * 32
        pending_calls = [
            {
                "tool_name": "run_skill_process",
                "args": {
                    "operation": "start",
                    "script_path": (
                        "skills/generic-skill/scripts/query.py"
                    ),
                    "args": ["term"],
                },
                "outcome": "pending",
                "artifacts": [],
                "result_data": {},
            },
            {
                "tool_name": "run_skill_process",
                "args": {
                    "operation": "call",
                    "process_id": process_id,
                    "method_name": "query",
                    "method_args": ["term"],
                },
                "outcome": "pending",
                "artifacts": [],
                "result_data": {},
            },
        ]
        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [_decision_call(plan), *pending_calls],
            allowed_skill_scripts=[(
                "generic-skill",
                "scripts/query.py",
                "a" * 64,
            )],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIn("distinct actual dispatch receipt", error)
        self.assertEqual(
            ["gate-group-process-1"],
            audit["missing_receipt_group_ids"],
        )

    def test_process_terminal_receipt_is_exact_and_not_replayable(self):
        plan = _process_plan(two_groups=True)
        process_id = "sp_" + "d" * 32
        callable_receipt = build_callable_skill_result_receipt(
            "run_skill_process",
            {"result": {"status": "success", "rows": [1]}},
        )
        receipt = build_skill_process_evidence_receipt(
            skill_name="generic-skill",
            script_resource="scripts/query.py",
            script_sha256="a" * 64,
            package_sha256="b" * 64,
            process_id=process_id,
            invocation_mode="instance",
            completion_kind="structured_call",
            outcome="success",
            call_id="33333333-3333-4333-8333-333333333333",
            method_name="query",
            call_result_status="success",
            callable_result_receipt=callable_receipt,
        )
        terminal_call = {
            "tool_name": "run_skill_process",
            "args": {"operation": "read", "process_id": process_id},
            "outcome": "success",
            "artifacts": [],
            "result_data": {"process_evidence_receipt": receipt},
        }
        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [
                _decision_call(plan),
                terminal_call,
                dict(terminal_call),
            ],
            allowed_skill_scripts=[(
                "generic-skill",
                "scripts/query.py",
                "a" * 64,
            )],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIn("distinct actual dispatch receipt", error)
        self.assertEqual(1, len(audit["successful_group_ids"]))
        self.assertEqual(1, len(audit["missing_receipt_group_ids"]))

        single_plan = _process_plan()
        wrong_process = {
            **terminal_call,
            "args": {
                "operation": "read",
                "process_id": "sp_" + "e" * 32,
            },
        }
        wrong_audit, wrong_error = _knowledge_gate_receipt_audit(
            single_plan,
            _digest(single_plan),
            [_decision_call(single_plan), wrong_process],
            allowed_skill_scripts=[(
                "generic-skill",
                "scripts/query.py",
                "a" * 64,
            )],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )
        self.assertIn("distinct actual dispatch receipt", wrong_error)
        self.assertEqual(
            ["gate-group-process-1"],
            wrong_audit["missing_receipt_group_ids"],
        )

    def test_process_typed_failure_is_a_failed_not_successful_gate_receipt(self):
        plan = _process_plan()
        process_id = "sp_" + "f" * 32
        callable_receipt = build_callable_skill_result_receipt(
            "run_skill_process",
            {
                "result": {
                    "status": "error",
                    "error": "upstream unavailable",
                },
            },
        )
        receipt = build_skill_process_evidence_receipt(
            skill_name="generic-skill",
            script_resource="scripts/query.py",
            script_sha256="a" * 64,
            package_sha256="b" * 64,
            process_id=process_id,
            invocation_mode="instance",
            completion_kind="structured_call",
            outcome="error",
            call_id="44444444-4444-4444-8444-444444444444",
            method_name="query",
            call_result_status="success",
            callable_result_receipt=callable_receipt,
        )
        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [
                _decision_call(plan),
                {
                    "tool_name": "run_skill_process",
                    "args": {
                        "operation": "read",
                        "process_id": process_id,
                    },
                    "outcome": "error",
                    "transport_outcome": "success",
                    "artifacts": [],
                    "result_data": {
                        "process_evidence_receipt": receipt,
                    },
                },
            ],
            allowed_skill_scripts=[(
                "generic-skill",
                "scripts/query.py",
                "a" * 64,
            )],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )

        self.assertIsNone(error)
        self.assertEqual([], audit["successful_group_ids"])
        self.assertEqual(
            ["gate-group-process-1"],
            audit["failed_group_ids"],
        )
        self.assertEqual(
            ["group:gate-group-process-1:failed"],
            audit["gap_ids"],
        )

    def test_exact_resource_preload_can_satisfy_only_its_matching_gate_group(
        self,
    ):
        resource_sha256 = "c" * 64
        plan = _resource_plan(
            skill_md_sha256="a" * 64,
            package_sha256="b" * 64,
            resource_sha256=resource_sha256,
        )
        exact_preload = {
            "tool_name": "skill_view",
            "args": {
                "name": "generic-skill",
                "file_path": "references/gate.md",
            },
            "outcome": "success",
            "artifacts": [],
            "result_data": {"sha256": resource_sha256},
            "skill_resource_complete": True,
            "deterministic_prerequisite_preload": True,
        }

        audit, error = _knowledge_gate_receipt_audit(
            plan,
            _digest(plan),
            [exact_preload, _decision_call(plan)],
            allowed_skill_scripts=[],
            allowed_skill_commands=[],
            allowed_skill_http_prefixes=[],
            allowed_skill_http_post_prefixes=[],
        )

        self.assertIsNone(error)
        self.assertEqual(
            ["gate-group-resource"],
            audit["successful_group_ids"],
        )
        self.assertEqual([], audit["missing_receipt_group_ids"])

        for changed_call in (
            {
                **exact_preload,
                "result_data": {"sha256": "d" * 64},
            },
            {
                **exact_preload,
                "tool_name": "read_file",
            },
            {
                **exact_preload,
                "skill_resource_complete": False,
            },
        ):
            rejected, rejected_error = _knowledge_gate_receipt_audit(
                plan,
                _digest(plan),
                [changed_call, _decision_call(plan)],
                allowed_skill_scripts=[],
                allowed_skill_commands=[],
                allowed_skill_http_prefixes=[],
                allowed_skill_http_post_prefixes=[],
            )
            self.assertIn(
                "distinct actual dispatch receipt",
                rejected_error,
            )
            self.assertEqual(
                ["gate-group-resource"],
                rejected["missing_receipt_group_ids"],
            )


class KnowledgeGateDelegatedChildTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_gate_resource_preload_is_bound_into_child_runtime(
        self,
    ):
        from tools.isolated_skill_executor import (
            compute_skill_package_digest,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "references").mkdir()
            main = root / "SKILL.md"
            resource = root / "references" / "gate.md"
            main.write_text("# Generic Skill\n", encoding="utf-8")
            resource.write_text("bounded local evidence\n", encoding="utf-8")
            main_sha256 = hashlib.sha256(main.read_bytes()).hexdigest()
            resource_sha256 = hashlib.sha256(
                resource.read_bytes()
            ).hexdigest()
            package_sha256 = compute_skill_package_digest(root)
            plan = _resource_plan(
                skill_md_sha256=main_sha256,
                package_sha256=package_sha256,
                resource_sha256=resource_sha256,
            )
            accepted = validate_knowledge_gate_decisions(
                plan,
                expected_sha256=_digest(plan),
                supplied_sha256=_digest(plan),
                decisions=[{
                    "check_id": "KG-1",
                    "outcome": "yes",
                    "reason": "The exact local evidence is required.",
                }],
            )
            typed_decision_receipt = (
                build_knowledge_gate_decision_receipt(accepted)
            )
            observed: dict[str, object] = {}

            async def fake_dispatch(name, args, *, context):
                self.assertEqual("skill_view", name)
                self.assertEqual("generic-skill", args["name"])
                selected_file = {
                    "SKILL.md": main,
                    "references/gate.md": resource,
                }[args["file_path"]]
                return json.dumps({
                    "success": True,
                    "content": selected_file.read_text(encoding="utf-8"),
                    "sha256": hashlib.sha256(
                        selected_file.read_bytes()
                    ).hexdigest(),
                    "truncated": False,
                })

            async def fake_run_stream(*args, **kwargs):
                observed.update(kwargs)
                receipts = kwargs[
                    "preloaded_knowledge_gate_resource_receipts"
                ]
                self.assertEqual(1, len(receipts))
                self.assertEqual(
                    {
                        "skill_name": "generic-skill",
                        "resource_path": "references/gate.md",
                        "sha256": resource_sha256,
                        "complete": True,
                    },
                    {
                        key: receipts[0][key]
                        for key in (
                            "skill_name",
                            "resource_path",
                            "sha256",
                            "complete",
                        )
                    },
                )
                call_id = "decision-preloaded-resource"
                yield {
                    "type": "agent_event",
                    "event_type": "tool.started",
                    "tool_name": "submit_knowledge_gate_decisions",
                    "tool_call_id": call_id,
                    "payload": {
                        "tool_name": (
                            "submit_knowledge_gate_decisions"
                        ),
                        "tool_call_id": call_id,
                        "args_compacted": {},
                    },
                }
                yield {
                    "type": "agent_event",
                    "event_type": "tool.dispatch_started",
                    "tool_name": "submit_knowledge_gate_decisions",
                    "tool_call_id": call_id,
                    "payload": {
                        "tool_name": (
                            "submit_knowledge_gate_decisions"
                        ),
                        "tool_call_id": call_id,
                        "actual_dispatch_attempted": True,
                        "audit_args": {},
                        "audit_args_are_dispatch_derived": True,
                    },
                }
                yield {
                    "type": "agent_event",
                    "event_type": "tool.completed",
                    "tool_name": "submit_knowledge_gate_decisions",
                    "tool_call_id": call_id,
                    "payload": {
                        "tool_name": (
                            "submit_knowledge_gate_decisions"
                        ),
                        "tool_call_id": call_id,
                        "outcome": "success",
                        "actual_dispatch_attempted": True,
                        "exact_capability_receipt": {
                            "result_data": {},
                            "knowledge_gate_decision_receipt": (
                                typed_decision_receipt
                            ),
                        },
                    },
                }
                yield {
                    "type": "delta",
                    "content": (
                        "Substantive result grounded in the exact preloaded "
                        "local evidence, with provenance and explicit scope. "
                        * 20
                    ),
                }
                yield {"type": "done", "finish_reason": "stop"}

            context = replace(
                _context("skill_view"),
                enabled_user_skills=("generic-skill",),
                allowed_skill_resources=(
                    ("generic-skill", "SKILL.md"),
                    ("generic-skill", "references/gate.md"),
                ),
                allowed_skill_package_digests=(
                    ("generic-skill", package_sha256),
                ),
            )
            with (
                patch(
                    "skills.scanner.resolve_skill_path",
                    return_value=main,
                ),
                patch(
                    "tools.delegation.registry_dispatch",
                    fake_dispatch,
                ),
                patch("agent_loop.run_stream", fake_run_stream),
                patch(
                    "tools.delegation.persist_result_for_history",
                    return_value="results/preloaded-resource.txt",
                ),
            ):
                result = await _run_child(
                    {
                        "goal": (
                            "Use the exact conditional local evidence and "
                            "produce one bounded finding."
                        ),
                        "skill_name": "generic-skill",
                        "worker_id": "worker-a",
                        "worker_file": "references/gate.md",
                        "step_type": "worker",
                        "step_id": "worker-a",
                        "workflow_stage": "analysis",
                        "tools": [
                            "skill_view",
                            "submit_knowledge_gate_decisions",
                        ],
                        "required_capability_skills": [
                            "generic-skill",
                        ],
                        "knowledge_gate_plan": plan,
                        "knowledge_gate_plan_sha256": _digest(plan),
                    },
                    context,
                    0,
                )

        self.assertEqual("completed", result["status"], result.get("error"))
        self.assertEqual(
            ["gate-group-resource"],
            result["knowledge_gate_receipt_audit"][
                "successful_group_ids"
            ],
        )
        self.assertTrue(
            observed["verified_preloaded_input_receipt"]["complete"]
        )

    async def test_unmarked_static_candidate_requires_no_receipt(self):
        plan = _static_native_plan()

        async def fake_run_stream(*args, **kwargs):
            self.assertEqual(["web_extract"], args[2])
            self.assertIsNone(kwargs.get("knowledge_gate_plan"))
            yield {
                "type": "delta",
                "content": (
                    "The optional static capability was not needed for this "
                    "bounded substantive result. " * 20
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/static-optional.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "answer using the static capability only if needed",
                    "skill_name": "generic-skill",
                    "worker_id": "worker-a",
                    "step_type": "knowledge_bootstrap",
                    "tools": ["web_extract"],
                    "unconditional_capability_plan": plan,
                    "unconditional_capability_plan_sha256": _digest(plan),
                },
                _context("web_extract"),
                0,
            )

        self.assertEqual("completed", result["status"], result.get("error"))
        self.assertEqual(
            [],
            result["capability_receipt_audit"]["required_tool_names"],
        )

    async def test_static_skill_candidate_requires_declared_main_preload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main = root / "SKILL.md"
            main.write_text(
                "---\n"
                "name: supporting-adapter\n"
                "description: Static adapter fixture.\n"
                "---\n",
                encoding="utf-8",
            )
            main_sha = hashlib.sha256(main.read_bytes()).hexdigest()
            package_sha = snapshot_skill_package(root).sha256
            plan = _static_native_plan()
            plan["selectors"] = ["skill:supporting-adapter"]
            plan["candidates"][0].update({
                "skill_name": "supporting-adapter",
                "skill_md_sha256": main_sha,
                "package_sha256": package_sha,
            })
            context = replace(
                _context("web_extract"),
                enabled_user_skills=("supporting-adapter",),
                allowed_skill_resources=(
                    ("supporting-adapter", "SKILL.md"),
                ),
                allowed_skill_package_digests=(
                    ("supporting-adapter", package_sha),
                ),
            )
            with (
                patch(
                    "skills.scanner.resolve_skill_path",
                    return_value=main,
                ),
                patch("agent_loop.run_stream") as run_stream,
            ):
                result = await _run_child(
                    {
                        "goal": "use one static supporting adapter",
                        "skill_name": "generic-skill",
                        "worker_id": "worker-a",
                        "step_type": "knowledge_bootstrap",
                        "tools": ["web_extract"],
                        "unconditional_capability_plan": plan,
                        "unconditional_capability_plan_sha256": _digest(
                            plan
                        ),
                    },
                    context,
                    0,
                )

        self.assertEqual("error", result["status"])
        self.assertIn(
            "required_capability_skills",
            result["error"],
        )
        self.assertIn("supporting-adapter", result["error"])
        run_stream.assert_not_called()

    async def test_gate_does_not_hide_unconditional_static_tool(self):
        gate_plan = _native_plan()
        static_plan = _static_native_plan()

        def started(name: str, call_id: str, args: dict) -> dict:
            return {
                "type": "agent_event",
                "event_type": "tool.started",
                "tool_name": name,
                "tool_call_id": call_id,
                "payload": {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "args_compacted": args,
                },
            }

        def completed(name: str, call_id: str) -> dict:
            return {
                "type": "agent_event",
                "event_type": "tool.completed",
                "tool_name": name,
                "tool_call_id": call_id,
                "payload": {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "outcome": "success",
                    "actual_dispatch_attempted": True,
                    "exact_capability_receipt": {"result_data": {}},
                },
            }

        async def fake_run_stream(*args, **kwargs):
            # run_stream receives the complete static base authority. Its
            # knowledge-gate middleware exposes only the decision schema until
            # the first typed receipt, then restores web_extract even though
            # the web_search branch below was not selected.
            self.assertEqual(
                {
                    "submit_knowledge_gate_decisions",
                    "web_extract",
                },
                set(args[2]),
            )
            self.assertEqual(
                ["web_search"],
                kwargs["knowledge_gate_candidate_authority"]["tool_names"],
            )
            yield started(
                "submit_knowledge_gate_decisions",
                "decision-1",
                _decision_call(gate_plan, outcome="no")["args"],
            )
            yield completed(
                "submit_knowledge_gate_decisions",
                "decision-1",
            )
            yield started(
                "web_extract",
                "extract-1",
                {"url": "https://example.test/evidence"},
            )
            yield completed("web_extract", "extract-1")
            yield {
                "type": "delta",
                "content": (
                    "Substantive evidence extracted through the ordinary "
                    "static capability. " * 20
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/static-worker.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "perform one bounded static evidence check",
                    "skill_name": "generic-skill",
                    "worker_id": "worker-a",
                    "step_type": "knowledge_bootstrap",
                    "tools": [
                        "submit_knowledge_gate_decisions",
                        "web_search",
                        "web_extract",
                    ],
                    "required_capability_tools": ["web_extract"],
                    "unconditional_capability_plan": static_plan,
                    "unconditional_capability_plan_sha256": _digest(
                        static_plan
                    ),
                    "knowledge_gate_plan": gate_plan,
                    "knowledge_gate_plan_sha256": _digest(gate_plan),
                },
                _context("web_search", "web_extract"),
                0,
            )

        self.assertEqual("completed", result["status"], result.get("error"))
        self.assertIn(
            "web_extract",
            result["capability_receipt_audit"][
                "successful_tool_names"
            ],
        )
        self.assertEqual(
            [],
            result["knowledge_gate_receipt_audit"]["activated_group_ids"],
        )

    async def test_decision_control_is_not_exposed_without_a_valid_plan(self):
        async def fake_run_stream(*args, **kwargs):
            self.assertEqual(["web_search"], args[2])
            self.assertNotIn(
                "submit_knowledge_gate_decisions",
                args[2],
            )
            self.assertNotIn(
                "knowledge_gate_candidate_authority",
                {
                    key: value
                    for key, value in kwargs.items()
                    if value is not None
                },
            )
            yield {
                "type": "delta",
                "content": "A bounded ordinary delegated result. " * 20,
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/ordinary-worker.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "perform one ordinary bounded check",
                    "step_type": "utility",
                },
                _context(
                    "web_search",
                    "submit_knowledge_gate_decisions",
                ),
                0,
            )
        self.assertEqual("completed", result["status"])

    async def test_forged_plan_fails_before_model_execution(self):
        plan = _native_plan()
        with (
            patch("agent_loop.run_stream") as run_stream,
            patch("tools.delegation.persist_result_for_history") as persist,
        ):
            result = await _run_child(
                {
                    "goal": "perform one bounded evidence check",
                    "skill_name": "generic-skill",
                    "worker_id": "worker-a",
                    "step_type": "knowledge_bootstrap",
                    "tools": [
                        "submit_knowledge_gate_decisions",
                        "web_search",
                    ],
                    "knowledge_gate_plan": plan,
                    "knowledge_gate_plan_sha256": "0" * 64,
                },
                _context(
                    "submit_knowledge_gate_decisions",
                    "web_search",
                ),
                0,
            )
        self.assertEqual("error", result["status"])
        self.assertIn("does not match", result["error"])
        run_stream.assert_not_called()
        persist.assert_not_called()

    async def test_successful_decision_and_candidate_dispatch_complete(self):
        plan = _native_plan()
        forwarded_events: list[dict] = []

        async def event_sink(event: dict) -> None:
            forwarded_events.append(event)

        def started(name: str, call_id: str, args: dict) -> dict:
            return {
                "type": "agent_event",
                "event_type": "tool.started",
                "tool_name": name,
                "tool_call_id": call_id,
                "payload": {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "args_compacted": args,
                },
            }

        def completed(name: str, call_id: str) -> dict:
            return {
                "type": "agent_event",
                "event_type": "tool.completed",
                "tool_name": name,
                "tool_call_id": call_id,
                "payload": {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "outcome": "success",
                    "actual_dispatch_attempted": True,
                    "exact_capability_receipt": {
                        "result_data": {},
                    },
                },
            }

        async def fake_run_stream(*args, **kwargs):
            self.assertEqual(plan, kwargs["knowledge_gate_plan"])
            self.assertEqual(
                _digest(plan),
                kwargs["knowledge_gate_plan_sha256"],
            )
            self.assertEqual(
                ["submit_knowledge_gate_decisions"],
                args[2],
            )
            self.assertEqual([], kwargs["allowed_skill_http_prefixes"])
            self.assertEqual(
                ["web_search"],
                kwargs["knowledge_gate_candidate_authority"]["tool_names"],
            )
            decision_args = _decision_call(plan)["args"]
            yield started(
                "submit_knowledge_gate_decisions",
                "decision-1",
                decision_args,
            )
            yield completed(
                "submit_knowledge_gate_decisions",
                "decision-1",
            )
            yield started(
                "web_search",
                "search-1",
                {"query": "bounded evidence"},
            )
            yield completed("web_search", "search-1")
            yield {
                "type": "delta",
                "content": (
                    "Substantive bounded evidence result with findings and "
                    "verification. " * 20
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/gate-worker.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "perform one bounded evidence check",
                    "skill_name": "generic-skill",
                    "worker_id": "worker-a",
                    "step_type": "knowledge_bootstrap",
                    "tools": [
                        "submit_knowledge_gate_decisions",
                        "web_search",
                    ],
                    "knowledge_gate_plan": plan,
                    "knowledge_gate_plan_sha256": _digest(plan),
                },
                replace(
                    _context("web_search"),
                    event_sink=event_sink,
                ),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(
            ["gate-group-1"],
            result["knowledge_gate_receipt_audit"][
                "successful_group_ids"
            ],
        )
        self.assertEqual(
            [],
            result["knowledge_gate_receipt_audit"]["gap_ids"],
        )
        final_audit = next(
            event
            for event in forwarded_events
            if event.get("event_type")
            == "debug.knowledge_gate.final_audit"
        )
        self.assertTrue(final_audit["payload"]["audit_valid"])
        self.assertEqual(
            ["gate-group-1"],
            final_audit["payload"]["successful_group_ids"],
        )
        self.assertEqual([], final_audit["payload"]["gap_ids"])

    async def test_handler_receipt_accounts_for_unactivated_gate_check_id(self):
        """A valid typed decision must not depend on model ID repetition."""

        plan = json.loads(json.dumps(_native_plan()))
        plan["checks"][0]["branches"][0]["outcome"] = "no"
        plan["groups"][0]["outcome"] = "no"
        plan_sha256 = _digest(plan)
        decision_args = {
            "plan_sha256": plan_sha256,
            "decisions": [{
                "check_id": "KG-1",
                "outcome": "yes",
                "reason": "The preloaded source is sufficient.",
            }],
        }
        accepted = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=plan_sha256,
            supplied_sha256=plan_sha256,
            decisions=decision_args["decisions"],
        )
        decision_receipt = build_knowledge_gate_decision_receipt(accepted)

        async def fake_run_stream(*args, **kwargs):
            self.assertEqual(plan, kwargs["knowledge_gate_plan"])
            yield {
                "type": "agent_event",
                "event_type": "tool.started",
                "tool_name": "submit_knowledge_gate_decisions",
                "tool_call_id": "decision-1",
                "payload": {
                    "tool_name": "submit_knowledge_gate_decisions",
                    "tool_call_id": "decision-1",
                    "args_compacted": decision_args,
                },
            }
            yield {
                "type": "agent_event",
                "event_type": "tool.completed",
                "tool_name": "submit_knowledge_gate_decisions",
                "tool_call_id": "decision-1",
                "payload": {
                    "tool_name": "submit_knowledge_gate_decisions",
                    "tool_call_id": "decision-1",
                    "outcome": "success",
                    "actual_dispatch_attempted": True,
                    "exact_capability_receipt": {
                        "result_data": {},
                        "knowledge_gate_decision_receipt": decision_receipt,
                    },
                },
            }
            # The model deliberately omits KG-1. The exact handler receipt is
            # the authority for that already-decided control-plane state.
            yield {"type": "delta", "content": '{"finding":"bounded"}'}
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/gate-worker.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "perform one bounded evidence check",
                    "skill_name": "generic-skill",
                    "worker_id": "worker-a",
                    "step_type": "knowledge_bootstrap",
                    "tools": [
                        "submit_knowledge_gate_decisions",
                        "web_search",
                    ],
                    "required_output_ids": ["KG-1"],
                    "knowledge_gate_plan": plan,
                    "knowledge_gate_plan_sha256": plan_sha256,
                },
                _context(
                    "submit_knowledge_gate_decisions",
                    "web_search",
                ),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertIn("KNOWLEDGE_GATE_CHECKS_JSON", result["summary"])
        self.assertIn('"id":"KG-1"', result["summary"])
        self.assertIn('"status":"pass"', result["summary"])
        self.assertEqual(
            [],
            result["knowledge_gate_receipt_audit"]["activated_group_ids"],
        )

    async def test_decision_receipt_survives_secret_free_dispatch_projection(
        self,
    ):
        """Final audit must not reconstruct control state from debug args."""

        plan = _native_plan()
        plan_sha256 = _digest(plan)
        accepted = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=plan_sha256,
            supplied_sha256=plan_sha256,
            decisions=_decision_call(plan)["args"]["decisions"],
        )
        receipt_core = {
            "schema_version": 1,
            "plan_sha256": plan_sha256,
            "decision_outcomes": [
                {
                    "check_id": row["check_id"],
                    "outcome": row["outcome"],
                }
                for row in accepted["decisions"]
            ],
            "activated_group_ids": accepted["activated_group_ids"],
            "unresolved_group_ids": accepted["unresolved_group_ids"],
            "unknown_check_ids": accepted["unknown_check_ids"],
        }
        decision_receipt = {
            **receipt_core,
            "receipt_sha256": _digest(receipt_core),
        }

        def started(name: str, call_id: str) -> dict:
            return {
                "type": "agent_event",
                "event_type": "tool.started",
                "tool_name": name,
                "tool_call_id": call_id,
                "payload": {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "args_compacted": {
                        "_chatds_arguments_redacted": True,
                    },
                    "args_are_dispatch_payload": False,
                    "preflight_pending": True,
                },
            }

        def dispatch_started(
            name: str,
            call_id: str,
            audit_args: dict,
        ) -> dict:
            return {
                "type": "agent_event",
                "event_type": "tool.dispatch_started",
                "tool_name": name,
                "tool_call_id": call_id,
                "payload": {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "actual_dispatch_attempted": True,
                    "audit_args": audit_args,
                    "audit_args_are_dispatch_derived": True,
                },
            }

        def completed(
            name: str,
            call_id: str,
            *,
            typed_decision_receipt: dict | None = None,
        ) -> dict:
            exact_receipt = {"result_data": {}}
            if typed_decision_receipt is not None:
                exact_receipt["knowledge_gate_decision_receipt"] = (
                    typed_decision_receipt
                )
            return {
                "type": "agent_event",
                "event_type": "tool.completed",
                "tool_name": name,
                "tool_call_id": call_id,
                "payload": {
                    "tool_name": name,
                    "tool_call_id": call_id,
                    "outcome": "success",
                    "actual_dispatch_attempted": True,
                    "exact_capability_receipt": exact_receipt,
                },
            }

        async def fake_run_stream(*args, **kwargs):
            self.assertEqual(plan, kwargs["knowledge_gate_plan"])
            yield started(
                "submit_knowledge_gate_decisions",
                "decision-secret-free",
            )
            yield dispatch_started(
                "submit_knowledge_gate_decisions",
                "decision-secret-free",
                {},
            )
            yield completed(
                "submit_knowledge_gate_decisions",
                "decision-secret-free",
                typed_decision_receipt=decision_receipt,
            )
            yield started("web_search", "search-secret-free")
            yield dispatch_started(
                "web_search",
                "search-secret-free",
                {},
            )
            yield completed("web_search", "search-secret-free")
            yield {
                "type": "delta",
                "content": (
                    "Substantive bounded evidence result with findings and "
                    "verification. " * 20
                ),
            }
            yield {"type": "done", "finish_reason": "stop"}

        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/gate-secret-free.txt",
            ),
        ):
            result = await _run_child(
                {
                    "goal": "perform one bounded evidence check",
                    "skill_name": "generic-skill",
                    "worker_id": "worker-a",
                    "step_type": "knowledge_bootstrap",
                    "tools": [
                        "submit_knowledge_gate_decisions",
                        "web_search",
                    ],
                    "knowledge_gate_plan": plan,
                    "knowledge_gate_plan_sha256": plan_sha256,
                },
                _context("web_search"),
                0,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(
            ["gate-group-1"],
            result["knowledge_gate_receipt_audit"][
                "successful_group_ids"
            ],
        )
