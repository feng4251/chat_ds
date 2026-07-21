import tempfile
import unittest
from pathlib import Path

from skills.loader import load_skill_content


class SkillTemplateSubstitutionTests(unittest.TestCase):
    @staticmethod
    def _load(source: str, *, session_id: str = "session-42") -> tuple[Path, dict]:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name) / "template-fixture"
        root.mkdir()
        (root / "SKILL.md").write_text(source, encoding="utf-8")
        loaded = load_skill_content(
            root / "SKILL.md",
            skill_dir=str(root),
            session_id=session_id,
        )
        # Keep the directory alive for assertions that inspect its absolute path.
        loaded["_test_temp_dir"] = temp_dir
        return root, loaded

    def test_standard_skill_body_preserves_template_tokens_literally(self):
        body = (
            "# Tutorial\n\n"
            "In a shell script, write `cd ${SKILL_DIR}`.\n"
            "The placeholder `${SESSION_ID}` is part of this example.\n"
        )
        root, loaded = self._load(
            "---\n"
            "name: template-fixture\n"
            "description: Preserve tutorial examples as literal Markdown.\n"
            "---\n"
            + body
        )

        self.assertNotIn("error", loaded)
        self.assertEqual(body, loaded["content"])
        self.assertNotIn(str(root), loaded["content"])

    def test_versioned_namespaced_body_template_is_an_explicit_opt_in(self):
        root, loaded = self._load(
            "---\n"
            "name: template-fixture\n"
            "description: Exercise explicit harness body templating.\n"
            "metadata:\n"
            "  hermes:\n"
            "    body_template:\n"
            "      schema_version: 1\n"
            "      enabled: true\n"
            "---\n"
            "root=${SKILL_DIR}\nsession=${SESSION_ID}\n"
        )

        self.assertNotIn("error", loaded)
        self.assertEqual(
            f"root={root.resolve()}\nsession=session-42\n",
            loaded["content"],
        )

    def test_runtime_config_fields_expand_without_templating_the_body(self):
        root, loaded = self._load(
            "---\n"
            "name: template-fixture\n"
            "description: Resolve declared MCP runtime configuration.\n"
            "mcp_servers:\n"
            "  - name: helper\n"
            "    command: ${SKILL_DIR}/bin/server\n"
            "    args:\n"
            "      - --session=${SESSION_ID}\n"
            "      - ${SKILL_DIR}/config.json\n"
            "---\n"
            "Document `${SKILL_DIR}` and `${SESSION_ID}` literally.\n"
        )

        self.assertNotIn("error", loaded)
        self.assertEqual(
            "Document `${SKILL_DIR}` and `${SESSION_ID}` literally.\n",
            loaded["content"],
        )
        self.assertEqual(
            {
                "name": "helper",
                "command": f"{root.resolve()}/bin/server",
                "args": [
                    "--session=session-42",
                    f"{root.resolve()}/config.json",
                ],
            },
            loaded["mcp_servers"][0],
        )


if __name__ == "__main__":
    unittest.main()
