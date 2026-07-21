import unittest

from agent_loop import (
    HarnessRunState,
    _bind_loader_owned_skill_runtime_ir,
)


def _many_worker_contract(worker_count: int = 80) -> dict:
    workers = [
        {"id": f"worker-{index:03d}", "file": f"workers/worker-{index:03d}.yaml"}
        for index in range(worker_count)
    ]
    modular_files = [f"outputs/result-{index:03d}.md" for index in range(worker_count)]
    output = {
        "declared_modular_files": modular_files,
        "declared_final_artifact": "PORTABLE_FULL.md",
        "declared_file_count": worker_count + 1,
        "merge_mandatory": True,
        "merge_input_order": modular_files,
        "merge_separator": "",
    }
    execution = {
        "workers": workers,
        "routes": [{
            "id": "portable-full",
            "patterns": ["portable.*assessment"],
            "priority": 10,
            "requires_full_output": True,
            "waves": [{
                "id": "all-workers",
                "mode": "parallel",
                "workers": [worker["id"] for worker in workers],
                "dependencies": [],
            }],
        }],
        "output_contract": output,
        "diagnostics": {"errors": [], "warnings": []},
    }
    return {
        "worker_files": [worker["file"] for worker in workers],
        "workers": workers,
        "execution_contract": execution,
        "output_contract": output,
        "requires_worker_outputs": True,
        "requires_modular_artifacts": True,
        "requires_merge": True,
    }


class SkillRuntimeIRBindingTests(unittest.TestCase):
    def _state(self) -> HarnessRunState:
        return HarnessRunState(
            available_tools={
                "skill_view", "delegate_task", "write_file", "merge_files",
            },
            original_user_text="Produce a portable comprehensive assessment",
        )

    def test_compact_activation_installs_complete_loader_owned_runtime_ir(self):
        digest = "a" * 64
        contract = _many_worker_contract()
        state = self._state()

        complete = state.record_skill_view(
            {"name": "portable-workflow"},
            {
                "success": True,
                "content": "Follow the complete portable workflow.",
                "skill_md_sha256": digest,
                "activation_envelope_compacted": True,
                "activation_envelope_omitted_sections": [
                    "workflow_contract", "execution_contract",
                ],
                "workflow_contract_summary": {"sha256": "f" * 64},
                "resource_graph": {
                    "categories": {"workers": {"count": 80}},
                },
            },
        )

        self.assertTrue(complete)
        self.assertEqual(
            digest,
            state.viewed_skill_main_sha256["portable-workflow"],
        )
        self.assertNotIn("portable-workflow", state.skill_workflow_contracts)
        self.assertNotIn(
            "__manifest__",
            state.viewed_skill_files["portable-workflow"],
        )

        installed, reason = _bind_loader_owned_skill_runtime_ir(
            state,
            "portable-workflow",
            {
                "skill_md_sha256": digest,
                "workflow_contract": contract,
            },
        )

        self.assertTrue(installed, reason)
        runtime_contract = state.skill_workflow_contracts["portable-workflow"]
        self.assertEqual(
            80,
            len(runtime_contract["execution_contract"]["workers"]),
        )
        plan = state.skill_execution_plans["portable-workflow"]
        self.assertEqual("matched", plan["selection"])
        self.assertEqual(80, len(plan["required_workers"]))
        artifact_plan = state.skill_artifact_plans["portable-workflow"]
        self.assertTrue(artifact_plan["valid"], artifact_plan)
        self.assertEqual(80, len(artifact_plan["modular_artifacts"]))
        self.assertEqual(
            digest,
            state.skill_runtime_ir_sha256["portable-workflow"],
        )

    def test_changed_main_document_revokes_stale_runtime_authority(self):
        viewed_digest = "1" * 64
        current_digest = "2" * 64
        state = self._state()
        contract = _many_worker_contract(3)
        state.record_skill_view(
            {"name": "portable-workflow"},
            {
                "success": True,
                "content": "Old instructions.",
                "skill_md_sha256": viewed_digest,
                "workflow_contract": contract,
            },
        )
        self.assertIn("portable-workflow", state.skill_execution_plans)

        installed, reason = _bind_loader_owned_skill_runtime_ir(
            state,
            "portable-workflow",
            {
                "skill_md_sha256": current_digest,
                "workflow_contract": contract,
            },
        )

        self.assertFalse(installed)
        self.assertEqual("skill_document_changed_after_activation", reason)
        self.assertNotIn("portable-workflow", state.skill_workflow_contracts)
        self.assertNotIn("portable-workflow", state.skill_execution_plans)
        self.assertNotIn("portable-workflow", state.skill_artifact_plans)
        self.assertNotIn("skill.md", state.viewed_skill_files["portable-workflow"])
        self.assertNotIn("portable-workflow", state.viewed_skill_main_sha256)
        error = state.skill_runtime_ir_binding_errors["portable-workflow"]
        self.assertEqual(viewed_digest, error["viewed_skill_md_sha256"])
        self.assertEqual(current_digest, error["loaded_skill_md_sha256"])

    def test_paginated_main_is_trusted_only_after_contiguous_eof(self):
        digest = "3" * 64
        state = self._state()
        first = {
            "success": True,
            "file": "SKILL.md",
            "content": "abc",
            "sha256": digest,
            "skill_md_sha256": digest,
            # A bounded transport may still fit compiler metadata beside the
            # first page.  It must not become runtime authority before EOF.
            "workflow_contract": _many_worker_contract(1),
            "pagination": {
                "offset": 0,
                "returned_chars": 3,
                "total_chars": 6,
                "has_more": True,
                "next_offset": 3,
            },
        }
        self.assertFalse(
            state.record_skill_view({"name": "portable-workflow"}, first)
        )
        self.assertNotIn("portable-workflow", state.viewed_skill_main_sha256)
        self.assertNotIn("portable-workflow", state.skill_workflow_contracts)
        installed, reason = _bind_loader_owned_skill_runtime_ir(
            state,
            "portable-workflow",
            {"skill_md_sha256": digest, "workflow_contract": _many_worker_contract(1)},
        )
        self.assertFalse(installed)
        self.assertEqual("canonical_main_document_incomplete", reason)

        last = {
            "success": True,
            "file": "SKILL.md",
            "content": "def",
            "sha256": digest,
            "pagination": {
                "offset": 3,
                "returned_chars": 3,
                "total_chars": 6,
                "has_more": False,
                "next_offset": None,
            },
        }
        self.assertTrue(state.record_skill_view(
            {
                "name": "portable-workflow",
                "file_path": "SKILL.md",
                "offset": 3,
            },
            last,
        ))
        self.assertEqual(
            digest,
            state.viewed_skill_main_sha256["portable-workflow"],
        )
        installed, reason = _bind_loader_owned_skill_runtime_ir(
            state,
            "portable-workflow",
            {"skill_md_sha256": digest, "workflow_contract": _many_worker_contract(1)},
        )
        self.assertTrue(installed, reason)

    def test_manifest_or_resource_contract_cannot_install_runtime_ir(self):
        digest = "7" * 64
        contract = _many_worker_contract(2)
        state = self._state()
        state.record_skill_view(
            {"name": "portable-workflow"},
            {
                "success": True,
                "content": "Complete instructions without inline IR.",
                "skill_md_sha256": digest,
            },
        )

        self.assertTrue(state.record_skill_view(
            {"name": "portable-workflow", "file_path": "__manifest__"},
            {
                "success": True,
                "file": "__manifest__",
                "workflow_contract": contract,
                "resource_graph": {"categories": {"workers": {"count": 2}}},
            },
        ))
        self.assertNotIn("portable-workflow", state.skill_workflow_contracts)

        self.assertTrue(state.record_skill_view(
            {
                "name": "portable-workflow",
                "file_path": "workers/worker-000.yaml",
            },
            {"success": True, "workflow_contract": contract},
        ))
        self.assertNotIn("portable-workflow", state.skill_workflow_contracts)

    def test_new_complete_activation_replaces_old_resource_receipts(self):
        old_digest = "5" * 64
        new_digest = "6" * 64
        state = self._state()
        state.record_skill_view(
            {"name": "portable-workflow"},
            {"success": True, "content": "old", "skill_md_sha256": old_digest},
        )
        state.record_skill_view(
            {
                "name": "portable-workflow",
                "file_path": "workers/old.yaml",
            },
            {},
        )
        state.skill_runtime_ir_sha256["portable-workflow"] = old_digest
        state.skill_capability_plans["portable-workflow"] = {"status": "accepted"}

        self.assertTrue(state.record_skill_view(
            {"name": "portable-workflow"},
            {"success": True, "content": "new", "skill_md_sha256": new_digest},
        ))

        self.assertEqual(
            {"skill.md"},
            state.viewed_skill_files["portable-workflow"],
        )
        self.assertEqual(
            new_digest,
            state.viewed_skill_main_sha256["portable-workflow"],
        )
        self.assertNotIn("portable-workflow", state.skill_runtime_ir_sha256)
        self.assertNotIn("portable-workflow", state.skill_capability_plans)

    def test_small_full_activation_retains_legacy_manifest_equivalence(self):
        digest = "4" * 64
        contract = _many_worker_contract(1)
        state = self._state()

        self.assertTrue(state.record_skill_view(
            {"name": "small-workflow"},
            {
                "success": True,
                "content": "Run the small workflow.",
                "skill_md_sha256": digest,
                "workflow_contract": contract,
                "resource_graph": {
                    "categories": {
                        "workers": {"sample": ["workers/worker-000.yaml"]},
                    },
                },
            },
        ))

        self.assertIn("small-workflow", state.skill_workflow_contracts)
        self.assertIn("small-workflow", state.skill_execution_plans)
        self.assertIn("__manifest__", state.viewed_skill_files["small-workflow"])
        installed, reason = _bind_loader_owned_skill_runtime_ir(
            state,
            "small-workflow",
            {"skill_md_sha256": digest, "workflow_contract": contract},
        )
        self.assertTrue(installed, reason)
        self.assertEqual(digest, state.skill_runtime_ir_sha256["small-workflow"])


if __name__ == "__main__":
    unittest.main()
