from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime import python_env
from tools.isolated_skill_executor import (
    IsolatedSkillExecutorError,
    build_process_lease_open_request,
    create_process_owner_scope,
    snapshot_skill_package,
)
from tools.skill_runtime_profile import (
    select_skill_runtime_profile,
)


class SkillRuntimeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "skill"
        self.workspace = Path(self.tempdir.name) / "workspace"
        (self.root / "scripts").mkdir(parents=True)
        self.workspace.mkdir()
        (self.root / "SKILL.md").write_text(
            "---\nname: profile-fixture\n---\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_mixed_routes_follow_only_exact_reachable_source_closure(self) -> None:
        self.write(
            "scripts/base.cjs",
            'require("./base_helper.cjs");\n',
        )
        self.write(
            "scripts/base_helper.cjs",
            'const readline = require("readline");\n'
            "module.exports = readline;\n",
        )
        self.write(
            "scripts/browser.cjs",
            'const { chromium } = require("playwright");\n'
            "module.exports = chromium;\n",
        )
        snapshot = snapshot_skill_package(self.root)

        base = select_skill_runtime_profile(
            snapshot, "scripts/base.cjs"
        )
        browser = select_skill_runtime_profile(
            snapshot, "scripts/browser.cjs"
        )

        self.assertEqual("base-v1", base.runtime_profile)
        self.assertEqual(
            ("scripts/base.cjs", "scripts/base_helper.cjs"),
            base.reachable_sources,
        )
        self.assertEqual(
            "browser-automation-v1",
            browser.runtime_profile,
        )
        self.assertEqual(("playwright",), browser.runtime_node_packages)

    def test_side_effect_esm_import_routes_to_browser_profile(self) -> None:
        self.write(
            "scripts/browser.mjs",
            'import "playwright" assert { type: "javascript" };\n',
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/browser.mjs"
        )

        self.assertEqual(
            "browser-automation-v1",
            selection.runtime_profile,
        )
        self.assertEqual(
            ("playwright",),
            selection.runtime_node_packages,
        )

    def test_unfixed_package_json_dependency_fails_before_runtime(self) -> None:
        self.write(
            "scripts/run.cjs",
            'const axios = require("axios");\nconsole.log(axios);\n',
        )
        self.write(
            "package.json",
            json.dumps({"dependencies": {"axios": "1.0.0"}}),
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(snapshot, "scripts/run.cjs")

        self.assertEqual(
            "skill_runtime_dependency_unsupported",
            raised.exception.code,
        )

    def test_vendored_node_dependency_closure_is_snapshot_bound(self) -> None:
        self.write(
            "scripts/run.cjs",
            'const cheerio = require("cheerio");\nconsole.log(cheerio);\n',
        )
        self.write(
            "package.json",
            json.dumps({"dependencies": {"cheerio": "1.0.0"}}),
        )
        self.write(
            "node_modules/cheerio/package.json",
            json.dumps({
                "name": "cheerio",
                "version": "1.0.0",
                "main": "index.js",
                "dependencies": {},
            }),
        )
        self.write("node_modules/cheerio/index.js", "module.exports = {};\n")
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.cjs"
        )

        self.assertEqual("base-v1", selection.runtime_profile)
        self.assertEqual(("cheerio",), selection.runtime_node_packages)

    def test_unrelated_package_declarations_do_not_escalate_or_block(self) -> None:
        self.write(
            "scripts/base.cjs",
            'const fs = require("fs");\nmodule.exports = fs;\n',
        )
        self.write(
            "scripts/base.py",
            "import json\nprint(json.dumps({}))\n",
        )
        self.write(
            "package.json",
            json.dumps({
                "dependencies": {
                    "playwright": "^1.62.0",
                    "axios": "^1.0.0",
                },
            }),
        )
        self.write(
            "requirements.txt",
            "selenium>=99\n",
        )
        snapshot = snapshot_skill_package(self.root)

        node = select_skill_runtime_profile(
            snapshot, "scripts/base.cjs"
        )
        python = select_skill_runtime_profile(
            snapshot, "scripts/base.py"
        )

        self.assertEqual("base-v1", node.runtime_profile)
        self.assertEqual((), node.runtime_node_packages)
        self.assertEqual("base-v1", python.runtime_profile)
        self.assertEqual((), python.runtime_requirements)

    def test_fixed_node_version_must_satisfy_reachable_declaration(self) -> None:
        self.write(
            "scripts/browser.cjs",
            'require("playwright");\n',
        )
        self.write(
            "package.json",
            json.dumps({"dependencies": {"playwright": "^1.62.0"}}),
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/browser.cjs"
            )

        self.assertEqual(
            "skill_runtime_dependency_declaration_unsupported",
            raised.exception.code,
        )

    def test_python_requirement_version_contract_is_preserved(self) -> None:
        self.write(
            "scripts/browser.py",
            "from selenium import webdriver\n",
        )
        self.write(
            "requirements.txt",
            'selenium[websocket]>=4.40; python_version >= "3.12"\n',
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/browser.py"
        )

        self.assertEqual(
            "browser-automation-v1",
            selection.runtime_profile,
        )
        self.assertEqual(
            (
                'selenium[websocket]>=4.40; '
                'python_version >= "3.12"',
            ),
            selection.runtime_requirements,
        )

    def test_dynamic_node_dependency_requires_exact_entrypoint_marker(self) -> None:
        self.write(
            "scripts/dynamic.cjs",
            'const name = "playwright";\nrequire(name);\n',
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/dynamic.cjs"
            )

        self.assertEqual(
            "skill_runtime_dynamic_dependency_unsupported",
            raised.exception.code,
        )

    def test_dynamic_node_marker_validates_all_declared_dependencies(self) -> None:
        self.write(
            "scripts/dynamic.cjs",
            "// CHATDS_RUNTIME_PROFILE=browser-automation-v1\n"
            'const name = "playwright";\nrequire(name);\n',
        )
        self.write(
            "package.json",
            json.dumps({"dependencies": {"playwright": "^1.60.0"}}),
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/dynamic.cjs"
        )

        self.assertEqual(
            "browser-automation-v1",
            selection.runtime_profile,
        )
        self.assertEqual(
            ("playwright",),
            selection.runtime_node_packages,
        )

    def test_dynamic_python_import_requires_exact_entrypoint_marker(self) -> None:
        self.write(
            "scripts/dynamic.py",
            "import importlib\n"
            'module_name = "selenium"\n'
            "importlib.import_module(module_name)\n",
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/dynamic.py"
            )

        self.assertEqual(
            "skill_runtime_dynamic_dependency_unsupported",
            raised.exception.code,
        )

    def test_dynamic_python_marker_proves_declared_requirements(self) -> None:
        self.write(
            "scripts/dynamic.py",
            "# CHATDS_RUNTIME_PROFILE=browser-automation-v1\n"
            "from importlib import import_module\n"
            'module_name = "selenium"\n'
            "import_module(module_name)\n",
        )
        self.write(
            "requirements.txt",
            "selenium[websocket]==4.40.0\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/dynamic.py"
        )

        self.assertEqual(
            "browser-automation-v1",
            selection.runtime_profile,
        )
        self.assertEqual(
            ("selenium[websocket]==4.40.0",),
            selection.runtime_requirements,
        )

    def test_literal_dynamic_python_import_routes_without_marker(self) -> None:
        self.write(
            "scripts/dynamic.py",
            "from importlib import import_module\n"
            'import_module("selenium.webdriver")\n',
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/dynamic.py"
        )

        self.assertEqual(
            "browser-automation-v1",
            selection.runtime_profile,
        )
        self.assertEqual(("selenium",), selection.runtime_requirements)

    def test_shell_literal_variable_script_enters_reachable_closure(self) -> None:
        self.write(
            "scripts/run.sh",
            "script=scripts/browser.py; python \"$script\"\n",
        )
        self.write(
            "scripts/browser.py",
            "from selenium import webdriver\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            "browser-automation-v1",
            selection.runtime_profile,
        )
        self.assertEqual(
            ("scripts/browser.py", "scripts/run.sh"),
            selection.reachable_sources,
        )
        self.assertEqual(
            ("bash", "python"),
            selection.runtime_commands,
        )
        self.assertEqual("skill", selection.required_cwd)

    def test_shell_unknown_script_and_eval_require_marker(self) -> None:
        for source in (
            'python "$script"\n',
            'source "$helper"\n',
            'eval "$generated"\n',
            'python -c "print(1)"\n',
            'result=$(curl)\n',
            "coproc FETCH curl https://example.test\n",
        ):
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.sh"
                    )
                self.assertEqual(
                    "skill_runtime_dynamic_dependency_unsupported",
                    raised.exception.code,
                )

    def test_missing_literal_code_paths_fail_closed(self) -> None:
        cases = {
            "scripts/run.sh": "source scripts/missing.sh\n",
            "scripts/run.py": (
                "import runpy\n"
                'runpy.run_path("scripts/missing.py")\n'
            ),
            "scripts/subprocess.py": (
                "import subprocess\n"
                'subprocess.run("scripts/missing.py")\n'
            ),
        }
        for entrypoint, source in cases.items():
            with self.subTest(entrypoint=entrypoint):
                self.write(entrypoint, source)
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, entrypoint
                    )
                self.assertEqual(
                    "skill_runtime_dynamic_dependency_unsupported",
                    raised.exception.code,
                )

    def test_shell_data_variables_are_not_code_dispatch(self) -> None:
        self.write(
            "scripts/run.sh",
            'URL="https://example.test/data"\n'
            "count=$((1 << 2))\n"
            'curl "$URL" | jq .\n'
            "printf '%s' '$(literal-data)'\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual("base-v1", selection.runtime_profile)
        self.assertEqual(
            ("bash", "curl", "jq"),
            selection.runtime_commands,
        )

    def test_shell_control_words_are_not_runtime_commands(self) -> None:
        self.write(
            "scripts/run.sh",
            "if command -v curl; then\n"
            "  for url in one two; do\n"
            '    curl "$url"\n'
            "  done\n"
            "fi\n"
            "fetch() { curl https://example.test; }\n"
            "fetch\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            ("bash", "curl"),
            selection.runtime_commands,
        )

    def test_data_heredoc_body_is_not_parsed_as_commands(self) -> None:
        self.write(
            "scripts/run.sh",
            "cat > output.txt <<'EOF'\n"
            "hello world\n"
            "$(literal inert because delimiter is quoted)\n"
            "EOF\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            ("bash", "cat"),
            selection.runtime_commands,
        )

    def test_multiple_and_tab_stripped_heredocs_preserve_header_commands(
        self,
    ) -> None:
        self.write(
            "scripts/run.sh",
            "cat 2<<ONE <<-TWO; jq --version\n"
            "first body\n"
            "ONE\n"
            "\tsecond body\n"
            "\tTWO\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            ("bash", "cat", "jq"),
            selection.runtime_commands,
        )

    def test_interpreter_and_expanding_heredocs_require_marker(self) -> None:
        for source in (
            "python <<'PY'\n"
            "from selenium import webdriver\n"
            "PY\n",
            "node <<'JS'\n"
            "require('playwright')\n"
            "JS\n",
            "bash -s <<'SH'\n"
            "curl https://example.test\n"
            "SH\n",
            "cat <<EOF\n"
            "$(python scripts/generated.py)\n"
            "EOF\n",
        ):
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.sh"
                    )
                self.assertEqual(
                    "skill_runtime_dynamic_dependency_unsupported",
                    raised.exception.code,
                )

    def test_resolved_variable_interpreter_heredoc_requires_marker(
        self,
    ) -> None:
        self.write(
            "scripts/run.sh",
            "interp=python\n"
            '"$interp" <<\'PY\'\n'
            "from selenium import webdriver\n"
            "PY\n",
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.sh"
            )

        self.assertEqual(
            "skill_runtime_dynamic_dependency_unsupported",
            raised.exception.code,
        )

    def test_interpreter_local_script_heredoc_is_data(self) -> None:
        self.write(
            "scripts/run.sh",
            "python scripts/known.py <<'PYDATA'\n"
            "python data, not source\n"
            "PYDATA\n"
            "node scripts/known.js <<'JSDATA'\n"
            "javascript data, not source\n"
            "JSDATA\n"
            "bash scripts/known.sh <<'SHDATA'\n"
            "shell data, not source\n"
            "SHDATA\n",
        )
        self.write("scripts/known.py", "print(input())\n")
        self.write("scripts/known.js", "process.stdin.resume();\n")
        self.write("scripts/known.sh", "read -r value\n")
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            (
                "scripts/known.js",
                "scripts/known.py",
                "scripts/known.sh",
                "scripts/run.sh",
            ),
            selection.reachable_sources,
        )
        self.assertEqual(
            ("bash", "node", "python"),
            selection.runtime_commands,
        )

    def test_stdin_dispatch_consumer_requires_marker(self) -> None:
        for source in (
            "xargs python <<'EOF'\nscripts/generated.py\nEOF\n",
            "cat <<'EOF' | xargs python\nscripts/generated.py\nEOF\n",
        ):
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.sh"
                    )
                self.assertEqual(
                    "skill_runtime_dynamic_dependency_unsupported",
                    raised.exception.code,
                )

    def test_unsafe_or_unterminated_heredoc_fails_closed(self) -> None:
        cases = (
            "cat <<$DELIMITER\nbody\n$DELIMITER\n",
            "cat <<EOF\nunterminated\n",
        )
        for source in cases:
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.sh"
                    )
                self.assertIn(
                    raised.exception.code,
                    {
                        "skill_runtime_heredoc_unsupported",
                        "skill_runtime_heredoc_limit",
                    },
                )

    def test_here_string_is_not_reparsed_as_heredoc(self) -> None:
        for source in (
            "grep x <<< 'x'\n",
            "grep x \\<<< 'x'\n",
        ):
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                selection = select_skill_runtime_profile(
                    snapshot, "scripts/run.sh"
                )
                self.assertEqual(
                    ("bash", "grep"),
                    selection.runtime_commands,
                )

    def test_interpreter_here_string_requires_marker(self) -> None:
        for source in (
            "python <<< 'from selenium import webdriver'\n",
            "cat <<< 'from selenium import webdriver' | python -\n",
            "cat <<< 'from selenium import webdriver' |& python -\n",
            "cat <<< 'from selenium import webdriver' "
            "2>&1 | python -\n",
            "bash <<< 'curl https://example.test'\n",
        ):
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.sh"
                    )
                self.assertEqual(
                    "skill_runtime_dynamic_dependency_unsupported",
                    raised.exception.code,
                )

    def test_continued_logical_lines_preserve_commands_and_heredoc_header(
        self,
    ) -> None:
        cases = (
            (
                "curl \\\n https://example.test\n",
                ("bash", "curl"),
            ),
            (
                "cat \\\n <<EOF\nbody\nEOF\n",
                ("bash", "cat"),
            ),
            (
                "cat <<EOF \\\n | jq .\n{}\nEOF\n",
                ("bash", "cat", "jq"),
            ),
        )
        for source, commands in cases:
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                selection = select_skill_runtime_profile(
                    snapshot, "scripts/run.sh"
                )
                self.assertEqual(commands, selection.runtime_commands)

    def test_implicit_logical_continuations_preserve_commands(self) -> None:
        cases = (
            (
                "cat input.json |\n"
                " jq .\n",
                ("bash", "cat", "jq"),
            ),
            (
                "curl https://example.test &&\n"
                " jq .\n",
                ("bash", "curl", "jq"),
            ),
            (
                "curl https://example.test ||\n"
                " jq .\n",
                ("bash", "curl", "jq"),
            ),
            (
                'cat <<EOF "arg\n'
                'continued"\n'
                "body\n"
                "EOF\n",
                ("bash", "cat"),
            ),
        )
        for source, commands in cases:
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                selection = select_skill_runtime_profile(
                    snapshot, "scripts/run.sh"
                )
                self.assertEqual(commands, selection.runtime_commands)

    def test_heredoc_dangling_pipeline_fails_closed(self) -> None:
        self.write(
            "scripts/run.sh",
            "cat <<EOF |\n"
            " jq .\n"
            "{}\n"
            "EOF\n",
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.sh"
            )

        self.assertEqual(
            "skill_runtime_shell_parse_unsupported",
            raised.exception.code,
        )

    def test_comment_backslash_does_not_continue_logical_line(self) -> None:
        self.write(
            "scripts/run.sh",
            "printf ok # comment \\\n"
            "cat <<EOF\n"
            "body\n"
            "EOF\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(("bash", "cat"), selection.runtime_commands)

    def test_crlf_heredoc_and_comment_boundary(self) -> None:
        self.write(
            "scripts/run.sh",
            "cat <<EOF\r\nhello\r\nEOF\r\n"
            "printf ok;# ignored <<FAKE\r\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(("bash", "cat"), selection.runtime_commands)

    def test_heredoc_pipeline_to_interpreter_requires_marker(self) -> None:
        for source in (
            "cat <<'PY' | python -\n"
            "from selenium import webdriver\n"
            "PY\n",
            "cat <<'PY' |& python -\n"
            "from selenium import webdriver\n"
            "PY\n",
            "cat <<'PY' 2>&1 | python -\n"
            "from selenium import webdriver\n"
            "PY\n",
            "cat <<'SH' | bash\n"
            "curl https://example.test\n"
            "SH\n",
        ):
            with self.subTest(source=source):
                self.write("scripts/run.sh", source)
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.sh"
                    )
                self.assertEqual(
                    "skill_runtime_dynamic_dependency_unsupported",
                    raised.exception.code,
                )

    def test_upstream_interpreter_does_not_receive_downstream_heredoc(
        self,
    ) -> None:
        self.write(
            "scripts/run.sh",
            "python scripts/known.py | cat <<'EOF'\n"
            "data\n"
            "EOF\n",
        )
        self.write("scripts/known.py", "print('known')\n")
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            ("scripts/known.py", "scripts/run.sh"),
            selection.reachable_sources,
        )
        self.assertEqual(
            ("bash", "cat", "python"),
            selection.runtime_commands,
        )

    def test_unquoted_heredoc_uses_heredoc_expansion_rules(self) -> None:
        for body in (
            "'$(python scripts/generated.py)'",
            "# $(python scripts/generated.py)",
            "$((value[index]))",
        ):
            with self.subTest(body=body):
                self.write(
                    "scripts/run.sh",
                    f"cat <<EOF\n{body}\nEOF\n",
                )
                snapshot = snapshot_skill_package(self.root)
                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.sh"
                    )
                self.assertEqual(
                    "skill_runtime_dynamic_dependency_unsupported",
                    raised.exception.code,
                )

        self.write(
            "scripts/run.sh",
            "cat <<EOF\n"
            "\\$(literal data)\n"
            "EOF\n",
        )
        snapshot = snapshot_skill_package(self.root)
        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )
        self.assertEqual(("bash", "cat"), selection.runtime_commands)

        self.write(
            "scripts/run.sh",
            "cat <<EOF\n"
            "<(printf PROCESS >&2)\n"
            "EOF\n",
        )
        snapshot = snapshot_skill_package(self.root)
        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )
        self.assertEqual(("bash", "cat"), selection.runtime_commands)

    def test_invalid_contiguous_input_operator_fails_closed(self) -> None:
        self.write("scripts/run.sh", "grep x <<<< 'x'\n")
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.sh"
            )

        self.assertEqual(
            "skill_runtime_heredoc_unsupported",
            raised.exception.code,
        )

    def test_multiline_quote_is_folded_before_statement_analysis(self) -> None:
        self.write(
            "scripts/run.sh",
            'curl "https://example.test/\ncontinued"\n',
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            ("bash", "curl"),
            selection.runtime_commands,
        )

    def test_unterminated_multiline_quote_fails_closed(self) -> None:
        self.write(
            "scripts/run.sh",
            'curl "https://example.test/\n',
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.sh"
            )

        self.assertEqual(
            "skill_runtime_shell_parse_unsupported",
            raised.exception.code,
        )

    def test_command_substitution_honors_shell_comment_boundaries(
        self,
    ) -> None:
        self.write(
            "scripts/run.sh",
            "printf ok;# $(python scripts/not_executed.py)\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(("bash",), selection.runtime_commands)

        self.write(
            "scripts/run.sh",
            "printf '%s' foo#$(python scripts/executed.py)\n",
        )
        snapshot = snapshot_skill_package(self.root)
        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.sh"
            )
        self.assertEqual(
            "skill_runtime_dynamic_dependency_unsupported",
            raised.exception.code,
        )

    def test_local_script_heads_are_closure_not_external_commands(self) -> None:
        self.write(
            "scripts/run.sh",
            '"$CHATDS_SKILL_DIR/scripts/helper.sh"\n',
        )
        self.write(
            "scripts/helper.sh",
            "#!/bin/bash\n"
            "printf '%s\\n' ok\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertEqual(
            ("scripts/helper.sh", "scripts/run.sh"),
            selection.reachable_sources,
        )
        self.assertEqual(("bash",), selection.runtime_commands)
        self.assertIsNone(selection.required_cwd)

    def test_direct_local_script_requires_compatible_shebang(self) -> None:
        self.write(
            "scripts/run.sh",
            "# CHATDS_RUNTIME_PROFILE=base-v1\n"
            '"$CHATDS_SKILL_DIR/scripts/helper.sh"\n',
        )
        self.write(
            "scripts/helper.sh",
            "printf '%s\\n' ok\n",
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.sh"
            )

        self.assertEqual(
            "skill_runtime_direct_entrypoint_unsupported",
            raised.exception.code,
        )

    def test_direct_local_script_rejects_unattested_shebang_path(self) -> None:
        for shebang in (
            "#!/nonexistent/python3",
            "#!/usr/bin/python3",
        ):
            with self.subTest(shebang=shebang):
                self.write(
                    "scripts/run.py",
                    "import subprocess\n"
                    'subprocess.run(["scripts/helper.py"], check=True)\n',
                )
                self.write(
                    "scripts/helper.py",
                    shebang + "\n"
                    "print('unreachable')\n",
                )
                snapshot = snapshot_skill_package(self.root)

                with self.assertRaises(
                    IsolatedSkillExecutorError
                ) as raised:
                    select_skill_runtime_profile(
                        snapshot, "scripts/run.py"
                    )

                self.assertEqual(
                    "skill_runtime_direct_entrypoint_unsupported",
                    raised.exception.code,
                )

    def test_python_subprocess_local_script_is_closure_not_command(self) -> None:
        self.write(
            "scripts/run.py",
            "import subprocess\n"
            'subprocess.run(["scripts/browser.py"], check=True)\n',
        )
        self.write(
            "scripts/browser.py",
            "#!/usr/bin/env python3\n"
            "from selenium import webdriver\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.py"
        )

        self.assertEqual(
            "browser-automation-v1",
            selection.runtime_profile,
        )
        self.assertEqual(
            ("scripts/browser.py", "scripts/run.py"),
            selection.reachable_sources,
        )
        self.assertNotIn("browser.py", selection.runtime_commands)
        self.assertEqual("skill", selection.required_cwd)

    def test_python_subprocess_string_local_script_is_closure(self) -> None:
        self.write(
            "scripts/run.py",
            "import subprocess\n"
            'subprocess.run("scripts/browser.py", check=True)\n',
        )
        self.write(
            "scripts/browser.py",
            "#!/usr/bin/env python3\n"
            "from selenium import webdriver\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.py"
        )

        self.assertEqual(
            ("scripts/browser.py", "scripts/run.py"),
            selection.reachable_sources,
        )
        self.assertNotIn("browser.py", selection.runtime_commands)
        self.assertEqual("skill", selection.required_cwd)

    def test_nested_helper_dispatch_uses_entrypoint_script_cwd(self) -> None:
        self.write(
            "scripts/run.sh",
            "source \"$SKILL_DIR/helpers/helper.sh\"\n",
        )
        self.write(
            "helpers/helper.sh",
            "bash ./child.sh\n",
        )
        self.write("scripts/child.sh", "printf entrypoint-child\n")
        self.write("helpers/child.sh", "printf helper-child\n")
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.sh"
        )

        self.assertIn("scripts/child.sh", selection.reachable_sources)
        self.assertNotIn("helpers/child.sh", selection.reachable_sources)
        self.assertEqual("script", selection.required_cwd)

    def test_cwd_mutation_with_relative_dispatch_fails_closed(self) -> None:
        self.write(
            "scripts/run.sh",
            "cd helpers\n"
            "python scripts/browser.py\n",
        )
        self.write(
            "scripts/browser.py",
            "from selenium import webdriver\n",
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.sh"
            )

        self.assertEqual(
            "skill_runtime_cwd_mutation_unsupported",
            raised.exception.code,
        )

    def test_node_child_process_dispatch_requires_marker(self) -> None:
        self.write(
            "scripts/run.cjs",
            'const { spawn } = require("child_process");\n'
            'spawn("python", ["scripts/browser.py"]);\n',
        )
        self.write(
            "scripts/browser.py",
            "from selenium import webdriver\n",
        )
        snapshot = snapshot_skill_package(self.root)

        with self.assertRaises(IsolatedSkillExecutorError) as raised:
            select_skill_runtime_profile(
                snapshot, "scripts/run.cjs"
            )

        self.assertEqual(
            "skill_runtime_dynamic_dependency_unsupported",
            raised.exception.code,
        )

    def test_python_subprocess_data_argument_is_not_dynamic_dispatch(self) -> None:
        self.write(
            "scripts/run.py",
            "import subprocess\n"
            "url = get_url()\n"
            'subprocess.run(["curl", url], check=True)\n',
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.py"
        )

        self.assertEqual("base-v1", selection.runtime_profile)
        self.assertIn("curl", selection.runtime_commands)

    def test_reachable_import_uses_declared_distribution_alias(self) -> None:
        self.write(
            "scripts/run.py",
            "import bs4\n",
        )
        self.write(
            "requirements.txt",
            "beautifulsoup4==4.13.4\n",
        )
        snapshot = snapshot_skill_package(self.root)

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.py"
        )

        self.assertEqual(
            ("beautifulsoup4==4.13.4",),
            selection.runtime_requirements,
        )

    def test_live_mutation_cannot_change_profile_or_process_open_bytes(self) -> None:
        script = self.write(
            "scripts/run.cjs",
            'const readline = require("readline");\n',
        )
        snapshot = snapshot_skill_package(self.root)
        original_digest = snapshot.file_sha256("scripts/run.cjs")
        script.write_text(
            'const { chromium } = require("playwright");\n',
            encoding="utf-8",
        )

        selection = select_skill_runtime_profile(
            snapshot, "scripts/run.cjs"
        )
        self.assertEqual("base-v1", selection.runtime_profile)
        with patch.dict(
            os.environ,
            {"EXECUTOR_V2_AUTH_TOKEN": "x" * 64},
        ):
            payload, _encoded = build_process_lease_open_request(
                owner_scope=create_process_owner_scope(
                    user_id="u",
                    session_id="s",
                    root_run_id="r",
                ),
                skill_root=self.root,
                skill_snapshot=snapshot,
                workspace=self.workspace,
                entrypoint="scripts/run.cjs",
            )
        self.assertEqual(original_digest, payload["script_sha256"])
        sent = next(
            item for item in payload["skill_files"]
            if item["path"] == "scripts/run.cjs"
        )
        self.assertEqual(original_digest, sent["sha256"])

    def test_profile_preflight_binds_browser_socket_and_identity(self) -> None:
        response = {
            "valid": True,
            "runtime_identity": {
                "execution_runtime": "isolated_skill_executor",
                "python_implementation": "cpython",
                "python_version": "3.12.1",
                "platform": "linux",
                "network": "disabled",
                "dependency_install": "disabled",
                "runtime_profile": "browser-automation-v1",
                "network_policy": {
                    "direct": "disabled",
                    "egress": "policy_proxy",
                },
            },
            "requirements": [],
            "commands": [{"name": "node", "available": True}],
            "environment_variables": [],
            "platform_groups": [],
        }
        with (
            patch.dict(
                os.environ,
                {"SKILL_BROWSER_EXECUTOR_SOCKET": "/browser.sock"},
            ),
            patch(
                "tools.isolated_skill_executor."
                "probe_isolated_runtime_capabilities",
                return_value=response,
            ) as probe,
        ):
            result = python_env.preflight_isolated_skill_runtime(
                commands=["node"],
                runtime_profile="browser-automation-v1",
            )

        self.assertTrue(result["valid"], result)
        self.assertEqual(
            "browser-automation-v1",
            result["runtime_binding"]["runtime_profile"],
        )
        self.assertEqual(
            64,
            len(result["runtime_binding"]["socket_identity_sha256"]),
        )
        self.assertEqual(
            64,
            len(
                result["runtime_binding"][
                    "capability_identity_sha256"
                ]
            ),
        )
        probe.assert_called_once_with(
            requirements=[],
            commands=["node"],
            environment_variables=[],
            platform_groups=[],
            socket_path="/browser.sock",
        )

    def test_profile_preflight_rejects_wrong_executor_identity(self) -> None:
        response = {
            "valid": True,
            "runtime_identity": {
                "execution_runtime": "isolated_skill_executor",
                "python_implementation": "cpython",
                "python_version": "3.12.1",
                "platform": "linux",
                "network": "disabled",
                "dependency_install": "disabled",
                "runtime_profile": "base-v1",
                "network_policy": {
                    "direct": "disabled",
                    "egress": "none",
                },
            },
            "requirements": [],
            "commands": [],
            "environment_variables": [],
            "platform_groups": [],
        }
        with (
            patch.dict(
                os.environ,
                {"SKILL_BROWSER_EXECUTOR_SOCKET": "/browser.sock"},
            ),
            patch(
                "tools.isolated_skill_executor."
                "probe_isolated_runtime_capabilities",
                return_value=response,
            ),
        ):
            result = python_env.preflight_isolated_skill_runtime(
                runtime_profile="browser-automation-v1",
            )

        self.assertFalse(result["valid"])
        self.assertEqual(
            "runtime_profile_mismatch",
            result["error_code"],
        )

    def test_exact_entrypoint_preflight_reselects_profile_and_hashes(self) -> None:
        self.write(
            "scripts/browser.cjs",
            'require("playwright");\n',
        )
        snapshot = snapshot_skill_package(self.root)
        expected = {
            "valid": True,
            "checked": True,
            "blockers": [],
            "packages": {"requirements": [], "status": "satisfied"},
        }
        with patch(
            "runtime.python_env.preflight_isolated_skill_runtime",
            return_value=dict(expected),
        ) as preflight:
            result = python_env.preflight_skill_entrypoint_runtime(
                self.root,
                "scripts/browser.cjs",
                expected_package_sha256=snapshot.sha256,
                expected_script_sha256=snapshot.file_sha256(
                    "scripts/browser.cjs"
                ),
            )

        self.assertTrue(result["valid"], result)
        self.assertEqual(
            "browser-automation-v1",
            result["entrypoint_runtime"]["runtime_profile"],
        )
        preflight.assert_called_once_with(
            requirements=[],
            commands=["node"],
            environment_variables=None,
            platform_groups=None,
            runtime_profile="browser-automation-v1",
        )

    def test_exact_entrypoint_preflight_rejects_stale_package_before_probe(
        self,
    ) -> None:
        self.write("scripts/run.py", "print('ok')\n")
        with patch(
            "runtime.python_env.preflight_isolated_skill_runtime",
        ) as preflight:
            result = python_env.preflight_skill_entrypoint_runtime(
                self.root,
                "scripts/run.py",
                expected_package_sha256="0" * 64,
            )

        self.assertFalse(result["valid"])
        self.assertEqual(
            "skill_runtime_profile_authority_mismatch",
            result["error_code"],
        )
        preflight.assert_not_called()
