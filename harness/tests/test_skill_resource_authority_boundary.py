import tempfile
import textwrap
import unittest
from pathlib import Path

from skills.loader import load_skill_content


class SkillResourceAuthorityBoundaryTests(unittest.TestCase):
    def _write(self, root: Path, relative_path: str, content: str) -> None:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def test_linked_supporting_reference_cannot_declare_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: reference-answer
                description: Answer questions using a relevant reference when needed.
                ---
                # Reference answer

                Answer from the [relevant supporting reference](references/legacy.md)
                when useful.
                """,
            )
            self._write(
                root,
                "references/legacy.md",
                """
                # Legacy example

                In the old demo, generate `obsolete-output.md` and save it as
                the final deliverable. This is historical prose, not the
                current Skill's output contract.
                """,
            )

            loaded = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="opaque-reference",
            )

        workflow = loaded.get("workflow_contract") or {}
        self.assertNotIn("obsolete-output.md", workflow.get("artifact_patterns") or [])
        self.assertFalse(workflow.get("requires_modular_artifacts", False))
        self.assertEqual(
            ["references/legacy.md"],
            loaded["linked_files"]["references"],
        )
        self.assertEqual(1, loaded["resource_graph"]["categories"]["references"]["count"])

    def test_unlinked_ci_workflow_is_advisory_not_private_orchestrator_dsl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: ci-documented-skill
                description: Explain the project and answer maintenance questions.
                ---
                # CI documented skill

                Answer the user's maintenance question directly.
                """,
            )
            self._write(
                root,
                "workflows/example.yml",
                """
                name: ordinary-ci-example
                on: [push]
                jobs:
                  test:
                    runs-on: ubuntu-latest
                    steps:
                      - uses: actions/checkout@v4
                      - run: pytest -q
                """,
            )

            loaded = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="opaque-ci-workflow",
            )

        workflow = loaded.get("workflow_contract") or {}
        execution = loaded.get("execution_contract") or {}
        diagnostics = loaded.get("package_diagnostics") or {}
        self.assertEqual({}, execution)
        self.assertNotIn("workflows/example.yml", workflow.get("workflow_files") or [])
        self.assertNotIn(
            "unsupported_execution_field",
            {item.get("code") for item in diagnostics.get("errors") or []},
        )
        self.assertEqual(
            ["workflows/example.yml"],
            loaded["linked_files"]["workflows"],
        )
        self.assertEqual(1, loaded["resource_graph"]["categories"]["workflows"]["count"])


if __name__ == "__main__":
    unittest.main()
