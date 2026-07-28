import tempfile
import unittest
from pathlib import Path

from knowledge_gate import (
    MAX_GATE_CHECKS,
    MAX_GATE_IDENTIFIER_CHARS,
    compile_symbolic_knowledge_gate,
)
from skills.loader import _normalize_worker_config


class SymbolicKnowledgeGateCompilerTests(unittest.TestCase):
    def _compile(self, root: Path, gate):
        return compile_symbolic_knowledge_gate(
            gate,
            skill_dir=root,
            source_file="orchestration/workers/researcher.yaml",
            worker_id="researcher",
        )

    @staticmethod
    def _error_codes(compilation) -> set[str]:
        return {
            diagnostic.code
            for diagnostic in compilation.diagnostics
            if diagnostic.level == "errors"
        }

    def test_legacy_flat_tools_form_one_conditional_group_and_close_resources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            references = root / "references"
            references.mkdir()
            (references / "guide.md").write_text("# guide", encoding="utf-8")
            (references / "alpha.txt").write_text("alpha", encoding="utf-8")
            (references / "beta.txt").write_text("beta", encoding="utf-8")
            (root / "NOTICE").write_text("notice", encoding="utf-8")

            compiled = self._compile(
                root,
                {
                    "checks": [{
                        "id": "source-ready",
                        "question": "Is the evidence source sufficient?",
                        "if_yes": None,
                        "if_no": "Acquire bounded supporting evidence.",
                        "tools": [
                            "skill:evidence-catalog",
                            "WebSearch",
                            "Bash(python:*)",
                            "references/guide.md",
                            "references/*.txt",
                            {
                                "name": "package-notice",
                                "source": "project",
                                "path": "NOTICE",
                            },
                        ],
                    }],
                },
            )

            self.assertTrue(compiled.ir["valid"])
            check = compiled.ir["checks"][0]
            self.assertTrue(check["legacy_ambiguous"])
            yes_branch, no_branch = check["branches"]
            self.assertEqual("yes", yes_branch["outcome"])
            self.assertEqual([], yes_branch["selector_groups"])
            self.assertEqual("no", no_branch["outcome"])
            self.assertEqual(1, len(no_branch["selector_groups"]))
            self.assertEqual(
                [
                    "skill:evidence-catalog",
                    "WebSearch",
                    "Bash(python:*)",
                    "references/guide.md",
                    "references/*.txt",
                    "NOTICE",
                ],
                no_branch["selector_groups"][0]["selectors"],
            )
            self.assertEqual(("evidence-catalog",), compiled.skill_refs)
            self.assertEqual(
                (
                    "references/guide.md",
                    "references/alpha.txt",
                    "references/beta.txt",
                    "NOTICE",
                ),
                compiled.local_resources,
            )
            self.assertEqual(
                ["references/alpha.txt", "references/beta.txt"],
                next(
                    row["resources"]
                    for row in compiled.ir["resource_expansions"]
                    if row["selector"] == "references/*.txt"
                ),
            )

    def test_narrowed_selectors_preserve_exact_command_grammar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            valid = self._compile(
                root,
                {
                    "checks": [{
                        "id": "safe-command",
                        "question": "Is the exact command adapter required?",
                        "if_no": "Run only the declared command adapter.",
                        "tools": ["Bash(python:*)"],
                    }]
                },
            )
            self.assertTrue(valid.ir["valid"])
            no_branch = next(
                branch
                for branch in valid.ir["checks"][0]["branches"]
                if branch["outcome"] == "no"
            )
            self.assertEqual(
                ["Bash(python:*)"],
                no_branch["selector_groups"][0]["selectors"],
            )

            invalid_selectors = (
                "Bash(/usr/bin/python:*)",
                "Bash(*:*)",
                "WebSearch(domain:foo)",
            )
            for index, selector in enumerate(invalid_selectors):
                with self.subTest(selector=selector):
                    invalid = self._compile(
                        root,
                        {
                            "checks": [{
                                "id": f"unsafe-policy-{index}",
                                "question": "Can this policy be preserved?",
                                "if_no": "Do not erase selector policy.",
                                "tools": [selector],
                            }]
                        },
                    )
                    self.assertFalse(invalid.ir["valid"])
                    self.assertIn(
                        "knowledge_gate_narrowed_selector_unsupported",
                        self._error_codes(invalid),
                    )
                    diagnostic = next(
                        item
                        for item in invalid.diagnostics
                        if item.code
                        == "knowledge_gate_narrowed_selector_unsupported"
                    )
                    self.assertEqual(
                        selector,
                        diagnostic.context["selector"],
                    )

    def test_branch_local_tools_override_inherited_tools_even_when_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            compiled = self._compile(
                Path(temp_dir),
                {
                    "checks": {
                        "freshness": {
                            "question": "Is the cached result fresh?",
                            "tools": {"any_of": ["CacheRead"]},
                            "if_yes": {
                                "action": "Continue without acquisition.",
                                "tool_groups": [],
                            },
                            "if_no": {
                                "instruction": "Refresh both evidence classes.",
                                "tools": {
                                    "all_of": [
                                        "SourceFetch",
                                        {"any_of": ["IndexA", "IndexB"]},
                                    ]
                                },
                            },
                            "if_unknown": "Inspect the existing cache.",
                        }
                    }
                },
            )

            self.assertTrue(compiled.ir["valid"])
            check = compiled.ir["checks"][0]
            self.assertFalse(check["legacy_ambiguous"])
            branches = {
                branch["outcome"]: branch for branch in check["branches"]
            }
            self.assertEqual([], branches["yes"]["selector_groups"])
            self.assertTrue(branches["yes"]["branch_local_tools"])
            self.assertEqual(
                [["SourceFetch"], ["IndexA", "IndexB"]],
                [
                    group["selectors"]
                    for group in branches["no"]["selector_groups"]
                ],
            )
            self.assertEqual(
                [["CacheRead"]],
                [
                    group["selectors"]
                    for group in branches["unknown"]["selector_groups"]
                ],
            )

    def test_explicit_tool_groups_are_and_of_or_groups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            compiled = self._compile(
                Path(temp_dir),
                {
                    "checks": [{
                        "id": "triangulation",
                        "question": "Has each evidence class been checked?",
                        "if_no": "Collect one result from every group.",
                        "tool_groups": [
                            ["CatalogA", "CatalogB"],
                            {
                                "id": "independent-source",
                                "mode": "one_of",
                                "selectors": ["ArchiveA", "ArchiveB"],
                            },
                        ],
                    }]
                },
            )

            self.assertTrue(compiled.ir["valid"])
            check = compiled.ir["checks"][0]
            self.assertFalse(check["legacy_ambiguous"])
            groups = check["branches"][0]["selector_groups"]
            self.assertEqual(
                [["CatalogA", "CatalogB"], ["ArchiveA", "ArchiveB"]],
                [group["selectors"] for group in groups],
            )
            self.assertEqual("independent-source", groups[1]["id"])

    def test_canonical_ids_are_unicode_aware_and_allow_leading_underscore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            longest_id = "_" + ("证" * (MAX_GATE_IDENTIFIER_CHARS - 1))
            compiled = compile_symbolic_knowledge_gate(
                {
                    "checks": [{
                        "id": longest_id,
                        "question": "证据是否充分？",
                        "if_no": "获取一个精确来源。",
                        "tool_groups": [{
                            "id": "_来源组",
                            "any_of": ["web_search"],
                        }],
                    }],
                },
                skill_dir=root,
                source_file="orchestration/workers/研究员.yaml",
                worker_id="_研究员",
            )

            self.assertTrue(compiled.ir["valid"])
            self.assertEqual(longest_id, compiled.ir["checks"][0]["id"])
            self.assertEqual(
                "_来源组",
                compiled.ir["checks"][0]["branches"][0][
                    "selector_groups"
                ][0]["id"],
            )

    def test_noncanonical_ids_fail_closed_at_symbolic_compile(self):
        invalid_ids = (
            "contains space",
            "unsafe@delimiter",
            "🧪",
            "e\u0301",
            "a" * (MAX_GATE_IDENTIFIER_CHARS + 1),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for invalid_id in invalid_ids:
                with self.subTest(invalid_id=invalid_id):
                    compiled = self._compile(
                        root,
                        {
                            "checks": [{
                                "id": invalid_id,
                                "question": "Is this ID canonical?",
                            }],
                        },
                    )
                    self.assertFalse(compiled.ir["valid"])
                    self.assertIn(
                        "knowledge_gate_check_id_invalid",
                        self._error_codes(compiled),
                    )

            invalid_worker = compile_symbolic_knowledge_gate(
                {"checks": [{"id": "valid", "question": "Valid?"}]},
                skill_dir=root,
                source_file="orchestration/workers/invalid.yaml",
                worker_id="invalid worker",
            )
            self.assertFalse(invalid_worker.ir["valid"])
            self.assertIn(
                "knowledge_gate_worker_id_invalid",
                self._error_codes(invalid_worker),
            )

    def test_invalid_declarations_fail_closed_with_stable_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            checks = [
                {
                    "id": "duplicate",
                    "question": "First?",
                    "if_no": "Read.",
                    "tools": ["../outside.md"],
                },
                {
                    "id": "DUPLICATE",
                    "question": "Second?",
                    "if_no": {
                        "action": "Run.",
                        "command": "unsafe extension",
                    },
                },
            ]
            checks.extend(
                {"id": f"bounded-{index}", "question": "Bounded?"}
                for index in range(MAX_GATE_CHECKS)
            )

            compiled = self._compile(root, {"checks": checks})
            codes = self._error_codes(compiled)

            self.assertFalse(compiled.ir["valid"])
            self.assertIn("knowledge_gate_check_limit_exceeded", codes)
            self.assertIn("duplicate_knowledge_gate_check_id", codes)
            self.assertIn("knowledge_gate_local_path_invalid", codes)
            # This branch lies beyond the duplicate check, so test an unknown
            # execution key independently from the bounded-prefix behavior.
            unknown = self._compile(
                root,
                {
                    "checks": [{
                        "id": "unknown-key",
                        "question": "Valid?",
                        "if_no": {
                            "action": "Inspect.",
                            "command": "not part of the portable grammar",
                        },
                    }]
                },
            )
            self.assertIn(
                "knowledge_gate_branch_key_unknown",
                self._error_codes(unknown),
            )
            self.assertFalse(unknown.ir["valid"])
            invalid_description = self._compile(
                root,
                {"description": 123, "checks": []},
            )
            self.assertIn(
                "knowledge_gate_text_type_invalid",
                self._error_codes(invalid_description),
            )
            self.assertFalse(invalid_description.ir["valid"])

    def test_missing_or_symlinked_glob_targets_never_enter_resource_closure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            references = root / "references"
            references.mkdir()
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("outside", encoding="utf-8")
            try:
                (references / "linked.txt").symlink_to(outside)
                compiled = self._compile(
                    root,
                    {
                        "checks": [{
                            "id": "local-only",
                            "question": "Is a local source available?",
                            "if_no": "Read the package source.",
                            "tools": ["references/*.txt"],
                        }]
                    },
                )
            finally:
                outside.unlink(missing_ok=True)

            self.assertFalse(compiled.ir["valid"])
            self.assertIn(
                "knowledge_gate_glob_no_matches",
                self._error_codes(compiled),
            )
            self.assertEqual((), compiled.local_resources)

    def test_loader_exposes_gate_only_skill_and_local_resource_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            references = root / "references"
            references.mkdir()
            (references / "one.md").write_text("one", encoding="utf-8")
            (references / "two.md").write_text("two", encoding="utf-8")
            gate = {
                "checks": [{
                    "id": "coverage",
                    "question": "Is coverage adequate?",
                    "if_no": "Acquire evidence.",
                    "tools": {
                        "any_of": [
                            "skill:evidence-catalog",
                            "skill:gate-only-helper",
                            "references/*.md",
                        ]
                    },
                }]
            }
            diagnostics = {"errors": [], "warnings": [], "info": []}

            worker = _normalize_worker_config(
                "researcher",
                {
                    "worker_id": "researcher",
                    "skills": ["existing-helper", "evidence-catalog"],
                    "knowledge_gate": gate,
                },
                skill_dir=root,
                file_path="orchestration/workers/researcher.yaml",
                diagnostics=diagnostics,
                source_file="orchestration/workers/researcher.yaml",
            )

            self.assertEqual([], diagnostics["errors"])
            self.assertEqual(gate, worker["knowledge_gate"])
            self.assertEqual(
                [
                    "existing-helper",
                    "evidence-catalog",
                    "skill:gate-only-helper",
                ],
                worker["skills"],
            )
            self.assertEqual(
                ["references/one.md", "references/two.md"],
                worker["local_resources"],
            )
            self.assertEqual(
                ["evidence-catalog", "gate-only-helper"],
                worker["knowledge_gate_skill_refs"],
            )
            self.assertTrue(worker["knowledge_gate_ir"]["valid"])
            self.assertEqual(
                ["coverage"],
                worker["required_gate_ids"],
            )

    def test_loader_preserves_all_compiler_admitted_gate_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            for check_count in (81, MAX_GATE_CHECKS):
                with self.subTest(check_count=check_count):
                    diagnostics = {"errors": [], "warnings": [], "info": []}
                    worker = _normalize_worker_config(
                        "bounded-worker",
                        {
                            "worker_id": "bounded-worker",
                            "knowledge_gate": {
                                "checks": [
                                    {
                                        "id": f"check-{index}",
                                        "question": f"Is check {index} satisfied?",
                                    }
                                    for index in range(check_count)
                                ],
                            },
                        },
                        skill_dir=root,
                        file_path="orchestration/workers/bounded-worker.yaml",
                        diagnostics=diagnostics,
                        source_file="orchestration/workers/bounded-worker.yaml",
                    )

                    self.assertEqual([], diagnostics["errors"])
                    self.assertTrue(worker["knowledge_gate_ir"]["valid"])
                    self.assertEqual(
                        [f"check-{index}" for index in range(check_count)],
                        worker["required_gate_ids"],
                    )

    def test_compiler_rejects_129th_gate_and_loader_keeps_bounded_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            gate = {
                "checks": [
                    {
                        "id": f"check-{index}",
                        "question": f"Is check {index} satisfied?",
                    }
                    for index in range(MAX_GATE_CHECKS + 1)
                ],
            }

            compiled = self._compile(root, gate)
            self.assertFalse(compiled.ir["valid"])
            self.assertIn(
                "knowledge_gate_check_limit_exceeded",
                self._error_codes(compiled),
            )
            self.assertEqual(MAX_GATE_CHECKS, len(compiled.ir["checks"]))

            diagnostics = {"errors": [], "warnings": [], "info": []}
            worker = _normalize_worker_config(
                "bounded-worker",
                {
                    "worker_id": "bounded-worker",
                    "knowledge_gate": gate,
                },
                skill_dir=root,
                file_path="orchestration/workers/bounded-worker.yaml",
                diagnostics=diagnostics,
                source_file="orchestration/workers/bounded-worker.yaml",
            )

            self.assertFalse(worker["knowledge_gate_ir"]["valid"])
            self.assertEqual(
                [f"check-{index}" for index in range(MAX_GATE_CHECKS)],
                worker["required_gate_ids"],
            )
            self.assertIn(
                "knowledge_gate_check_limit_exceeded",
                {
                    diagnostic["code"]
                    for diagnostic in diagnostics["errors"]
                },
            )


if __name__ == "__main__":
    unittest.main()
