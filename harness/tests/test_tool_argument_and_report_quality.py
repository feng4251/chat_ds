import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import workspace_context
import tools.code_execution as code_execution
import tools.path_security as path_security
from tools.omission_guard import contains_compacted_history_omission
from agent_loop import (
    HarnessRunState,
    _compact_tool_call_arguments,
    _deterministic_verifier_payload,
    _markdown_artifact_findings,
)
from tools.code_execution import (
    _code_with_session_snapshot,
    _execution_boundary_error,
    _managed_runtime_reason,
    _referenced_persisted_result_paths,
    _rewrite_isolated_session_paths,
)


class ToolArgumentAndReportQualityTests(unittest.TestCase):
    def test_compacted_write_file_trace_uses_structured_redaction(self):
        args = json.dumps({"filepath": "report.md", "content": "x" * 2000})

        compacted = _compact_tool_call_arguments("write_file", args)

        self.assertNotIn("__CHATDS_OMITTED", compacted)
        payload = json.loads(compacted)
        self.assertNotIn("content", payload)
        self.assertTrue(payload["content_omitted"]["_chatds_argument_omitted"])
        self.assertEqual(payload["content_omitted"]["kind"], "large_file_content")


    def test_compacted_write_file_trace_moves_redaction_out_of_content_field(self):
        args = json.dumps({"filepath": "report.md", "content": "x" * 2000})

        compacted = _compact_tool_call_arguments("write_file", args)
        payload = json.loads(compacted)

        self.assertNotIn("content", payload)
        self.assertTrue(payload["content_omitted"]["_chatds_argument_omitted"])

    def test_compacted_execute_code_trace_moves_redaction_out_of_code_field(self):
        args = json.dumps({"code": "print('x')\n" * 200})

        compacted = _compact_tool_call_arguments("execute_code", args)
        payload = json.loads(compacted)

        self.assertNotIn("code", payload)
        self.assertTrue(payload["code_omitted"]["_chatds_argument_omitted"])

    def test_omission_guard_rejects_structured_metadata_strings(self):
        payload = json.dumps({"_chatds_argument_omitted": True, "kind": "large_argument"})

        self.assertTrue(contains_compacted_history_omission(payload))

    def test_artifact_scan_uses_shared_embedded_marker_guard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                (workspace / "polluted.md").write_text(
                    "# Report\n\n```json\n"
                    '{"_chatds_argument_omitted": true, "chars": 9000}'
                    "\n```\n",
                    encoding="utf-8",
                )
                (workspace / "clean.md").write_text(
                    "# Report\n\nThe author omitted an optional appendix.\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(user_id="u", session_id="s")

                findings = _markdown_artifact_findings(
                    run_state,
                    ["polluted.md", "clean.md"],
                )

                joined = "\n".join(findings)
                self.assertIn("polluted.md", joined)
                self.assertNotIn("clean.md", joined)

    def test_relative_workspace_file_operations_use_managed_runtime(self):
        reason = _managed_runtime_reason("import os\nprint(os.listdir('GAL3_AD_CDP'))")

        self.assertEqual(reason, "workspace file operation code")

    def test_execute_code_does_not_snapshot_unreferenced_large_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(path_security, "SANDBOX_ROOT", root), patch.object(code_execution, "SANDBOX_ROOT", root):
                workspace = path_security.sandbox_dir("u", "s", sub="workspace")
                for index in range(5):
                    (workspace / f"large_{index}.md").write_text("x" * 180_000, encoding="utf-8")

                code = _code_with_session_snapshot("print('ok')", "u", "s")

                self.assertEqual(code, "print('ok')")

    def test_execute_code_snapshots_explicitly_referenced_session_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(path_security, "SANDBOX_ROOT", root), patch.object(code_execution, "SANDBOX_ROOT", root):
                workspace = path_security.sandbox_dir("u", "s", sub="workspace")
                target = workspace / "input.md"
                target.write_text("source", encoding="utf-8")

                code = _code_with_session_snapshot(f"print(open('{target}').read())", "u", "s")

                self.assertIn("__chatds_files", code)
                self.assertIn("workspace/input.md", code)

    def test_isolated_path_rewrite_targets_only_session_workspace_and_skills(self):
        with patch.object(code_execution, "SKILL_DATA_ROOT", Path("/app/data/skills")):
            rewritten = _rewrite_isolated_session_paths(
                "\n".join((
                    "open('/app/workspace/input.txt').read()",
                    "open('workspace/output.txt', 'w').write('ok')",
                    "open('/app/data/skills/u/s/demo/assets/a.txt').read()",
                    "open('results/tool-output.txt').read()",
                    "label = 'friendships/example'",
                )),
                "u",
                "s",
            )

        self.assertIn("open('./input.txt')", rewritten)
        self.assertIn("open('../results/tool-output.txt')", rewritten)
        self.assertIn("friendships/example", rewritten)
        self.assertIn("open('./output.txt', 'w')", rewritten)
        self.assertIn("open('../skills/demo/assets/a.txt')", rewritten)
        self.assertIn("'friendships/example'", rewritten)

    def test_persisted_result_selection_is_current_session_literal_and_regular(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "u" / "s" / "results"
            other = root / "u" / "other" / "results"
            current.mkdir(parents=True)
            other.mkdir(parents=True)
            (current / "tool.txt").write_text("current", encoding="utf-8")
            (other / "tool.txt").write_text("other", encoding="utf-8")
            (current / "escape.txt").symlink_to(other / "tool.txt")
            with patch.object(code_execution, "SANDBOX_ROOT", root):
                selected = _referenced_persisted_result_paths(
                    "print(open('results/tool.txt').read())\n"
                    "print(open('results/escape.txt').read())",
                    "u",
                    "s",
                )

        self.assertEqual(("tool.txt",), selected)

    def test_workspace_file_reads_use_managed_runtime(self):
        reason = _managed_runtime_reason("from pathlib import Path\nprint(Path('workspace/report.md').stat().st_size)")

        self.assertEqual(reason, "workspace file operation code")

    def test_common_dataframe_file_apis_use_managed_runtime(self):
        cases = {
            "import pandas as pd\npd.read_csv('inputs/data.csv')": "workspace file operation code",
            "from pandas import read_json\nread_json(path_or_buf='inputs/data.json')": "workspace file operation code",
            "import pandas as pd\npd.read_excel('inputs/data.xlsx')": "workspace file operation code",
            "import pandas as pd\npd.read_parquet(path='inputs/data.parquet')": "workspace file operation code",
            "df.to_csv('outputs/data.csv')": "workspace file-write code",
            "df.to_json(path_or_buf='outputs/data.json')": "workspace file-write code",
            "df.to_excel('outputs/data.xlsx')": "workspace file-write code",
            "df.to_parquet(path='outputs/data.parquet')": "workspace file-write code",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(_managed_runtime_reason(code), expected)

    def test_common_numpy_file_apis_use_managed_runtime(self):
        cases = {
            "import numpy as np\nnp.load('inputs/data.npy')": "workspace file operation code",
            "from numpy import loadtxt\nloadtxt(fname='inputs/data.txt')": "workspace file operation code",
            "import numpy as np\nnp.save('outputs/data.npy', values)": "workspace file-write code",
            "from numpy import savetxt\nsavetxt('outputs/data.txt', values)": "workspace file-write code",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(_managed_runtime_reason(code), expected)

    def test_pathlib_and_shutil_explicit_paths_use_managed_runtime(self):
        cases = {
            "from pathlib import Path\np = Path('inputs') / 'data.csv'\np.read_text()": "workspace file operation code",
            "import pathlib\npathlib.Path('outputs/report.md').write_text('ok')": "workspace file-write code",
            "from pathlib import Path\nPath('outputs/report.md').open('w')": "workspace file-write code",
            "from pathlib import Path\nlist(Path('inputs').glob('*.csv'))": "workspace file operation code",
            "import shutil as sh\nsh.copyfile('inputs/a.csv', 'outputs/a.csv')": "workspace file-write code",
            "import os\np = os.path.join('inputs', 'data.csv')\nos.stat(p)": "workspace file operation code",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(_managed_runtime_reason(code), expected)

    def test_non_file_strings_and_path_construction_remain_isolated(self):
        cases = (
            "print(\"pd.read_csv('workspace/data.csv') is an example\")",
            "# open('workspace/data.csv')\nprint('done')",
            "from pathlib import Path\nprint(Path('report.md').suffix)",
            "import os\nprint(os.path.join('package', 'module'))",
            "import importlib\nimportlib.import_module('package.module')",
            "result.stat()  # arbitrary domain object, not pathlib",
            "archive.open('member.txt')  # archive member, not a workspace path",
            "import pandas as pd\nprint(pd.DataFrame({'a': [1]}).to_json())",
            "import pandas as pd, io\npd.read_csv(io.StringIO('a,b\\n1,2'))",
            "import pandas as pd\npd.read_json('{\"a\": [1]}')",
            "url = 'https://example.test/data.csv'\nprint(url)",
        )
        for code in cases:
            with self.subTest(code=code):
                self.assertIsNone(_managed_runtime_reason(code))

    def test_dynamic_pathlib_object_is_still_a_real_file_operation(self):
        reason = _managed_runtime_reason(
            "from pathlib import Path\np = Path(filename)\nprint(p.read_text())"
        )

        self.assertEqual(reason, "workspace file operation code")

    def test_remote_dataframe_url_is_network_not_workspace_path(self):
        reason = _managed_runtime_reason(
            "import pandas as pd\npd.read_csv('https://example.test/data.csv')"
        )

        self.assertEqual(reason, "network/API code")

    def test_explicit_relative_path_traversal_is_blocked(self):
        error = _execution_boundary_error(
            "import pandas as pd\npd.read_csv('../other-session/data.csv')",
            "user",
            "session",
        )

        self.assertIn("must not traverse", error)

        glob_error = _execution_boundary_error(
            "from pathlib import Path\nlist(Path('inputs').glob('../other-session/*.csv'))",
            "user",
            "session",
        )
        self.assertIn("must not traverse", glob_error)

    def test_explicit_unrelated_absolute_file_is_blocked(self):
        error = _execution_boundary_error(
            "import numpy as np\nnp.load('/etc/passwd')",
            "user",
            "session",
        )

        self.assertIn("Absolute paths are limited", error)

    def test_skill_report_verifier_rejects_duplicate_numbered_headings_within_h1(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                report = workspace / "FULL_REPORT.md"
                report.write_text(
                    "# 1. A\n"
                    "## 1.1 First\ncontent\n"
                    "## 1.1 Duplicate\ncontent\n"
                    "# 2. B\n## 2.1 Next\ncontent\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "FULL_REPORT.md",
                    "size_bytes": report.stat().st_size,
                })
                run_state.skill_workflow_contracts["demo"] = {
                    "requires_merge": True,
                    "quality_contract": {
                        "constraints": {
                            "forbid_duplicate_numbered_headings": True,
                        },
                    },
                    "output_contract": {
                        "declared_final_artifact": "FULL_REPORT.md",
                        "expected_min_bytes": 10,
                        "expected_min_lines": 5,
                        "declared_section_count": 2,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertEqual(payload["verdict"], "fail")
                self.assertIn("duplicate numbered section headings", payload["reason"])

    def test_skill_report_verifier_allows_repeated_local_numbers_across_h1(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                report = workspace / "FULL_REPORT.md"
                report.write_text(
                    "# Part A\n"
                    "## 1.1 Objective\ncontent\n"
                    "# Part B\n"
                    "## 1.1 Objective\ncontent\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "FULL_REPORT.md",
                    "size_bytes": report.stat().st_size,
                })
                run_state.skill_workflow_contracts["demo"] = {
                    "requires_merge": True,
                    "quality_contract": {
                        "constraints": {
                            "forbid_duplicate_numbered_headings": True,
                        },
                    },
                    "output_contract": {
                        "declared_final_artifact": "FULL_REPORT.md",
                        "expected_min_bytes": 10,
                        "expected_min_lines": 4,
                        "declared_section_count": 2,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertNotEqual(payload["verdict"], "fail")
                self.assertNotIn("duplicate numbered section headings", "\n".join(payload["findings"]))

    def test_skill_report_verifier_does_not_invent_heading_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                report = workspace / "report.md"
                report.write_text(
                    "# Part\n## 1.1 Allowed\ntext\n## 1.1 Also allowed\ntext\n",
                    encoding="utf-8",
                )
                run_state = HarnessRunState(user_id="u", session_id="s")
                run_state.artifacts.append({
                    "kind": "file",
                    "path": "report.md",
                    "size_bytes": report.stat().st_size,
                })
                run_state.skill_workflow_contracts["plain-standard-skill"] = {
                    "output_contract": {
                        "declared_final_artifact": "report.md",
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertNotIn(
                    "duplicate numbered section headings",
                    "\n".join(payload["findings"]),
                )

    def test_workflow_warning_does_not_fail_after_artifact_contract_satisfied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(workspace_context, "WORKSPACE_ROOT", root):
                workspace = workspace_context.get_workspace("u", "s")
                module_a = "# 1. A\n## 1.1 First\ncontent\n"
                module_b = "# 2. B\n## 2.1 Next\ncontent\n"
                (workspace / "01_a.md").write_text(module_a, encoding="utf-8")
                (workspace / "02_b.md").write_text(module_b, encoding="utf-8")
                (workspace / "README.md").write_text(
                    "[A](01_a.md) [B](02_b.md) [Report](FULL_REPORT.md)\n",
                    encoding="utf-8",
                )
                (workspace / "_checklist.md").write_text(
                    "| Item | Status |\n|---|---|\n"
                    "| Section 1 | PASS |\n"
                    "| Section 2 | PASS |\n"
                    "| Auto merge | PASS |\n",
                    encoding="utf-8",
                )
                report = workspace / "FULL_REPORT.md"
                report.write_text(module_a + module_b, encoding="utf-8")
                run_state = HarnessRunState(user_id="u", session_id="s")
                for name in ("01_a.md", "02_b.md", "README.md", "_checklist.md", "FULL_REPORT.md"):
                    path = workspace / name
                    if not path.exists():
                        path.write_text("content", encoding="utf-8")
                    artifact = {
                        "kind": "file",
                        "path": name,
                        "size_bytes": path.stat().st_size,
                    }
                    if name == "FULL_REPORT.md":
                        artifact.update({
                            "source_tool": "merge_files",
                            "is_merged": True,
                            "input_files": ["01_a.md", "02_b.md"],
                        })
                    run_state.artifacts.append(artifact)
                run_state.viewed_skill_files["demo"] = {"__manifest__"}
                run_state.skill_workflow_contracts["demo"] = {
                    "requires_modular_artifacts": True,
                    "requires_merge": True,
                    "orchestrator_files": ["orchestration/orchestrator.yaml"],
                    "output_contract": {
                        "declared_file_count": 5,
                        "declared_final_artifact": "FULL_REPORT.md",
                        "declared_modular_files": ["01_a.md", "02_b.md"],
                        "declared_ancillary_files": ["README.md", "_checklist.md"],
                        "merge_mandatory": True,
                        "expected_min_bytes": 10,
                        "expected_min_lines": 4,
                        "declared_section_count": 2,
                    },
                }

                payload = _deterministic_verifier_payload(
                    run_state,
                    requested_artifact=True,
                    complex_report=True,
                )

                self.assertEqual(payload["verdict"], "pass", payload)
                self.assertFalse(payload["needs_more_work"])
                self.assertIn("not directly observed", "\n".join(payload["findings"]))


class ExecuteCodeWorkspaceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_result_reference_is_snapshotted_into_session_runtime(self):
        isolated = AsyncMock(return_value={
            "status": "success",
            "stdout": "current",
            "stderr": "",
            "returncode": 0,
            "network": "disabled",
            "artifacts": [],
            "workspace_applied": True,
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(path_security, "SANDBOX_ROOT", root),
                patch.object(code_execution, "SANDBOX_ROOT", root),
                patch.object(code_execution, "SKILL_DATA_ROOT", root / "skills"),
                patch(
                    "tools.isolated_skill_executor.execute_isolated_session_code",
                    isolated,
                ),
            ):
                workspace = path_security.sandbox_dir("u", "s", sub="workspace")
                results = path_security.sandbox_dir("u", "s", sub="results")
                (results / "tool.txt").write_text("current", encoding="utf-8")
                result = json.loads(await code_execution.execute_code(
                    "print(open('results/tool.txt').read())",
                    user_id="u",
                    session_id="s",
                ))

        self.assertEqual("success", result["status"])
        isolated.assert_awaited_once()
        kwargs = isolated.await_args.kwargs
        self.assertEqual(workspace, kwargs["workspace"])
        self.assertEqual(results, kwargs["results_root"])
        self.assertEqual(("tool.txt",), kwargs["result_paths"])
        self.assertIn("open('../results/tool.txt')", kwargs["code"])

    async def test_pandas_relative_read_dispatches_isolated_session_runtime(self):
        isolated = AsyncMock(return_value={
            "status": "success",
            "stdout": "ok",
            "stderr": "",
            "returncode": 0,
            "network": "disabled",
            "artifacts": [],
            "workspace_applied": True,
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(path_security, "SANDBOX_ROOT", root),
                patch.object(code_execution, "SANDBOX_ROOT", root),
                patch.object(code_execution, "SKILL_DATA_ROOT", root / "skills"),
                patch(
                    "tools.isolated_skill_executor.execute_isolated_session_code",
                    isolated,
                ),
            ):
                result = json.loads(await code_execution.execute_code(
                    "import pandas as pd\nprint(pd.read_csv('inputs/data.csv'))",
                    user_id="u",
                    session_id="s",
                ))

        self.assertEqual(result["execution_runtime"], "isolated_session_python")
        self.assertEqual(result["network"], "disabled")
        self.assertNotIn("managed_session_python", json.dumps(result))
        isolated.assert_awaited_once()

    async def test_network_code_never_falls_back_to_harness_python(self):
        isolated = AsyncMock(return_value={
            "status": "error",
            "stdout": "",
            "stderr": "Network is unreachable",
            "returncode": 1,
            "network": "disabled",
            "artifacts": [],
            "workspace_applied": True,
        })
        managed = AsyncMock(side_effect=AssertionError("must never execute in harness"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(path_security, "SANDBOX_ROOT", root),
                patch.object(code_execution, "SANDBOX_ROOT", root),
                patch.object(code_execution, "SKILL_DATA_ROOT", root / "skills"),
                patch(
                    "tools.isolated_skill_executor.execute_isolated_session_code",
                    isolated,
                ),
                patch("tools.skill_python.run_managed_python_code", managed),
            ):
                result = json.loads(await code_execution.execute_code(
                    "import urllib.request\nurllib.request.urlopen('https://example.com')",
                    user_id="u",
                    session_id="s",
                ))

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["network_access"], "unavailable")
        self.assertIn("will not retry in harness", result["degraded_reason"])
        isolated.assert_awaited_once()
        managed.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
