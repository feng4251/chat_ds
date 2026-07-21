import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop import _bounded_skill_execution_exposure
from skills.loader import load_skill_content
from skills.manager import SkillsManager
from tools.context import ToolContext
from tools.registry import delegated_resource_boundary_error
from tools.skills import _stable_value_summary, skill_view
from tools.tool_result_storage import wrap_result


class LargeSkillProgressiveDisclosureTests(unittest.TestCase):
    def _build_skill(
        self,
        base: Path,
        count: int = 513,
        *,
        name_padding: str = "",
    ) -> Path:
        root = base / "u" / "s" / "large-standard-skill"
        references = root / "references"
        references.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            "---\n"
            "name: large-standard-skill\n"
            "description: Exercise progressive disclosure for a large resource package.\n"
            "---\n"
            "Read only the request-relevant reference.\n",
            encoding="utf-8",
        )
        for index in range(count):
            (references / f"item-{index:04d}{name_padding}.md").write_text(
                f"resource {index}\n", encoding="utf-8",
            )
        return root

    def _view_main(self, base: Path, manager: SkillsManager) -> dict:
        with (
            patch("skills.scanner.USER_SKILLS_BASE", base),
            patch("skills.manager.USER_SKILLS_BASE", base),
            patch("tools.skills.get_manager", return_value=manager),
        ):
            raw = asyncio.run(skill_view(
                name="large-standard-skill",
                user_id="u",
                session_id="s",
            ))
        return {"raw": raw, "payload": json.loads(raw)}

    def test_compacted_contract_hash_is_independent_of_mapping_insertion_order(self):
        first = {
            "workers": {
                "beta": {"depends_on": ["alpha"], "tools": ["read"]},
                "alpha": {"depends_on": [], "tools": []},
            },
            "schema_version": 1,
        }
        second = {
            "schema_version": 1,
            "workers": {
                "alpha": {"tools": [], "depends_on": []},
                "beta": {"tools": ["read"], "depends_on": ["alpha"]},
            },
        }
        self.assertEqual(
            _stable_value_summary(first, kind="workflow_contract")["sha256"],
            _stable_value_summary(second, kind="workflow_contract")["sha256"],
        )

    def test_large_selected_skill_activates_without_ambient_resource_grants(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._build_skill(Path(temp_dir))
            loaded = load_skill_content(
                root / "SKILL.md", skill_dir=str(root), session_id="s",
            )
            loaded["_chatds_scope"] = "session"
            exposure = _bounded_skill_execution_exposure(
                "use large-standard-skill",
                ["skills_list", "skill_view", "delegate_task"],
                {"large-standard-skill"},
                {"large-standard-skill": loaded},
                {},
                selected_skill_names=("large-standard-skill",),
            )

        self.assertFalse(exposure.missing_requirements)
        self.assertEqual(
            {
                ("large-standard-skill", "SKILL.md"),
                ("large-standard-skill", "__manifest__"),
            },
            set(exposure.allowed_skill_resources),
        )

    def test_large_manifest_pages_are_stable_and_one_resource_is_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = self._build_skill(base)
            manager = SkillsManager()
            first = manager._load_resource_manifest(
                root / "SKILL.md", root, "large-standard-skill", "s",
                offset=0, limit=200,
            )
            second = manager._load_resource_manifest(
                root / "SKILL.md", root, "large-standard-skill", "s",
                offset=first["manifest_pagination"]["next_offset"], limit=200,
            )

            self.assertTrue(first["success"])
            self.assertEqual(513, first["linked_file_count"])
            self.assertEqual(200, first["manifest_pagination"]["returned_entries"])
            self.assertTrue(first["manifest_pagination"]["has_more"])
            self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])

            context = ToolContext(
                user_id="u",
                session_id="s",
                agent_kind="primary",
                skill_execution_resource_boundary=True,
                allowed_skill_resources=(
                    ("large-standard-skill", "SKILL.md"),
                    ("large-standard-skill", "__manifest__"),
                ),
                selected_skill_browse_roots=("large-standard-skill",),
            )
            with patch("skills.scanner.USER_SKILLS_BASE", base):
                self.assertIsNone(delegated_resource_boundary_error(
                    "skill_view",
                    {
                        "name": "large-standard-skill",
                        "file_path": "references/item-0512.md",
                    },
                    context,
                ))
                child_error = delegated_resource_boundary_error(
                    "skill_view",
                    {
                        "name": "large-standard-skill",
                        "file_path": "references/item-0512.md",
                    },
                    ToolContext(
                        user_id="u",
                        session_id="s",
                        agent_kind="delegate",
                        delegated_resource_boundary=True,
                        skill_execution_resource_boundary=True,
                        allowed_skill_resources=((
                            "large-standard-skill", "SKILL.md",
                        ),),
                        selected_skill_browse_roots=("large-standard-skill",),
                    ),
                )
            self.assertIn("outside the compiled task closure", child_error)

    def test_small_activation_keeps_complete_compatible_resource_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            self._build_skill(base, count=3)
            viewed = self._view_main(base, SkillsManager())

        payload = viewed["payload"]
        self.assertTrue(payload["success"])
        self.assertIn(
            "Read only the request-relevant reference.",
            payload["content"],
        )
        self.assertEqual(
            [
                "references/item-0000.md",
                "references/item-0001.md",
                "references/item-0002.md",
            ],
            payload["linked_files"]["references"],
        )
        self.assertNotIn("linked_files_truncated", payload)

    def test_large_activation_is_parseable_below_tool_cap_and_manifest_is_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = self._build_skill(
                base,
                count=1_200,
                name_padding="-" + ("x" * 64),
            )
            manager = SkillsManager()
            viewed = self._view_main(base, manager)
            raw = viewed["raw"]
            payload = viewed["payload"]

            # The generic result wrapper must not turn the activation payload
            # into a non-JSON prefix plus a truncation notice.
            self.assertLessEqual(len(raw), 48_000)
            wrapped = wrap_result(raw, "skill_view", user_id="u", session_id="s")
            self.assertEqual(raw, wrapped)
            self.assertEqual(payload, json.loads(wrapped))
            self.assertIn(
                "Read only the request-relevant reference.",
                payload["content"],
            )
            self.assertTrue(payload["linked_files_truncated"])
            self.assertEqual(1_200, payload["linked_file_count"])
            self.assertEqual(
                1_200,
                payload["linked_files_returned_count"]
                + payload["linked_files_omitted_count"],
            )
            self.assertEqual("__manifest__", payload["linked_files_manifest"]["file_path"])

            offset = 0
            manifest_paths: list[str] = []
            manifest_hashes: set[str] = set()
            while True:
                page = manager._load_resource_manifest(
                    root / "SKILL.md",
                    root,
                    "large-standard-skill",
                    "s",
                    offset=offset,
                    limit=512,
                )
                manifest_hashes.add(page["manifest_sha256"])
                manifest_paths.extend(
                    path
                    for paths in page["linked_files"].values()
                    for path in paths
                )
                pagination = page["manifest_pagination"]
                if not pagination["has_more"]:
                    break
                offset = pagination["next_offset"]

            self.assertEqual(1, len(manifest_hashes))
            self.assertEqual(1_200, len(manifest_paths))
            self.assertEqual(1_200, len(set(manifest_paths)))

    def test_many_worker_contract_is_summarized_but_runtime_ir_stays_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = self._build_skill(base, count=0)
            instruction_tail = "END OF COMPLETE PORTABLE WORKFLOW INSTRUCTIONS"
            (root / "SKILL.md").write_text(
                "---\n"
                "name: large-standard-skill\n"
                "description: Execute a portable many-worker workflow when requested.\n"
                "---\n"
                "Follow the declared workflow and inspect only routed workers.\n"
                + ("Keep this generic instruction in context.\n" * 500)
                + instruction_tail
                + "\n",
                encoding="utf-8",
            )
            orchestration = root / "orchestration"
            workers_dir = orchestration / "workers"
            workers_dir.mkdir(parents=True)
            worker_ids = [f"worker-{index:03d}" for index in range(80)]
            worker_registry = "\n".join(
                f"  {worker_id}:\n"
                f"    file: orchestration/workers/{worker_id}.yaml"
                for worker_id in worker_ids
            )
            routes = "\n".join(
                f"  route-{index:03d}:\n"
                f"    patterns: ['portable task {index:03d}']\n"
                f"    workers: [{worker_id}]"
                for index, worker_id in enumerate(worker_ids)
            )
            artifacts = "\n".join(
                f"    - outputs/result-{index:03d}.json"
                for index in range(80)
            )
            (orchestration / "workflow.yaml").write_text(
                "orchestrator_id: large-standard-skill\n"
                "workers:\n"
                + worker_registry
                + "\nrouting_rules:\n"
                + routes
                + "\noutput_contract:\n"
                "  declared_artifacts:\n"
                + artifacts
                + "\n  declared_file_count: 80\n",
                encoding="utf-8",
            )
            for index, worker_id in enumerate(worker_ids):
                fields = "\n".join(
                    f"    field_{field:02d}:\n"
                    "      type: string\n"
                    f"      description: portable field {field:02d} "
                    + ("z" * 120)
                    for field in range(16)
                )
                dependency = f"[{worker_ids[index - 1]}]" if index else "[]"
                (workers_dir / f"{worker_id}.yaml").write_text(
                    f"worker_id: {worker_id}\n"
                    f"name: Portable Worker {index:03d}\n"
                    f"depends_on: {dependency}\n"
                    "result_schema:\n"
                    "  type: object\n"
                    "  properties:\n"
                    + fields
                    + "\n",
                    encoding="utf-8",
                )

            manager = SkillsManager()
            with (
                patch("skills.scanner.USER_SKILLS_BASE", base),
                patch("skills.manager.USER_SKILLS_BASE", base),
                patch("tools.skills.get_manager", return_value=manager),
            ):
                raw = asyncio.run(skill_view(
                    name="large-standard-skill",
                    user_id="u",
                    session_id="s",
                ))
                manifest_raw = asyncio.run(skill_view(
                    name="large-standard-skill",
                    file_path="__manifest__",
                    limit=512,
                    user_id="u",
                    session_id="s",
                ))
                full_runtime = manager.load_skill(
                    name="large-standard-skill",
                    user_id="u",
                    session_id="s",
                )

        payload = json.loads(raw)
        manifest_payload = json.loads(manifest_raw)
        self.assertLessEqual(len(raw), 48_000)
        self.assertLessEqual(len(manifest_raw), 48_000)
        self.assertTrue(payload["content"].rstrip().endswith(instruction_tail))
        self.assertTrue(payload["activation_envelope_compacted"])
        self.assertNotIn("workflow_contract", payload)
        self.assertNotIn("execution_contract", payload)
        self.assertEqual(
            64,
            len(payload["execution_contract_summary"]["sha256"]),
        )
        self.assertEqual(
            80,
            payload["execution_contract_summary"]["counts"]["workers"],
        )
        self.assertNotIn("workflow_contract", manifest_payload)
        self.assertNotIn("execution_contract", manifest_payload)
        self.assertEqual(
            payload["skill_md_sha256"],
            manifest_payload["skill_md_sha256"],
        )
        self.assertEqual(
            payload["skill_md_sha256"],
            full_runtime["skill_md_sha256"],
        )
        self.assertEqual(
            manifest_payload["linked_file_count"],
            manifest_payload["manifest_pagination"]["total_entries"],
        )
        self.assertEqual(
            80,
            len(full_runtime["execution_contract"]["workers"]),
        )

    def test_abnormally_long_resource_path_is_not_in_activation_sample(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = self._build_skill(base, count=0)
            nested = root / "references"
            for index in range(7):
                nested = nested / (f"segment-{index}-" + ("y" * 150))
            nested.mkdir(parents=True)
            target = nested / "resource.md"
            target.write_text("long path resource\n", encoding="utf-8")
            long_relative_path = str(target.relative_to(root))
            self.assertGreater(len(long_relative_path.encode("utf-8")), 1_024)

            manager = SkillsManager()
            viewed = self._view_main(base, manager)
            payload = viewed["payload"]
            manifest = manager._load_resource_manifest(
                root / "SKILL.md",
                root,
                "large-standard-skill",
                "s",
                offset=0,
                limit=128,
            )

        self.assertNotIn(long_relative_path, viewed["raw"])
        self.assertEqual(1, payload["linked_file_count"])
        self.assertEqual(0, payload["linked_files_returned_count"])
        self.assertEqual(1, payload["linked_files_omitted_count"])
        self.assertEqual(1, payload["linked_file_oversized_paths_omitted_count"])
        self.assertEqual(
            1,
            payload["linked_files_summary"]["references"][
                "oversized_paths_omitted"
            ],
        )
        self.assertEqual(
            [long_relative_path],
            manifest["linked_files"]["references"],
        )

    def test_large_main_document_reassembles_from_exact_contiguous_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = self._build_skill(base, count=0)
            body = "".join(
                f"instruction {index:05d}: \\\"portable\\\" 路径/δ\n"
                for index in range(5_000)
            )
            canonical = (
                "---\n"
                "name: large-standard-skill\n"
                "description: Execute a Skill whose canonical instructions require paging.\n"
                "---\n"
                + body
            )
            (root / "SKILL.md").write_text(canonical, encoding="utf-8")
            manager = SkillsManager()
            pages: list[str] = []
            offset: int | None = None
            with (
                patch("skills.scanner.USER_SKILLS_BASE", base),
                patch("skills.manager.USER_SKILLS_BASE", base),
                patch("tools.skills.get_manager", return_value=manager),
            ):
                first_raw = asyncio.run(skill_view(
                    name="large-standard-skill",
                    user_id="u",
                    session_id="s",
                ))
                first = json.loads(first_raw)
                pages.append(first["content"])
                offset = first["next_offset"]
                while offset is not None:
                    page_raw = asyncio.run(skill_view(
                        name="large-standard-skill",
                        file_path="SKILL.md",
                        offset=offset,
                        user_id="u",
                        session_id="s",
                    ))
                    self.assertLessEqual(len(page_raw), 48_000)
                    page = json.loads(page_raw)
                    pages.append(page["content"])
                    offset = page["next_offset"]

        self.assertLessEqual(len(first_raw), 48_000)
        self.assertTrue(first["main_document_paged"])
        self.assertEqual(canonical, "".join(pages))


if __name__ == "__main__":
    unittest.main()
