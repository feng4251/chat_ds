import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import workspace_context
from agent_loop import (
    HarnessRunState,
    _apply_intent_selections_to_plan,
    _build_skill_execution_plan,
    _declared_worker_dependencies,
    _has_modular_artifacts_for_contract,
    _intent_dimension_ids,
    _intent_selected_mappings,
    _markdown_quality_findings,
    _next_incomplete_aggregation_step,
    _parse_intent_selections,
    _plan_requires_intent,
    _prerequisite_result_paths,
)
from tools.context import ToolContext
from tools.delegation import _run_child


def _generic_contract() -> dict:
    execution = {
        "workers": [
            {"id": "research", "file": "workers/research.yaml"},
            {"id": "analysis", "file": "workers/analysis.yaml"},
            {"id": "review", "file": "workers/review.yaml"},
        ],
        "routes": [
            {
                "id": "full",
                "patterns": ["comprehensive.*assessment"],
                "priority": 10,
                "requires_full_output": True,
                "waves": [
                    {
                        "id": "parallel",
                        "mode": "parallel",
                        "workers": ["research", "analysis"],
                        "dependencies": [],
                    },
                    {
                        "id": "review",
                        "mode": "sequential",
                        "workers": ["review"],
                        "dependencies": ["parallel"],
                    },
                ],
            }
        ],
        "knowledge_bootstrap": {
            "sources": [
                {
                    "id": "catalog",
                    "tool": "search",
                    "query_strategy": "find authoritative sources",
                }
            ]
        },
        "quality_contract": {
            "required_module_markers": ["**Summary**", "**Source**"],
            "constraints": {"max_lines_per_file": 20},
        },
        "diagnostics": {"errors": [], "warnings": []},
    }
    output = {
        "declared_modular_files": ["01_findings.md", "02_review.md"],
        "declared_ancillary_files": ["README.md", "_checklist.md"],
        "declared_final_artifact": "{NAME}_FULL.md",
        "declared_file_count": 5,
        "declared_section_count": 2,
        "expected_min_bytes": 10,
        "expected_min_lines": 4,
        "post_merge_checks": [
            "First line matches 01_findings.md first line",
            "Last line matches 02_review.md last line",
        ],
    }
    execution["output_contract"] = output
    return {
        "orchestrator_files": ["orchestration/orchestrator.yaml"],
        "worker_files": [
            "workers/research.yaml",
            "workers/analysis.yaml",
            "workers/review.yaml",
        ],
        "format_files": ["formats/output.md"],
        "workers": execution["workers"],
        "execution_contract": execution,
        "output_contract": output,
        "requires_worker_outputs": True,
        "requires_modular_artifacts": True,
        "requires_merge": True,
    }


class SkillWorkflowRuntimeTests(unittest.TestCase):
    @staticmethod
    def _workflow_ir_state() -> HarnessRunState:
        state = HarnessRunState(
            available_tools={"delegate_task"},
            original_user_text="Produce the required report",
        )
        state.skill_execution_plans["generic"] = {
            "selection": "workflow_ir",
            "required_workers": ["research"],
            "workers": {
                "research": {
                    "id": "research",
                    "name": "Research",
                    "dependencies": [],
                },
            },
            "waves": [{
                "id": "ir-wave-001",
                "mode": "sequential",
                "workers": ["research"],
                "dependencies": [],
            }],
            "bootstrap_sources": [],
            "aggregation_steps": [{
                "id": "synthesize",
                "required": True,
                "depends_on": [],
                "input_worker_ids": ["research"],
            }],
        }
        return state

    def _seven_source_state(self) -> HarnessRunState:
        contract = _generic_contract()
        contract["execution_contract"]["knowledge_bootstrap"]["sources"] = [
            {"id": f"source-{index}", "tool": "search"}
            for index in range(1, 8)
        ]
        state = HarnessRunState(
            available_tools={"delegate_task"},
            original_user_text="Please produce a comprehensive assessment",
        )
        state.skill_execution_plans["generic"] = _build_skill_execution_plan(
            contract,
            state.original_user_text,
        )
        return state

    def test_machine_degraded_bootstrap_advances_dag_and_preserves_gap(self):
        state = self._seven_source_state()
        gap = {
            "status": "unresolved",
            "source": "harness_http_retrieval_completeness",
            "terminal_reason": "pagination_page_limit",
            "open_chain_count": 1,
            "open_frontier_count": 1,
            "total_requests": 12,
        }
        update = state.record_delegate_task(
            {
                "tasks": [{
                    "goal": "collect source 1",
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "source-1",
                }],
            },
            {
                "status": "completed_degraded",
                "results": [{
                    "index": 0,
                    "status": "completed",
                    "completion_quality": "degraded",
                    "unresolved_retrieval": gap,
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "source-1",
                    "result_path": "results/source-1.md",
                    "result_chars": 500,
                    "result_shape": {
                        "semantic_short_result_valid": True,
                    },
                }],
            },
        )

        self.assertEqual(["source-1"], update["completed_step_ids"])
        self.assertEqual(
            ["source-1"], update["degraded_completed_step_ids"]
        )
        self.assertIn(
            "source-1", state.skill_completed_bootstrap["generic"]
        )
        recorded = state.skill_bootstrap_results["generic"]["source-1"]
        self.assertEqual("degraded", recorded["completion_quality"])
        self.assertEqual(gap, recorded["unresolved_retrieval"])

    def test_failed_worker_metadata_error_is_not_masked_by_completion_audit(self):
        contract = _generic_contract()
        state = HarnessRunState(
            available_tools={"delegate_task"},
            original_user_text="Please produce a comprehensive assessment",
        )
        state.skill_execution_plans["generic"] = _build_skill_execution_plan(
            contract,
            state.original_user_text,
        )
        state.skill_completed_bootstrap["generic"] = {"catalog"}
        original_error = (
            "required_result_schema keys must exactly match "
            "required_result_fields."
        )

        state.record_delegate_task(
            {
                "tasks": [{
                    "goal": "research",
                    "skill_name": "generic",
                    "worker_id": "research",
                    "worker_file": "workers/research.yaml",
                    "workflow_stage": "parallel",
                    "step_type": "worker",
                    "step_id": "research",
                }],
            },
            {
                "results": [{
                    "index": 0,
                    "status": "error",
                    "skill_name": "generic",
                    "worker_id": "research",
                    "step_type": "worker",
                    "step_id": "research",
                    "error": original_error,
                    "terminal_reason": "delegation_contract_error",
                    "failure_class": "contract_validation",
                    "retryable": False,
                }],
            },
        )

        recorded = state.skill_worker_results["generic"]["research"]
        self.assertEqual(original_error, recorded["error"])
        self.assertEqual("delegation_contract_error", recorded["terminal_reason"])
        self.assertIsNone(recorded["child_run_id"])

    @staticmethod
    def _failed_bootstrap_result(
        step_id: str,
        *,
        retryable: bool,
        terminal_reason: str,
        failure_class: str,
    ) -> dict:
        return {
            "status": "error",
            "skill_name": "generic",
            "step_type": "knowledge_bootstrap",
            "step_id": step_id,
            "error": f"{failure_class}: {step_id}",
            "terminal_reason": terminal_reason,
            "failure_class": failure_class,
            "retryable": retryable,
        }

    def test_delegate_all_error_envelope_records_children_and_terminal_failure(self):
        state = self._seven_source_state()
        task_ids = ["source-1", "source-2"]
        update = state.record_delegate_task(
            {"tasks": [
                {
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": step_id,
                }
                for step_id in task_ids
            ]},
            {
                "status": "error",
                "task_count": 2,
                "results": [
                    self._failed_bootstrap_result(
                        step_id,
                        retryable=False,
                        terminal_reason="provider_tool_stream_corrupt",
                        failure_class="provider_protocol",
                    )
                    for step_id in task_ids
                ],
            },
        )

        self.assertEqual(update["terminal_failed_step_ids"], task_ids)
        self.assertEqual(
            set(state.skill_failed_bootstrap["generic"]),
            set(task_ids),
        )
        terminal = state.terminal_delegate_failure()
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal["failure_class"], "provider_protocol")
        self.assertEqual(terminal["attempts"], 1)

    def test_delegate_candidates_run_unattempted_seventh_before_retries(self):
        state = self._seven_source_state()
        first_batch = [f"source-{index}" for index in range(1, 7)]
        state.record_delegate_task(
            {"tasks": [
                {
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": step_id,
                }
                for step_id in first_batch
            ]},
            {"status": "error", "results": [
                self._failed_bootstrap_result(
                    step_id,
                    retryable=True,
                    terminal_reason="provider_timeout",
                    failure_class="transient_external",
                )
                for step_id in first_batch
            ]},
        )

        candidates = state.ordered_delegate_candidates(
            "generic",
            "bootstrap",
            [f"source-{index}" for index in range(1, 8)],
        )
        self.assertEqual(candidates[0], "source-7")
        self.assertEqual(candidates[1:], first_batch)
        self.assertIsNone(state.terminal_delegate_failure())

    def test_parent_accepts_short_result_with_child_semantic_shape_receipt(self):
        state = self._seven_source_state()
        update = state.record_delegate_task(
            {
                "goal": "return one structured source status",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "source-1",
            },
            {
                "status": "completed",
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "source-1",
                    "result_path": "results/source-1.json",
                    "result_chars": 26,
                    "result_shape": {
                        "semantic_short_result_valid": True,
                        "structured_value": True,
                    },
                }],
            },
        )

        self.assertEqual(["source-1"], update["completed_step_ids"])
        self.assertEqual([], update["terminal_failed_step_ids"])
        self.assertIn("source-1", state.skill_completed_bootstrap["generic"])

    def test_transient_delegate_failure_becomes_terminal_after_one_parent_retry(self):
        state = self._seven_source_state()
        args = {"tasks": [{
            "skill_name": "generic",
            "step_type": "knowledge_bootstrap",
            "step_id": "source-1",
        }]}
        envelope = {"status": "error", "results": [
            self._failed_bootstrap_result(
                "source-1",
                retryable=True,
                terminal_reason="provider_timeout",
                failure_class="transient_external",
            )
        ]}

        first = state.record_delegate_task(args, envelope)
        second = state.record_delegate_task(args, envelope)

        self.assertEqual(first["retryable_failed_step_ids"], ["source-1"])
        self.assertEqual(second["terminal_failed_step_ids"], ["source-1"])
        terminal = state.terminal_delegate_failure()
        self.assertEqual(terminal["step_id"], "source-1")
        self.assertEqual(terminal["attempts"], 2)

    def test_workflow_ir_degraded_required_worker_retries_then_fails_closed(self):
        state = self._workflow_ir_state()
        args = {"tasks": [{
            "skill_name": "generic",
            "worker_id": "research",
            "step_type": "worker",
            "step_id": "research",
            "workflow_stage": "ir-wave-001",
        }]}
        envelope = {
            "status": "completed_degraded",
            "results": [{
                "status": "completed",
                "completion_quality": "degraded",
                "skill_name": "generic",
                "worker_id": "research",
                "step_type": "worker",
                "step_id": "research",
                "workflow_stage": "ir-wave-001",
                "result_path": "results/research.md",
                "result_chars": 500,
                "dispatch_receipt_audit": {
                    "mutating_dispatch_count": 0,
                },
            }],
        }

        first = state.record_delegate_task(args, envelope)

        self.assertEqual([], first["completed_step_ids"])
        self.assertEqual([], first["degraded_completed_step_ids"])
        self.assertEqual(["research"], first["retryable_failed_step_ids"])
        self.assertNotIn(
            "research",
            state.skill_completed_workers.get("generic", set()),
        )
        self.assertEqual(
            ["research"],
            state.ordered_delegate_candidates(
                "generic",
                "worker",
                ["research"],
            ),
        )
        self.assertIsNone(state.terminal_delegate_failure())

        second = state.record_delegate_task(args, envelope)

        self.assertEqual([], second["completed_step_ids"])
        self.assertEqual([], second["retryable_failed_step_ids"])
        self.assertEqual(["research"], second["terminal_failed_step_ids"])
        self.assertEqual(
            [],
            state.ordered_delegate_candidates(
                "generic",
                "worker",
                ["research"],
            ),
        )
        terminal = state.terminal_delegate_failure()
        self.assertIsNotNone(terminal)
        self.assertEqual("research", terminal["step_id"])
        self.assertEqual(2, terminal["attempts"])
        self.assertTrue(terminal["retryable"])
        self.assertEqual(
            "workflow_ir_required_step_degraded",
            terminal["terminal_reason"],
        )
        self.assertEqual("completion_quality", terminal["failure_class"])
        self.assertEqual(
            "required Workflow IR worker 'research' returned "
            "completion_quality=degraded; a complete result is mandatory",
            terminal["error"],
        )

    def test_workflow_ir_degraded_required_aggregation_is_retryable(self):
        state = self._workflow_ir_state()
        state.skill_completed_workers["generic"] = {"research"}
        state.skill_worker_results["generic"] = {
            "research": {
                "status": "completed",
                "completion_quality": "complete",
                "result_path": "results/research.md",
            },
        }
        args = {
            "skill_name": "generic",
            "step_type": "aggregation",
            "step_id": "synthesize",
            "workflow_stage": "aggregation",
            "required_result_paths": ["results/research.md"],
        }
        envelope = {
            "status": "completed_degraded",
            "results": [{
                "status": "completed",
                "completion_quality": "degraded",
                "skill_name": "generic",
                "step_type": "aggregation",
                "step_id": "synthesize",
                "workflow_stage": "aggregation",
                "result_path": "results/synthesize.md",
                "result_chars": 500,
                "required_result_paths": ["results/research.md"],
                "tool_audit": {
                    "read_result_paths": ["results/research.md"],
                },
                "dispatch_receipt_audit": {
                    "mutating_dispatch_count": 0,
                },
            }],
        }

        update = state.record_delegate_task(args, envelope)

        self.assertEqual([], update["completed_step_ids"])
        self.assertEqual([], update["degraded_completed_step_ids"])
        self.assertEqual(["synthesize"], update["retryable_failed_step_ids"])
        self.assertNotIn(
            "synthesize",
            state.skill_completed_aggregation.get("generic", set()),
        )
        self.assertEqual(
            ["synthesize"],
            state.ordered_delegate_candidates(
                "generic",
                "aggregation",
                ["synthesize"],
            ),
        )
        self.assertIsNone(state.terminal_delegate_failure())
        recorded = state.skill_aggregation_results["generic"]["synthesize"]
        self.assertEqual("error", recorded["status"])
        self.assertEqual("degraded", recorded["completion_quality"])
        self.assertTrue(recorded["retryable"])

    def test_workflow_ir_degraded_required_worker_with_mutating_dispatch_is_terminal(self):
        state = self._workflow_ir_state()
        args = {"tasks": [{
            "skill_name": "generic",
            "worker_id": "research",
            "step_type": "worker",
            "step_id": "research",
            "workflow_stage": "ir-wave-001",
        }]}
        envelope = {
            "status": "completed_degraded",
            "results": [{
                "status": "completed",
                "completion_quality": "degraded",
                "skill_name": "generic",
                "worker_id": "research",
                "step_type": "worker",
                "step_id": "research",
                "workflow_stage": "ir-wave-001",
                "result_path": "results/research.md",
                "result_chars": 500,
                "dispatch_receipt_audit": {
                    "mutating_dispatch_count": 1,
                    "mutating_tool_names": ["write_file"],
                },
            }],
        }

        update = state.record_delegate_task(args, envelope)

        self.assertEqual([], update["retryable_failed_step_ids"])
        self.assertEqual(["research"], update["terminal_failed_step_ids"])
        terminal = state.terminal_delegate_failure()
        self.assertIsNotNone(terminal)
        self.assertFalse(terminal["retryable"])
        self.assertEqual(
            "side_effect_state_uncertain",
            terminal["failure_class"],
        )
        self.assertEqual(
            "workflow_ir_required_step_degraded",
            terminal["terminal_reason"],
        )

    def test_workflow_ir_degraded_required_worker_without_dispatch_audit_is_terminal(self):
        state = self._workflow_ir_state()
        args = {"tasks": [{
            "skill_name": "generic",
            "worker_id": "research",
            "step_type": "worker",
            "step_id": "research",
            "workflow_stage": "ir-wave-001",
        }]}
        envelope = {
            "status": "completed_degraded",
            "results": [{
                "status": "completed",
                "completion_quality": "degraded",
                "skill_name": "generic",
                "worker_id": "research",
                "step_type": "worker",
                "step_id": "research",
                "workflow_stage": "ir-wave-001",
                "result_path": "results/research.md",
                "result_chars": 500,
            }],
        }

        update = state.record_delegate_task(args, envelope)

        self.assertEqual([], update["retryable_failed_step_ids"])
        self.assertEqual(["research"], update["terminal_failed_step_ids"])
        terminal = state.terminal_delegate_failure()
        self.assertIsNotNone(terminal)
        self.assertFalse(terminal["retryable"])
        self.assertEqual(
            "side_effect_state_uncertain",
            terminal["failure_class"],
        )

    def test_non_workflow_ir_degraded_worker_remains_completed(self):
        state = self._workflow_ir_state()
        state.skill_execution_plans["generic"]["selection"] = "full"

        update = state.record_delegate_task(
            {"tasks": [{
                "skill_name": "generic",
                "worker_id": "research",
                "step_type": "worker",
                "step_id": "research",
                "workflow_stage": "ir-wave-001",
            }]},
            {
                "status": "completed_degraded",
                "results": [{
                    "status": "completed",
                    "completion_quality": "degraded",
                    "skill_name": "generic",
                    "worker_id": "research",
                    "step_type": "worker",
                    "step_id": "research",
                    "workflow_stage": "ir-wave-001",
                    "result_path": "results/research.md",
                    "result_chars": 500,
                }],
            },
        )

        self.assertEqual(["research"], update["completed_step_ids"])
        self.assertEqual(
            ["research"],
            update["degraded_completed_step_ids"],
        )
        self.assertIn(
            "research",
            state.skill_completed_workers.get("generic", set()),
        )
        self.assertIsNone(state.terminal_delegate_failure())

    def test_artifact_synthesis_is_recorded_as_terminal_phase_not_worker(self):
        contract = _generic_contract()
        state = HarnessRunState(
            original_user_text="Please produce a comprehensive assessment",
        )
        state.skill_workflow_contracts["generic"] = contract
        state.skill_execution_plans["generic"] = _build_skill_execution_plan(
            contract,
            state.original_user_text,
        )
        state.skill_completed_bootstrap["generic"] = {"catalog"}
        state.skill_bootstrap_results["generic"] = {
            "catalog": {
                "status": "completed",
                "result_path": "results/catalog.txt",
            },
        }
        worker_paths = []
        for worker_id in ("research", "analysis", "review"):
            result_path = f"results/{worker_id}.txt"
            worker_paths.append(result_path)
            state.skill_completed_workers.setdefault("generic", set()).add(
                worker_id
            )
            state.skill_worker_results.setdefault("generic", {})[worker_id] = {
                "status": "completed",
                "result_path": result_path,
            }
        prerequisite_paths = ["results/catalog.txt", *worker_paths]

        update = state.record_delegate_task(
            {
                "goal": "write the declared modular package",
                "skill_name": "generic",
                "step_type": "artifact_synthesis",
                "step_id": "modular-package",
                "workflow_stage": "artifact-synthesis",
                "required_result_paths": prerequisite_paths,
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "step_type": "artifact_synthesis",
                    "step_id": "modular-package",
                    "result_path": "results/modular-package.txt",
                    "result_chars": 500,
                    "required_result_paths": prerequisite_paths,
                    "tool_audit": {
                        "read_result_paths": prerequisite_paths,
                    },
                }],
            },
        )

        self.assertEqual(update["completed_step_ids"], ["modular-package"])
        self.assertEqual(update["terminal_failed_step_ids"], [])
        self.assertIsNone(state.terminal_delegate_failure())
        self.assertEqual(
            state.delegate_step_status[
                ("generic", "artifact_synthesis", "modular-package")
            ]["status"],
            "completed",
        )
        self.assertNotIn(
            "modular-package",
            state.skill_worker_results.get("generic", {}),
        )
        self.assertNotIn(
            "modular-package",
            state.skill_failed_workers.get("generic", {}),
        )

    def test_intent_gate_precedes_worker_and_format_resource_gates(self):
        contract = _generic_contract()
        contract["execution_contract"]["intent_classification"] = {
            "dimensions": [{
                "id": "task_kind",
                "required": True,
                "values": ["comprehensive"],
                "source_file": "orchestration/orchestrator.yaml",
                "mappings": {
                    "workers_map": {
                        "comprehensive": ["research", "analysis", "review"],
                    }
                },
            }]
        }
        state = HarnessRunState(
            available_tools={"skill_view", "delegate_task"},
            original_user_text="Please produce a comprehensive risk assessment",
        )
        state.session_skill_names.add("generic")
        state.record_skill_view(
            {"name": "generic"},
            {
                "linked_files": {
                    "orchestration": ["orchestration/orchestrator.yaml"],
                    "workers": contract["worker_files"],
                    "formats": contract["format_files"],
                },
                "resource_graph": {
                    "categories": {
                        "orchestration": {
                            "sample": ["orchestration/orchestrator.yaml"],
                        },
                        "workers": {"sample": contract["worker_files"]},
                        "formats": {"sample": contract["format_files"]},
                    }
                },
                "workflow_contract": contract,
            },
        )
        state.record_skill_view(
            {"name": "generic", "file_path": "orchestration/orchestrator.yaml"},
            {},
        )

        needed, reason = state.needs_more_skill_workflow()

        self.assertTrue(needed)
        self.assertIn("delegate intent classification", reason)
        self.assertNotIn("worker", reason)
        self.assertNotIn("format", reason)
        self.assertNotIn(
            "workers/research.yaml",
            state.viewed_skill_files["generic"],
        )
        self.assertNotIn("formats/output.md", state.viewed_skill_files["generic"])

    def test_optional_nullable_intent_dimension_is_visible_and_may_be_omitted(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"][0]["patterns"] = ["will-not-match"]
        contract["execution_contract"]["intent_classification"] = {
            "dimensions": [
                {
                    "id": "task_kind",
                    "required": True,
                    "values": ["lookup"],
                    "mappings": {
                        "workers_map": {"lookup": ["research"]},
                    },
                },
                {
                    "id": "style",
                    "required": False,
                    "nullable": True,
                    "values": ["compact", "detailed"],
                    "mappings": {
                        "resource_map": {
                            "compact": "references/compact.md",
                            "detailed": "references/detailed.md",
                        }
                    },
                },
            ]
        }
        plan = _build_skill_execution_plan(contract, "Investigate one item")

        self.assertEqual(_intent_dimension_ids(plan), ["task_kind", "style"])
        selections, error = _parse_intent_selections(
            plan,
            {"intent_selections": {"task_kind": "lookup"}},
        )
        self.assertIsNone(error)
        self.assertEqual(selections, {"task_kind": "lookup"})
        bold_selections, bold_error = _parse_intent_selections(
            plan,
            {
                "summary": (
                    '**INTENT_SELECTIONS_JSON:** '
                    '{"task_kind":"lookup","style":"compact"}'
                )
            },
        )
        self.assertIsNone(bold_error)
        self.assertEqual(
            bold_selections,
            {"task_kind": "lookup", "style": "compact"},
        )
        self.assertEqual(
            _intent_selected_mappings(
                plan,
                {"task_kind": "lookup", "style": "compact"},
            )["style.resource_map"],
            "references/compact.md",
        )

    def test_intent_workers_map_exact_match_preserves_declared_route_waves(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"][0]["patterns"] = ["will-not-match"]
        contract["execution_contract"]["intent_classification"] = {
            "dimensions": [{
                "id": "task_kind",
                "required": True,
                "values": ["comprehensive"],
                "mappings": {
                    "workers_map": {
                        "comprehensive": ["research", "analysis", "review"],
                    }
                },
            }]
        }
        plan = _build_skill_execution_plan(contract, "Run the declared workflow")
        selections = {"task_kind": "comprehensive"}

        _apply_intent_selections_to_plan(
            plan,
            selections,
            _intent_selected_mappings(plan, selections),
        )

        self.assertEqual(plan["selection"], "intent_route_mapped")
        self.assertEqual(plan["route_id"], "full")
        self.assertEqual(plan["waves"][0]["workers"], ["research", "analysis"])
        self.assertEqual(plan["waves"][1]["workers"], ["review"])
        self.assertEqual(plan["waves"][1]["dependencies"], ["parallel"])
        self.assertTrue(plan["requires_full_output"])

    def test_intent_workers_map_projects_unique_minimal_superset_route_waves(self):
        contract = _generic_contract()
        execution = contract["execution_contract"]
        execution["routes"][0]["patterns"] = ["will-not-match"]
        execution["workers"].append({"id": "audit", "file": "workers/audit.yaml"})
        contract["worker_files"].append("workers/audit.yaml")
        execution["routes"].append({
            "id": "larger",
            "patterns": ["also-will-not-match"],
            "requires_full_output": True,
            "waves": [{
                "id": "all",
                "mode": "parallel",
                "workers": ["research", "analysis", "review", "audit"],
                "dependencies": [],
            }],
        })
        execution["intent_classification"] = {
            "dimensions": [{
                "id": "task_kind",
                "required": True,
                "values": ["targeted"],
                "mappings": {
                    "workers_map": {
                        "targeted": ["research", "review"],
                    }
                },
            }]
        }
        plan = _build_skill_execution_plan(contract, "Run a targeted workflow")
        selections = {"task_kind": "targeted"}

        _apply_intent_selections_to_plan(
            plan,
            selections,
            _intent_selected_mappings(plan, selections),
        )

        self.assertEqual(plan["route_id"], "full")
        self.assertEqual(
            [wave["workers"] for wave in plan["waves"]],
            [["research"], ["review"]],
        )
        self.assertEqual(plan["waves"][1]["dependencies"], ["parallel"])
        self.assertFalse(plan["requires_full_output"])

    def test_invalid_contract_never_builds_a_dispatchable_plan(self):
        contract = _generic_contract()
        contract["execution_contract"]["diagnostics"]["errors"] = [{
            "code": "worker_dependency_cycle",
            "message": "The worker graph contains a cycle.",
        }]
        plan = _build_skill_execution_plan(
            contract,
            "Please produce a comprehensive risk assessment",
        )

        self.assertEqual(plan["selection"], "invalid_contract")
        self.assertEqual(plan["required_workers"], [])
        self.assertEqual(plan["waves"], [])
        self.assertEqual(plan["bootstrap_sources"], [])
        self.assertFalse(plan["requires_full_output"])

        state = HarnessRunState(
            available_tools={"skill_view", "delegate_task"},
            original_user_text="Please produce a comprehensive risk assessment",
        )
        state.session_skill_names.add("generic")
        state.record_skill_view(
            {"name": "generic"},
            {
                "linked_files": {
                    "orchestration": ["orchestration/orchestrator.yaml"],
                    "workers": contract["worker_files"],
                    "formats": contract["format_files"],
                },
                "resource_graph": {
                    "categories": {
                        "orchestration": {
                            "sample": ["orchestration/orchestrator.yaml"],
                        },
                        "workers": {"sample": contract["worker_files"]},
                        "formats": {"sample": contract["format_files"]},
                    }
                },
                "workflow_contract": contract,
            },
        )
        needed, reason = state.needs_more_skill_workflow()
        self.assertFalse(needed)
        self.assertNotIn("delegate", reason)

    def test_generic_complete_report_wording_does_not_infer_a_full_route(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"][0]["patterns"] = [
            "wording-that-will-not-match"
        ]

        plan = _build_skill_execution_plan(
            contract,
            "Produce a complete end-to-end report package",
        )

        self.assertEqual(plan["selection"], "unmatched")
        self.assertIsNone(plan["route_id"])
        self.assertEqual(plan["required_workers"], [])
        self.assertFalse(plan["requires_full_output"])

    def test_parallel_tail_prerequisites_exclude_same_wave_sibling_paths(self):
        parallel_workers = [f"worker-{index}" for index in range(1, 8)]
        plan = {
            "workers": {
                worker_id: {"id": worker_id}
                for worker_id in parallel_workers + ["review"]
            },
            "waves": [
                {
                    "id": "parallel",
                    "mode": "parallel",
                    "workers": parallel_workers,
                    "dependencies": [],
                },
                {
                    "id": "review",
                    "mode": "sequential",
                    "workers": ["review"],
                    "dependencies": ["parallel"],
                },
            ],
        }
        completed_results = {
            worker_id: {"result_path": f"results/{worker_id}.txt"}
            for worker_id in parallel_workers[:6]
        }
        shared_paths = ["results/intent.txt", "results/bootstrap.txt"]
        tail_dependencies = _declared_worker_dependencies(
            plan,
            plan["waves"][0],
            parallel_workers[-1],
        )
        tail_paths = shared_paths + [
            completed_results[worker_id]["result_path"]
            for worker_id in tail_dependencies
            if worker_id in completed_results
        ]

        self.assertEqual(tail_dependencies, [])
        self.assertEqual(tail_paths, shared_paths)
        self.assertFalse(
            any(path.endswith("worker-1.txt") for path in tail_paths)
        )

        completed_results[parallel_workers[-1]] = {
            "result_path": f"results/{parallel_workers[-1]}.txt"
        }
        review_dependencies = _declared_worker_dependencies(
            plan,
            plan["waves"][1],
            "review",
        )
        review_paths = [
            completed_results[worker_id]["result_path"]
            for worker_id in review_dependencies
        ]
        self.assertEqual(review_dependencies, parallel_workers)
        self.assertEqual(len(review_paths), 7)

    def test_required_intent_dimensions_gate_bootstrap_and_persist_selections(self):
        contract = _generic_contract()
        contract["execution_contract"]["intent_classification"] = {
            "descriptions": ["Classify before dispatch."],
            "dimensions": [
                {
                    "id": "task_kind",
                    "values": ["lookup", "comprehensive"],
                    "workers_map": {
                        "lookup": ["research"],
                        "comprehensive": ["research", "analysis", "review"],
                    },
                    "mappings": {
                        "workers_map": {
                            "lookup": ["research"],
                            "comprehensive": ["research", "analysis", "review"],
                        }
                    },
                },
                {
                    "id": "knowledge_scope",
                    "values": ["internal", "external"],
                },
            ],
        }
        plan = _build_skill_execution_plan(
            contract,
            "Please produce a comprehensive risk assessment",
        )
        self.assertTrue(_plan_requires_intent(plan))
        state = HarnessRunState()
        state.skill_execution_plans["generic"] = plan

        state.record_delegate_task(
            {
                "goal": "bootstrap",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "catalog",
                "required_result_paths": ["results/intent.md"],
            },
            {"results": [{
                "status": "completed",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "catalog",
                "result_path": "results/catalog.md",
                "result_chars": 400,
                "required_result_paths": ["results/intent.md"],
                "tool_audit": {
                    "read_result_paths": ["results/intent.md"],
                },
            }]},
        )
        self.assertNotIn("catalog", state.skill_completed_bootstrap.get("generic", set()))

        summary = "\n".join([
            "task_kind — PASS: comprehensive",
            "knowledge_scope — PASS: external",
            "intent-resource-resolution — PASS: declared mappings resolved",
            'INTENT_SELECTIONS_JSON: {"task_kind":"comprehensive","knowledge_scope":"external"}',
        ])
        state.record_delegate_task(
            {
                "goal": "classify",
                "skill_name": "generic",
                "step_type": "intent_classification",
                "step_id": "intent-classification",
            },
            {"results": [{
                "status": "completed",
                "skill_name": "generic",
                "step_type": "intent_classification",
                "step_id": "intent-classification",
                "result_path": "results/intent.md",
                "result_chars": 500,
                "summary": summary,
            }]},
        )
        self.assertIn("generic", state.skill_completed_intent)
        self.assertEqual(
            state.skill_intent_selections["generic"],
            {"task_kind": "comprehensive", "knowledge_scope": "external"},
        )
        self.assertEqual(
            state.skill_intent_mappings["generic"]["task_kind.workers_map"],
            ["research", "analysis", "review"],
        )

        state.record_delegate_task(
            {
                "goal": "bootstrap",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "catalog",
                "required_result_paths": ["results/intent.md"],
            },
            {"results": [{
                "status": "completed",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "catalog",
                "result_path": "results/catalog.md",
                "result_chars": 400,
                "required_result_paths": ["results/intent.md"],
                "tool_audit": {
                    "read_result_paths": ["results/intent.md"],
                },
            }]},
        )
        self.assertIn("catalog", state.skill_completed_bootstrap["generic"])

    def test_intent_worker_mapping_resolves_an_unmatched_route_generically(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"][0]["patterns"] = ["will-not-match"]
        contract["execution_contract"]["intent_classification"] = {
            "dimensions": [{
                "id": "task_kind",
                "values": ["lookup", "comprehensive"],
                "mappings": {
                    "workers_map": {
                        "lookup": ["research"],
                        "comprehensive": ["research", "analysis", "review"],
                    }
                },
            }]
        }
        plan = _build_skill_execution_plan(contract, "Please investigate one item")
        self.assertEqual(plan["selection"], "pending_intent")
        state = HarnessRunState()
        state.skill_execution_plans["generic"] = plan
        state.record_delegate_task(
            {
                "goal": "classify",
                "skill_name": "generic",
                "step_type": "intent_classification",
                "step_id": "intent-classification",
            },
            {"results": [{
                "status": "completed",
                "skill_name": "generic",
                "step_type": "intent_classification",
                "step_id": "intent-classification",
                "result_path": "results/intent.md",
                "result_chars": 300,
                "summary": (
                    "task_kind — PASS: lookup\n"
                    "intent-resource-resolution — PASS: no mapped local files\n"
                    'INTENT_SELECTIONS_JSON: {"task_kind":"lookup"}'
                ),
            }]},
        )

        self.assertEqual(plan["selection"], "intent_route_mapped")
        self.assertEqual(plan["required_workers"], ["research"])
        self.assertEqual(plan["waves"][0]["workers"], ["research"])
        self.assertFalse(plan["requires_full_output"])

    def test_intent_descriptions_without_dimensions_do_not_create_a_gate(self):
        contract = _generic_contract()
        contract["execution_contract"]["intent_classification"] = {
            "descriptions": ["Documentation only"]
        }
        plan = _build_skill_execution_plan(
            contract,
            "Please produce a comprehensive risk assessment",
        )
        self.assertFalse(_plan_requires_intent(plan))

    def test_route_plan_preserves_parallel_then_sequential_dependencies(self):
        plan = _build_skill_execution_plan(
            _generic_contract(),
            "Please produce a comprehensive risk assessment",
        )

        self.assertEqual(plan["selection"], "matched")
        self.assertEqual(plan["route_id"], "full")
        self.assertEqual([source["id"] for source in plan["bootstrap_sources"]], ["catalog"])
        self.assertEqual(plan["waves"][0]["workers"], ["research", "analysis"])
        self.assertEqual(plan["waves"][1]["workers"], ["review"])
        self.assertEqual(plan["waves"][1]["dependencies"], ["parallel"])
        self.assertTrue(plan["requires_full_output"])

    def test_equal_priority_overlaps_are_ambiguous_not_worker_count_ranked(self):
        contract = _generic_contract()
        execution = contract["execution_contract"]
        execution["routes"] = [
            {
                "id": "small",
                "patterns": ["shared request"],
                "priority": 5,
                "waves": [{
                    "id": "small",
                    "mode": "direct",
                    "workers": ["research"],
                    "dependencies": [],
                }],
            },
            {
                "id": "large",
                # More matching patterns and more workers must not act as an
                # undeclared specificity/tie-break signal.
                "patterns": ["shared request", "shared", "request"],
                "priority": 5,
                "waves": [{
                    "id": "large",
                    "mode": "parallel",
                    "workers": ["research", "analysis", "review"],
                    "dependencies": [],
                }],
            },
            {
                "id": "fallback",
                "patterns": ["does not match"],
                "priority": 999,
                "default": True,
                "waves": [{
                    "id": "fallback",
                    "mode": "direct",
                    "workers": ["review"],
                    "dependencies": [],
                }],
            },
        ]

        plan = _build_skill_execution_plan(contract, "shared request")

        self.assertEqual(plan["selection"], "ambiguous")
        self.assertIsNone(plan["route_id"])
        self.assertEqual(plan["required_workers"], [])
        self.assertEqual(plan["ambiguous_route_ids"], ["small", "large"])

    def test_explicit_route_order_and_priority_resolve_overlaps(self):
        contract = _generic_contract()
        execution = contract["execution_contract"]
        execution["routes"] = [
            {
                "id": "first",
                "patterns": ["shared"],
                "priority": 5,
                "waves": [{
                    "id": "first",
                    "mode": "direct",
                    "workers": ["research"],
                    "dependencies": [],
                }],
            },
            {
                "id": "second",
                "patterns": ["shared"],
                "priority": 5,
                "waves": [{
                    "id": "second",
                    "mode": "parallel",
                    "workers": ["analysis", "review"],
                    "dependencies": [],
                }],
            },
        ]
        execution["route_selection_policy"] = {
            "tie_break": "explicit_order",
            "route_order": ["first", "second"],
        }

        ordered = _build_skill_execution_plan(contract, "shared")
        self.assertEqual(ordered["selection"], "matched")
        self.assertEqual(ordered["route_id"], "first")

        execution["routes"][1]["priority"] = 6
        priority = _build_skill_execution_plan(contract, "shared")
        self.assertEqual(priority["route_id"], "second")

    def test_runtime_rejects_persisted_redos_pattern_without_executing_it(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"][0]["patterns"] = ["(a+)+$"]
        started = time.perf_counter()

        plan = _build_skill_execution_plan(
            contract,
            "a" * 200_000 + "!",
        )

        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual(plan["selection"], "invalid_contract")
        self.assertEqual(plan["required_workers"], [])
        self.assertEqual(plan["waves"], [])

    def test_runtime_bounds_legacy_route_pattern_list_before_matching(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"][0]["patterns"] = [
            "safe-pattern"
        ] * 100_000
        started = time.perf_counter()

        plan = _build_skill_execution_plan(contract, "safe-pattern")

        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(plan["selection"], "invalid_contract")
        self.assertEqual(plan["required_workers"], [])

    def test_declared_default_route_replaces_undeclared_scope_guessing(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"][0]["patterns"] = [
            "wording-that-will-not-match"
        ]
        contract["execution_contract"]["routes"][0]["default"] = True
        plan = _build_skill_execution_plan(
            contract,
            "Produce a complete end-to-end report package",
        )

        self.assertEqual(plan["selection"], "default")
        self.assertEqual(plan["route_id"], "full")
        self.assertTrue(plan["requires_full_output"])

    def test_no_routes_uses_implicit_dependency_waves(self):
        contract = _generic_contract()
        contract["execution_contract"]["routes"] = []
        contract["execution_contract"]["workers"] = [
            {"id": "research", "file": "workers/research.yaml"},
            {
                "id": "review",
                "file": "workers/review.yaml",
                "dependencies": ["research"],
            },
        ]
        contract["workers"] = contract["execution_contract"]["workers"]
        plan = _build_skill_execution_plan(contract, "run the skill")

        self.assertEqual(plan["selection"], "implicit")
        self.assertEqual(plan["waves"][0]["workers"], ["research"])
        self.assertEqual(plan["waves"][1]["workers"], ["review"])

    def test_direct_route_does_not_inherit_package_wide_report_contract(self):
        contract = _generic_contract()
        contract["execution_contract"]["knowledge_bootstrap"] = {"sources": []}
        contract["execution_contract"]["routes"].append({
            "id": "direct",
            "patterns": ["single research lookup"],
            "priority": 20,
            "waves": [{
                "id": "direct",
                "mode": "direct",
                "workers": ["research"],
                "dependencies": [],
            }],
        })
        state = HarnessRunState(
            available_tools={"skill_view", "delegate_task"},
            original_user_text="single research lookup",
        )
        state.session_skill_names.add("generic")
        state.record_skill_view(
            {"name": "generic"},
            {
                "linked_files": {
                    "orchestration": ["orchestration/orchestrator.yaml"],
                    "workers": contract["worker_files"],
                    "formats": ["formats/output.md"],
                },
                "resource_graph": {
                    "categories": {
                        "orchestration": {
                            "sample": ["orchestration/orchestrator.yaml"],
                        },
                        "workers": {"sample": contract["worker_files"]},
                        "formats": {"sample": ["formats/output.md"]},
                    }
                },
                "workflow_contract": contract,
            },
        )
        state.record_skill_view(
            {"name": "generic", "file_path": "orchestration/orchestrator.yaml"},
            {},
        )
        state.record_skill_view(
            {"name": "generic", "file_path": "workers/research.yaml"},
            {},
        )
        self.assertFalse(
            state.skill_execution_plans["generic"]["requires_full_output"]
        )
        state.record_delegate_task(
            {
                "goal": "research",
                "skill_name": "generic",
                "worker_id": "research",
                "worker_file": "workers/research.yaml",
                "workflow_stage": "direct",
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "worker_id": "research",
                    "worker_file": "workers/research.yaml",
                    "workflow_stage": "direct",
                    "result_path": "results/research.txt",
                    "tool_audit": {
                        "inspected_skill_files": ["workers/research.yaml"],
                        "read_result_paths": [],
                    },
                }]
            },
        )
        self.assertEqual((False, ""), state.needs_more_skill_workflow())

    def test_gate_requires_bootstrap_and_actual_worker_completion_in_order(self):
        contract = _generic_contract()
        state = HarnessRunState(
            available_tools={"skill_view", "delegate_task", "write_file", "merge_files"},
            original_user_text=(
                "Please produce a comprehensive risk assessment\nNAME=TEST"
            ),
        )
        state.session_skill_names.add("generic")
        state.record_skill_view(
            {"name": "generic"},
            {
                "linked_files": {
                    "orchestration": ["orchestration/orchestrator.yaml"],
                    "workers": contract["worker_files"],
                    "formats": ["formats/output.md"],
                },
                "resource_graph": {
                    "categories": {
                        "orchestration": {"sample": ["orchestration/orchestrator.yaml"]},
                        "workers": {"sample": contract["worker_files"]},
                        "formats": {"sample": ["formats/output.md"]},
                    }
                },
                "workflow_contract": contract,
            },
        )
        for path in (
            "orchestration/orchestrator.yaml",
            "workers/research.yaml",
            "workers/analysis.yaml",
            "workers/review.yaml",
            "formats/output.md",
        ):
            state.record_skill_view({"name": "generic", "file_path": path}, {})

        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("knowledge bootstrap", reason)

        state.record_delegate_task(
            {
                "tasks": [{
                    "goal": "bootstrap",
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "catalog",
                }]
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "catalog",
                    "result_path": "results/catalog.txt",
                }]
            },
        )
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("parallel", reason)
        self.assertIn("research, analysis", reason)

        # A model cannot bypass the child audit by returning a fabricated
        # completed ledger row without the exact worker contract and reads.
        state.record_delegate_task(
            {
                "goal": "research without audit metadata",
                "skill_name": "generic",
                "worker_id": "research",
            },
            {"results": [{
                "status": "completed",
                "skill_name": "generic",
                "worker_id": "research",
                "result_path": "results/unverified-research.txt",
            }]},
        )
        self.assertNotIn(
            "research",
            state.skill_completed_workers.get("generic", set()),
        )

        state.record_delegate_task(
            {
                "tasks": [
                    {
                        "goal": "research",
                        "skill_name": "generic",
                        "worker_id": "research",
                        "worker_file": "workers/research.yaml",
                        "required_result_paths": ["results/catalog.txt"],
                    },
                    {
                        "goal": "analysis",
                        "skill_name": "generic",
                        "worker_id": "analysis",
                        "worker_file": "workers/analysis.yaml",
                        "required_result_paths": ["results/catalog.txt"],
                    },
                ]
            },
            {
                "results": [
                    {
                        "status": "completed",
                        "skill_name": "generic",
                        "worker_id": "research",
                        "worker_file": "workers/research.yaml",
                        "result_path": "results/research.txt",
                        "required_result_paths": ["results/catalog.txt"],
                        "tool_audit": {
                            "inspected_skill_files": ["workers/research.yaml"],
                            "read_result_paths": ["results/catalog.txt"],
                        },
                    },
                    {
                        "status": "completed",
                        "skill_name": "generic",
                        "worker_id": "analysis",
                        "worker_file": "workers/analysis.yaml",
                        "result_path": "results/analysis.txt",
                        "required_result_paths": ["results/catalog.txt"],
                        "tool_audit": {
                            "inspected_skill_files": ["workers/analysis.yaml"],
                            "read_result_paths": ["results/catalog.txt"],
                        },
                    },
                ]
            },
        )
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("review", reason)
        self.assertNotIn("research", reason)

        state.record_delegate_task(
            {
                "goal": "review",
                "skill_name": "generic",
                "worker_id": "review",
                "worker_file": "workers/review.yaml",
                "required_result_paths": [
                    "results/catalog.txt",
                    "results/research.txt",
                    "results/analysis.txt",
                ],
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "worker_id": "review",
                    "worker_file": "workers/review.yaml",
                    "result_path": "results/review.txt",
                    "required_result_paths": [
                        "results/catalog.txt",
                        "results/research.txt",
                        "results/analysis.txt",
                    ],
                    "tool_audit": {
                        "inspected_skill_files": ["workers/review.yaml"],
                        "read_result_paths": [
                            "results/catalog.txt",
                            "results/research.txt",
                            "results/analysis.txt",
                        ],
                    },
                }]
            },
        )
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("modular", reason)

    def test_aggregation_runtime_uses_topology_and_only_declared_dependency_results(self):
        contract = _generic_contract()
        contract["execution_contract"]["aggregation"] = {
            "steps": [
                {"id": "final", "depends_on": ["review"]},
                {"id": "review"},
                {"id": "independent"},
            ]
        }
        plan = _build_skill_execution_plan(
            contract,
            "Please produce a comprehensive assessment",
        )

        first = _next_incomplete_aggregation_step(plan, set())
        self.assertEqual("review", first["id"])

        state = HarnessRunState(original_user_text="run the workflow")
        state.skill_worker_results["generic"] = {
            "research": {
                "status": "completed",
                "result_path": "results/research.txt",
            }
        }
        state.skill_aggregation_results["generic"] = {
            "review": {
                "status": "completed",
                "result_path": "results/review.txt",
            },
            "independent": {
                "status": "completed",
                "result_path": "results/independent.txt",
            },
        }
        next_step = _next_incomplete_aggregation_step(
            plan,
            {"review", "independent"},
        )
        self.assertEqual("final", next_step["id"])

        final_paths = _prerequisite_result_paths(
            state,
            "generic",
            plan,
            "aggregation",
            step_id="final",
        )
        self.assertIn("results/research.txt", final_paths)
        self.assertIn("results/review.txt", final_paths)
        self.assertNotIn("results/independent.txt", final_paths)

        review_paths = _prerequisite_result_paths(
            state,
            "generic",
            plan,
            "aggregation",
            step_id="review",
        )
        self.assertIn("results/research.txt", review_paths)
        self.assertNotIn("results/review.txt", review_paths)
        self.assertNotIn("results/independent.txt", review_paths)

    def test_out_of_order_worker_and_aggregation_results_are_not_credited(self):
        contract = _generic_contract()
        contract["execution_contract"]["aggregation"] = {
            "steps": [
                {"id": "deduplicate"},
                {"id": "consistency", "depends_on": ["deduplicate"]},
            ]
        }
        state = HarnessRunState(
            available_tools={"skill_view", "delegate_task", "write_file", "merge_files"},
            original_user_text=(
                "Please produce a comprehensive risk assessment\nNAME=TEST"
            ),
        )
        state.session_skill_names.add("generic")
        state.record_skill_view(
            {"name": "generic"},
            {
                "linked_files": {
                    "orchestration": ["orchestration/orchestrator.yaml"],
                    "workers": contract["worker_files"],
                    "formats": ["formats/output.md"],
                },
                "resource_graph": {
                    "categories": {
                        "orchestration": {"sample": ["orchestration/orchestrator.yaml"]},
                        "workers": {"sample": contract["worker_files"]},
                        "formats": {"sample": ["formats/output.md"]},
                    }
                },
                "workflow_contract": contract,
            },
        )
        for path in (
            "orchestration/orchestrator.yaml",
            "workers/research.yaml",
            "workers/analysis.yaml",
            "workers/review.yaml",
            "formats/output.md",
        ):
            state.record_skill_view({"name": "generic", "file_path": path}, {})
        state.record_delegate_task(
            {
                "goal": "bootstrap",
                "skill_name": "generic",
                "step_type": "knowledge_bootstrap",
                "step_id": "catalog",
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "step_type": "knowledge_bootstrap",
                    "step_id": "catalog",
                    "result_path": "results/catalog.txt",
                }]
            },
        )
        state.record_delegate_task(
            {
                "goal": "review too early",
                "skill_name": "generic",
                "worker_id": "review",
                "workflow_stage": "review",
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "worker_id": "review",
                    "workflow_stage": "review",
                    "result_path": "results/review.txt",
                }]
            },
        )
        self.assertNotIn("review", state.skill_completed_workers.get("generic", set()))

        for worker_id in ("research", "analysis"):
            state.record_delegate_task(
                {
                    "goal": worker_id,
                    "skill_name": "generic",
                    "worker_id": worker_id,
                    "worker_file": f"workers/{worker_id}.yaml",
                    "workflow_stage": "parallel",
                    "required_result_paths": ["results/catalog.txt"],
                },
                {
                    "results": [{
                        "status": "completed",
                        "skill_name": "generic",
                        "worker_id": worker_id,
                        "worker_file": f"workers/{worker_id}.yaml",
                        "workflow_stage": "parallel",
                        "result_path": f"results/{worker_id}.txt",
                        "required_result_paths": ["results/catalog.txt"],
                        "tool_audit": {
                            "inspected_skill_files": [f"workers/{worker_id}.yaml"],
                            "read_result_paths": ["results/catalog.txt"],
                        },
                    }]
                },
            )
        state.record_delegate_task(
            {
                "goal": "review",
                "skill_name": "generic",
                "worker_id": "review",
                "worker_file": "workers/review.yaml",
                "workflow_stage": "review",
                "required_result_paths": [
                    "results/catalog.txt",
                    "results/research.txt",
                    "results/analysis.txt",
                ],
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "worker_id": "review",
                    "worker_file": "workers/review.yaml",
                    "workflow_stage": "review",
                    "result_path": "results/review.txt",
                    "required_result_paths": [
                        "results/catalog.txt",
                        "results/research.txt",
                        "results/analysis.txt",
                    ],
                    "tool_audit": {
                        "inspected_skill_files": ["workers/review.yaml"],
                        "read_result_paths": [
                            "results/catalog.txt",
                            "results/research.txt",
                            "results/analysis.txt",
                        ],
                    },
                }]
            },
        )
        state.record_delegate_task(
            {
                "goal": "consistency too early",
                "skill_name": "generic",
                "step_type": "aggregation",
                "step_id": "consistency",
            },
            {
                "results": [{
                    "status": "completed",
                    "skill_name": "generic",
                    "step_type": "aggregation",
                    "step_id": "consistency",
                    "result_path": "results/consistency.txt",
                }]
            },
        )
        self.assertNotIn(
            "consistency",
            state.skill_completed_aggregation.get("generic", set()),
        )
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("deduplicate", reason)

        for step_id in ("deduplicate", "consistency"):
            prerequisite_paths = [
                "results/catalog.txt",
                "results/research.txt",
                "results/analysis.txt",
                "results/review.txt",
            ]
            if step_id == "consistency":
                prerequisite_paths.append("results/deduplicate.txt")
            state.record_delegate_task(
                {
                    "goal": step_id,
                    "skill_name": "generic",
                    "step_type": "aggregation",
                    "step_id": step_id,
                    "required_result_paths": prerequisite_paths,
                },
                {
                    "results": [{
                        "status": "completed",
                        "skill_name": "generic",
                        "step_type": "aggregation",
                        "step_id": step_id,
                        "result_path": f"results/{step_id}.txt",
                        "required_result_paths": prerequisite_paths,
                        "tool_audit": {
                            "read_result_paths": prerequisite_paths,
                        },
                    }]
                },
            )
        needed, reason = state.needs_more_skill_workflow()
        self.assertTrue(needed)
        self.assertIn("modular", reason)

    def test_exact_declared_artifact_set_and_quality_markers_are_verified(self):
        contract = _generic_contract()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                module_one = "# Findings\n**Summary** decision\n**Source** catalog\nbody\n"
                module_two = "# Review\n**Summary** decision\n**Source** workers\nfinal\n"
                files = {
                    "01_findings.md": module_one,
                    "02_review.md": module_two,
                    "README.md": "# Index\n",
                    "_checklist.md": "# Checklist\ncomplete\n",
                    "TEST_FULL.md": module_one + module_two,
                }
                for name, content in files.items():
                    (workspace / name).write_text(content, encoding="utf-8")

                state = HarnessRunState(user_id="u", session_id="s")
                state.skill_workflow_contracts["generic"] = contract
                state.artifacts = [
                    {"path": name, "size_bytes": len(content.encode("utf-8"))}
                    for name, content in files.items()
                ]
                self.assertTrue(_has_modular_artifacts_for_contract(state, contract))
                self.assertEqual(
                    [],
                    _markdown_quality_findings(
                        state,
                        ["TEST_FULL.md"],
                        complex_report=True,
                    ),
                )

                (workspace / "02_review.md").write_text(
                    "# Review\n**Summary** decision\nfinal\n",
                    encoding="utf-8",
                )
                findings = _markdown_quality_findings(
                    state,
                    ["TEST_FULL.md"],
                    complex_report=True,
                )
                self.assertTrue(any("missing skill-declared markers" in item for item in findings))


class DelegatedWorkerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_disables_full_skill_enforcement_and_persists_full_result(self):
        observed = {}

        async def fake_run_stream(*args, **kwargs):
            observed.update(kwargs)
            yield {"type": "delta", "content": "worker evidence " * 500}
            yield {"type": "done", "finish_reason": "stop"}

        context = ToolContext(
            user_id="u",
            session_id="s",
            model_id="model",
            provider_config={
                "base_url": "http://example",
                "api_model": "model",
                "context_length": 303_872,
            },
            enabled_tools=("skill_view",),
            run_id="parent",
            root_run_id="root",
        )
        with (
            patch("agent_loop.run_stream", fake_run_stream),
            patch(
                "tools.delegation.registry_dispatch",
                return_value=json.dumps({
                    "success": True,
                    "content": "exact research worker contract",
                }),
            ) as registry_dispatch,
            patch(
                "tools.delegation.persist_result_for_history",
                return_value="results/delegate_worker.txt",
            ) as persist,
        ):
            result = await _run_child(
                {
                    "goal": "execute worker",
                    "skill_name": "generic",
                    "worker_id": "research",
                    "worker_file": "workers/research.yaml",
                    "workflow_stage": "parallel",
                    "step_type": "worker",
                    "step_id": "research",
                },
                context,
                0,
            )

        self.assertFalse(observed["enforce_session_skill_workflow"])
        self.assertEqual(result["result_path"], "results/delegate_worker.txt")
        self.assertGreater(result["result_chars"], 1000)
        self.assertNotIn("result", result)
        self.assertEqual(result["worker_id"], "research")
        self.assertEqual(
            result["tool_audit"]["inspected_skill_files"],
            ["workers/research.yaml"],
        )
        registry_dispatch.assert_awaited_once_with(
            "skill_view",
            {"name": "generic", "file_path": "workers/research.yaml"},
            context=context,
        )
        persist.assert_called_once()


if __name__ == "__main__":
    unittest.main()
