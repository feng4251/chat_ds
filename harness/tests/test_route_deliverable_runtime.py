import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import (
    HarnessRunState,
    _deterministic_verifier_payload,
    _workflow_gate_tool_policy,
)


def _route_contract(*, artifact_path: str, artifact_format: str, aggregate: bool):
    aggregation_steps = (
        [{"id": "canonicalize", "output": "canonical record"}]
        if aggregate
        else []
    )
    return {
        "workflow_files": ["workflows/catalog.yaml"],
        "orchestrator_files": ["workflows/catalog.yaml"],
        "worker_files": ["workers/catalog-reader.yaml"],
        "workers": [
            {
                "id": "catalog-reader",
                "file": "workers/catalog-reader.yaml",
            }
        ],
        "requires_worker_outputs": True,
        "execution_contract": {
            "workers": [
                {
                    "id": "catalog-reader",
                    "file": "workers/catalog-reader.yaml",
                }
            ],
            "worker_ids": ["catalog-reader"],
            "routes": [
                {
                    "id": "canonical-export",
                    "patterns": ["canonical export"],
                    "spawn_mode": "direct",
                    "workers": ["catalog-reader"],
                    "waves": [
                        {
                            "id": "direct",
                            "mode": "direct",
                            "workers": ["catalog-reader"],
                            "dependencies": [],
                        }
                    ],
                    "requires_full_output": False,
                    "deliverable": {
                        "type": artifact_format,
                        "canonical": artifact_path,
                    },
                    "output_contract": {
                        "declared_artifacts": [artifact_path],
                        "declared_file_count": 1,
                        "artifact_formats": {
                            artifact_path: artifact_format,
                        },
                        "route_scoped": True,
                    },
                    "source_file": "workflows/catalog.yaml",
                }
            ],
            "knowledge_bootstrap": {"sources": []},
            "aggregation": {"steps": aggregation_steps},
            "diagnostics": {
                "valid": True,
                "errors": [],
                "warnings": [],
            },
        },
    }


class RouteDeliverableRuntimeTests(unittest.TestCase):
    def _ready_state(self, contract: dict, workspace: Path) -> HarnessRunState:
        state = HarnessRunState(
            user_id="route-user",
            session_id="route-session",
            available_tools={"skill_view", "delegate_task", "write_file"},
            original_user_text="Run the canonical export route",
            skill_workflow_activation="complex_deliverable",
        )
        state.session_skill_names.add("catalog-skill")
        state.record_skill_view(
            {"name": "catalog-skill"},
            {
                "linked_files": {
                    "workflows": ["workflows/catalog.yaml"],
                    "workers": ["workers/catalog-reader.yaml"],
                },
                "resource_graph": {
                    "categories": {
                        "workflows": {"sample": ["workflows/catalog.yaml"]},
                        "workers": {
                            "sample": ["workers/catalog-reader.yaml"]
                        },
                    }
                },
                "workflow_contract": contract,
            },
        )
        for file_path in (
            "workflows/catalog.yaml",
            "workers/catalog-reader.yaml",
        ):
            state.record_skill_view(
                {"name": "catalog-skill", "file_path": file_path},
                {},
            )
        return state

    @staticmethod
    def _complete_worker(state: HarnessRunState) -> None:
        state.record_delegate_task(
            {
                "goal": "read the catalog",
                "skill_name": "catalog-skill",
                "step_type": "worker",
                "step_id": "catalog-reader",
                "worker_id": "catalog-reader",
                "worker_file": "workers/catalog-reader.yaml",
                "workflow_stage": "direct",
            },
            {
                "results": [
                    {
                        "status": "completed",
                        "skill_name": "catalog-skill",
                        "step_type": "worker",
                        "step_id": "catalog-reader",
                        "worker_id": "catalog-reader",
                        "worker_file": "workers/catalog-reader.yaml",
                        "workflow_stage": "direct",
                        "result_path": "results/catalog-reader.txt",
                        "result_chars": 500,
                        "tool_audit": {
                            "inspected_skill_files": [
                                "workers/catalog-reader.yaml"
                            ],
                            "read_result_paths": [],
                        },
                    }
                ]
            },
        )

    @staticmethod
    def _complete_aggregation(state: HarnessRunState) -> None:
        state.record_delegate_task(
            {
                "goal": "canonicalize records",
                "skill_name": "catalog-skill",
                "step_type": "aggregation",
                "step_id": "canonicalize",
                "workflow_stage": "aggregation",
                "required_result_paths": ["results/catalog-reader.txt"],
            },
            {
                "results": [
                    {
                        "status": "completed",
                        "skill_name": "catalog-skill",
                        "step_type": "aggregation",
                        "step_id": "canonicalize",
                        "result_path": "results/canonicalize.txt",
                        "result_chars": 500,
                        "required_result_paths": ["results/catalog-reader.txt"],
                        "tool_audit": {
                            "read_result_paths": ["results/catalog-reader.txt"]
                        },
                    }
                ]
            },
        )

    @staticmethod
    def _complete_artifact(
        state: HarnessRunState,
        workspace: Path,
        artifact_path: str,
        content: str,
        prerequisite_paths: list[str],
    ) -> dict:
        target = workspace / artifact_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return state.record_delegate_task(
            {
                "goal": "write exact route deliverable",
                "skill_name": "catalog-skill",
                "step_type": "artifact_synthesis",
                "step_id": "declared-artifacts",
                "workflow_stage": "artifact-synthesis",
                "required_result_paths": prerequisite_paths,
                "required_output_ids": [artifact_path],
            },
            {
                "results": [
                    {
                        "status": "completed",
                        "skill_name": "catalog-skill",
                        "step_type": "artifact_synthesis",
                        "step_id": "declared-artifacts",
                        "result_path": "results/declared-artifacts.txt",
                        "result_chars": 500,
                        "required_result_paths": prerequisite_paths,
                        "artifact_receipts": [
                            {
                                "path": artifact_path,
                                "source_tool": "write_file",
                                "tool_call_id": "write-1",
                                "size_bytes": target.stat().st_size,
                            }
                        ],
                        "tool_audit": {
                            "read_result_paths": prerequisite_paths,
                        },
                    }
                ]
            },
        )

    def test_json_canonical_route_runs_aggregation_write_receipt_and_verifier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            contract = _route_contract(
                artifact_path="reconciliation.json",
                artifact_format="json",
                aggregate=True,
            )
            with patch("agent_loop.get_workspace", return_value=workspace):
                state = self._ready_state(contract, workspace)
                plan = state.skill_execution_plans["catalog-skill"]
                artifact_plan = state.skill_artifact_plans["catalog-skill"]
                self.assertFalse(plan["requires_full_output"])
                self.assertTrue(plan["requires_artifact_output"])
                self.assertEqual(
                    ["canonicalize"],
                    [step["id"] for step in plan["aggregation_steps"]],
                )
                self.assertEqual(
                    "reconciliation.json",
                    artifact_plan["deliverable_artifacts"][0]["path"],
                )
                self.assertFalse(artifact_plan["merge"]["required"])

                self._complete_worker(state)
                needed, reason = state.needs_more_skill_workflow()
                self.assertTrue(needed)
                self.assertIn("aggregation", reason)
                self._complete_aggregation(state)

                needed, reason = state.needs_more_skill_workflow()
                self.assertTrue(needed)
                self.assertIn("generate declared artifacts", reason)
                policy = _workflow_gate_tool_policy(reason, state.available_tools)
                self.assertEqual(["delegate_task"], policy["tools"])
                self.assertNotIn("merge_files", policy["tools"])

                update = self._complete_artifact(
                    state,
                    workspace,
                    "reconciliation.json",
                    json.dumps({"records": [{"id": "A-1"}]}),
                    ["results/catalog-reader.txt", "results/canonicalize.txt"],
                )
                self.assertEqual(["declared-artifacts"], update["completed_step_ids"])
                self.assertEqual((False, ""), state.needs_more_skill_workflow())
                verifier = _deterministic_verifier_payload(state)
                self.assertIsNotNone(verifier)
                self.assertFalse(verifier["needs_more_work"])
                self.assertTrue(verifier["artifact_contract"]["valid"])

                # The same exact receipt/path is insufficient when the declared
                # JSON format is no longer valid: terminal verification fails.
                (workspace / "reconciliation.json").write_text(
                    "{not valid json",
                    encoding="utf-8",
                )
                verifier = _deterministic_verifier_payload(state)
                self.assertTrue(verifier["needs_more_work"])
                self.assertIn("invalid_json_artifact", verifier["reason"])

    def test_csv_single_artifact_route_requires_real_receipt_without_merge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            contract = _route_contract(
                artifact_path="catalog.csv",
                artifact_format="csv",
                aggregate=False,
            )
            with patch("agent_loop.get_workspace", return_value=workspace):
                state = self._ready_state(contract, workspace)
                self._complete_worker(state)
                needed, reason = state.needs_more_skill_workflow()
                self.assertTrue(needed)
                self.assertIn("generate declared artifacts", reason)

                update = self._complete_artifact(
                    state,
                    workspace,
                    "catalog.csv",
                    "accession,title\nA-1,Example object\n",
                    ["results/catalog-reader.txt"],
                )
                self.assertEqual(["declared-artifacts"], update["completed_step_ids"])
                self.assertTrue(
                    any(
                        artifact.get("delegated_artifact_receipt") is True
                        and artifact.get("path") == "catalog.csv"
                        for artifact in state.artifacts
                    )
                )
                self.assertEqual((False, ""), state.needs_more_skill_workflow())
                verifier = _deterministic_verifier_payload(state)
                self.assertFalse(verifier["needs_more_work"])
                self.assertTrue(verifier["artifact_contract"]["valid"])
                self.assertFalse(
                    state.skill_artifact_plans["catalog-skill"]["merge"]["required"]
                )

    def test_workspace_file_without_verified_receipt_does_not_close_route(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            contract = _route_contract(
                artifact_path="catalog.csv",
                artifact_format="csv",
                aggregate=False,
            )
            with patch("agent_loop.get_workspace", return_value=workspace):
                state = self._ready_state(contract, workspace)
                self._complete_worker(state)
                (workspace / "catalog.csv").write_text(
                    "accession,title\nA-1,Example object\n",
                    encoding="utf-8",
                )
                update = state.record_delegate_task(
                    {
                        "goal": "write exact route deliverable",
                        "skill_name": "catalog-skill",
                        "step_type": "artifact_synthesis",
                        "step_id": "declared-artifacts",
                        "workflow_stage": "artifact-synthesis",
                        "required_result_paths": ["results/catalog-reader.txt"],
                        "required_output_ids": ["catalog.csv"],
                    },
                    {
                        "results": [
                            {
                                "status": "completed",
                                "skill_name": "catalog-skill",
                                "step_type": "artifact_synthesis",
                                "step_id": "declared-artifacts",
                                "result_path": "results/declared-artifacts.txt",
                                "result_chars": 500,
                                "required_result_paths": [
                                    "results/catalog-reader.txt"
                                ],
                                "artifact_receipts": [],
                                "tool_audit": {
                                    "read_result_paths": [
                                        "results/catalog-reader.txt"
                                    ]
                                },
                            }
                        ]
                    },
                )
                self.assertEqual([], update["completed_step_ids"])
                self.assertEqual(
                    ["declared-artifacts"],
                    update["terminal_failed_step_ids"],
                )
                self.assertEqual([], state.artifacts)
                needed, reason = state.needs_more_skill_workflow()
                self.assertTrue(needed)
                self.assertIn("generate declared artifacts", reason)


if __name__ == "__main__":
    unittest.main()
