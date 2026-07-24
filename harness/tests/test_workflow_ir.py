import copy
import hashlib
import json
import math
import unittest

from workflow_ir import (
    InstructionDocumentError,
    WorkflowIRValidationError,
    WorkflowPlanAdapterError,
    canonicalize_skill_markdown,
    compile_worker_wave_plan,
    instruction_catalog_payload,
    parse_and_validate_workflow_ir,
    validate_workflow_ir,
    verify_instruction_execution_coverage,
)


CATALOG_SHA256 = "a" * 64
CAPABILITIES = ("cap-delegate", "cap-search")


class WorkflowIRTests(unittest.TestCase):
    def _document(self):
        return canonicalize_skill_markdown(
            """---
name: multilingual-workflow
version: "1"
---
# خطة متعددة اللغات

## 第一轮

1. 分析输入并保存结构化证据。
2. Review the same evidence independently.

| Роль | Задача |
|---|---|
| مراجِع | 独立检查 |

## 汇总

合并全部前置结果，并生成最终逻辑输出。
""",
            source_path="SKILL.md",
        )

    def _payload(self, document=None):
        document = document or self._document()
        executable = [unit for unit in document.units if unit.kind != "heading"]
        self.assertEqual(
            [unit.kind for unit in executable],
            ["list_item", "list_item", "table", "paragraph"],
        )
        first, second, table, final = executable
        nodes = [
            {
                "id": "worker-alpha",
                "kind": "delegate",
                "executor": "child_agent",
                "title": "Primary analysis",
                "role": "Analyzer",
                "phase": "round-1",
                "round": 1,
                "required": True,
                "instruction_ids": [first.id, table.id],
                "depends_on": [],
                "capability_ids": ["cap-delegate", "cap-search"],
                "result_id": "result-alpha",
                "result_schema": {
                    "type": "object",
                    "required": ["evidence"],
                    "properties": {"evidence": {"type": "array"}},
                },
                "output_ids": [],
                "join_policy": "all",
            },
            {
                "id": "worker-beta",
                "kind": "delegate",
                "executor": "child_agent",
                "title": "Independent review",
                "role": "Reviewer",
                "phase": "round-1",
                "round": 1,
                "required": True,
                "instruction_ids": [second.id, table.id],
                "depends_on": [],
                "capability_ids": ["cap-delegate"],
                "result_id": "result-beta",
                "result_schema": {
                    "type": "object",
                    "required": ["review"],
                    "properties": {"review": {"type": "string"}},
                },
                "output_ids": [],
                "join_policy": "all",
            },
            {
                "id": "aggregate-final",
                "kind": "aggregate",
                "executor": "child_agent",
                "title": "Merge exact prerequisites",
                "role": "Coordinator",
                "phase": "final",
                "round": 2,
                "required": True,
                "instruction_ids": [final.id],
                "depends_on": ["worker-alpha", "worker-beta"],
                "capability_ids": ["cap-delegate"],
                "result_id": "result-final",
                "result_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
                "output_ids": ["final-report"],
                "join_policy": "all",
            },
        ]
        coverage = []
        for unit in document.units:
            if unit.kind == "heading":
                coverage.append(
                    {
                        "instruction_id": unit.id,
                        "requirement": "advisory",
                        "disposition": "not_applicable",
                        "node_ids": [],
                        "output_ids": [],
                        "reason": "Structural heading; child units carry execution.",
                    }
                )
            elif unit.id == first.id:
                coverage.append(
                    {
                        "instruction_id": unit.id,
                        "requirement": "required",
                        "disposition": "mapped",
                        "node_ids": ["worker-alpha"],
                        "output_ids": [],
                        "reason": "",
                    }
                )
            elif unit.id == second.id:
                coverage.append(
                    {
                        "instruction_id": unit.id,
                        "requirement": "required",
                        "disposition": "mapped",
                        "node_ids": ["worker-beta"],
                        "output_ids": [],
                        "reason": "",
                    }
                )
            elif unit.id == table.id:
                coverage.append(
                    {
                        "instruction_id": unit.id,
                        "requirement": "required",
                        "disposition": "mapped",
                        "node_ids": ["worker-alpha", "worker-beta"],
                        "output_ids": [],
                        "reason": "",
                    }
                )
            else:
                coverage.append(
                    {
                        "instruction_id": unit.id,
                        "requirement": "required",
                        "disposition": "mapped",
                        "node_ids": ["aggregate-final"],
                        "output_ids": ["final-report"],
                        "reason": "",
                    }
                )
        return {
            "schema_version": "1",
            "complete": True,
            "skill": {"name": "multilingual-workflow", "version": "1"},
            "documents": [document.binding_dict()],
            "capability_catalog_sha256": CATALOG_SHA256,
            "nodes": nodes,
            "coverage": coverage,
            "outputs": [
                {
                    "id": "final-report",
                    "required": True,
                    "instruction_ids": [final.id],
                    "producer_node_ids": ["aggregate-final"],
                }
            ],
            "policies": {
                "completion_policy": "all_required",
                "failure_policy": "fail_closed",
                "max_parallelism": 6,
                "max_iterations_per_node": 32,
            },
            "counts": {
                "documents": 1,
                "instruction_units": len(document.units),
                "nodes": len(nodes),
                "coverage": len(coverage),
                "outputs": 1,
            },
        }

    def _validate(self, payload=None, document=None):
        document = document or self._document()
        return validate_workflow_ir(
            payload or self._payload(document),
            documents=[document],
            skill_name="multilingual-workflow",
            capability_catalog_sha256=CATALOG_SHA256,
            available_capability_ids=CAPABILITIES,
        )

    def assert_ir_error(self, code, payload, document=None):
        document = document or self._document()
        with self.assertRaises(WorkflowIRValidationError) as raised:
            self._validate(payload, document)
        self.assertEqual(raised.exception.code, code)

    def test_canonical_markdown_is_language_neutral_and_structural(self):
        document = self._document()

        self.assertEqual(
            [unit.kind for unit in document.units],
            [
                "heading",
                "heading",
                "list_item",
                "list_item",
                "table",
                "heading",
                "paragraph",
            ],
        )
        self.assertFalse(any("name:" in unit.text for unit in document.units))
        self.assertTrue(
            all(unit.source_sha256 == document.source_sha256 for unit in document.units)
        )
        self.assertEqual(document, self._document())
        self.assertEqual(len({unit.id for unit in document.units}), len(document.units))

        catalog = instruction_catalog_payload([document])
        self.assertEqual(catalog["counts"]["instruction_units"], len(document.units))
        self.assertRegex(catalog["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("مراجِع", json.dumps(catalog, ensure_ascii=False))

    def test_line_endings_are_canonical_but_exact_source_digest_still_changes(self):
        unix = canonicalize_skill_markdown("# 标题\n\n1. выполнить шаг\n")
        windows = canonicalize_skill_markdown("# 标题\r\n\r\n1. выполнить шаг\r\n")

        self.assertEqual(unix.canonical_sha256, windows.canonical_sha256)
        self.assertNotEqual(unix.source_sha256, windows.source_sha256)
        self.assertNotEqual(unix.instruction_set_sha256, windows.instruction_set_sha256)

    def test_code_blocks_are_one_unit_and_truncation_fails_closed(self):
        document = canonicalize_skill_markdown(
            "# Tool\n\n```python\nprint('ok')\n```\n"
        )
        self.assertEqual(
            [unit.kind for unit in document.units], ["heading", "code_block"]
        )

        for content, code in (
            ("# Tool\n\n```python\nprint('cut')", "unterminated_code_fence"),
            ("---\nname: cut", "unterminated_frontmatter"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(InstructionDocumentError) as raised:
                    canonicalize_skill_markdown(content)
                self.assertEqual(raised.exception.code, code)

        with self.assertRaises(InstructionDocumentError) as raised:
            canonicalize_skill_markdown("# More than bound", max_chars=8)
        self.assertEqual(raised.exception.code, "document_too_large")

    def test_source_paths_cannot_escape_the_skill_package(self):
        for path in ("../SKILL.md", "/tmp/SKILL.md", r"dir\SKILL.md"):
            with self.subTest(path=path):
                with self.assertRaises(InstructionDocumentError) as raised:
                    canonicalize_skill_markdown("# x", source_path=path)
                self.assertEqual(raised.exception.code, "invalid_source_path")

    def test_valid_ir_binds_content_capabilities_and_lowers_parallel_then_aggregate(
        self,
    ):
        document = self._document()
        workflow_ir = self._validate(self._payload(document), document)

        self.assertRegex(workflow_ir.ir_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(workflow_ir.to_dict()["documents"], [document.binding_dict()])
        plan = compile_worker_wave_plan(workflow_ir)
        self.assertEqual(plan["required_workers"], ["worker-alpha", "worker-beta"])
        self.assertEqual(
            plan["waves"],
            [
                {
                    "id": "ir-wave-001",
                    "mode": "parallel",
                    "workers": ["worker-alpha", "worker-beta"],
                    "dependencies": [],
                    "batch_limit": 6,
                }
            ],
        )
        self.assertEqual(
            plan["aggregation_steps"][0]["input_worker_ids"],
            ["worker-alpha", "worker-beta"],
        )
        self.assertEqual(plan["aggregation_steps"][0]["depends_on"], [])
        self.assertFalse(plan["requires_artifact_output"])
        self.assertEqual(plan["workflow_ir_outputs"][0]["id"], "final-report")

    def test_dependency_levels_become_parallel_waves_and_aggregation_dag_is_preserved(
        self,
    ):
        document = self._document()
        payload = self._payload(document)
        first_instruction = payload["nodes"][0]["instruction_ids"][0]
        headings = [unit for unit in document.units if unit.kind == "heading"]
        heading = headings[0]
        aggregation_heading = headings[1]
        payload["coverage"] = [
            item
            for item in payload["coverage"]
            if item["instruction_id"] not in {heading.id, aggregation_heading.id}
        ]
        payload["nodes"].insert(
            2,
            {
                "id": "worker-gamma",
                "kind": "verify",
                "executor": "child_agent",
                "title": "Second wave",
                "role": "Verifier",
                "phase": "round-2",
                "round": 2,
                "required": True,
                "instruction_ids": [heading.id],
                "depends_on": ["worker-alpha"],
                "capability_ids": ["cap-delegate"],
                "result_id": "result-gamma",
                "result_schema": {"type": "object"},
                "output_ids": [],
                "join_policy": "all",
            },
        )
        payload["nodes"].insert(
            3,
            {
                "id": "aggregate-check",
                "kind": "aggregate",
                "executor": "child_agent",
                "title": "Check all worker evidence",
                "role": "Quality gate",
                "phase": "aggregation",
                "round": 2,
                "required": True,
                "instruction_ids": [aggregation_heading.id],
                "depends_on": [
                    "worker-alpha",
                    "worker-beta",
                    "worker-gamma",
                ],
                "capability_ids": ["cap-delegate"],
                "result_id": "result-check",
                "result_schema": {"type": "object"},
                "output_ids": [],
                "join_policy": "all",
            },
        )
        payload["coverage"].append(
            {
                "instruction_id": heading.id,
                "requirement": "required",
                "disposition": "mapped",
                "node_ids": ["worker-gamma"],
                "output_ids": [],
                "reason": "",
            }
        )
        payload["coverage"].append(
            {
                "instruction_id": aggregation_heading.id,
                "requirement": "required",
                "disposition": "mapped",
                "node_ids": ["aggregate-check"],
                "output_ids": [],
                "reason": "",
            }
        )
        payload["nodes"][-1]["depends_on"] = ["aggregate-check"]
        payload["counts"]["nodes"] += 2
        # Coverage cardinality is unchanged: a former structural unit is now
        # explicitly classified and mapped to the second-wave verifier.
        payload["counts"]["coverage"] = len(payload["coverage"])

        workflow_ir = self._validate(payload, document)
        plan = compile_worker_wave_plan(workflow_ir)
        self.assertEqual(
            [wave["workers"] for wave in plan["waves"]],
            [["worker-alpha", "worker-beta"], ["worker-gamma"]],
        )
        self.assertEqual(plan["waves"][1]["dependencies"], ["ir-wave-001"])
        self.assertEqual(
            plan["aggregation_steps"][0]["input_worker_ids"],
            ["worker-alpha", "worker-beta", "worker-gamma"],
        )
        self.assertEqual(
            [step["id"] for step in plan["aggregation_steps"]],
            ["aggregate-check", "aggregate-final"],
        )
        self.assertEqual(
            plan["aggregation_steps"][1]["depends_on"], ["aggregate-check"]
        )
        self.assertIn(
            first_instruction,
            plan["workers"]["worker-alpha"]["instruction_ids"],
        )

    def test_one_receipt_cannot_satisfy_multiple_mapped_nodes(self):
        workflow_ir = self._validate()
        digest = hashlib.sha256(b"result").hexdigest()

        partial = verify_instruction_execution_coverage(
            workflow_ir,
            [
                {
                    "node_id": "worker-alpha",
                    "status": "succeeded",
                    "result_sha256": digest,
                }
            ],
        )
        self.assertFalse(partial.complete)
        shared = next(
            finding for finding in partial.findings if len(finding.node_ids) == 2
        )
        self.assertEqual(shared.status, "pending")
        self.assertEqual(shared.missing_node_ids, ("worker-beta",))

        complete = verify_instruction_execution_coverage(
            workflow_ir,
            [
                {
                    "node_id": node_id,
                    "status": "succeeded",
                    "result_sha256": digest,
                }
                for node_id in (
                    "worker-alpha",
                    "worker-beta",
                    "aggregate-final",
                )
            ],
        )
        self.assertTrue(complete.complete)
        self.assertFalse(complete.blocking_instruction_ids)

    def test_degraded_or_failed_required_receipts_remain_blocking(self):
        workflow_ir = self._validate()
        digest = "b" * 64
        report = verify_instruction_execution_coverage(
            workflow_ir,
            [
                {
                    "node_id": "worker-alpha",
                    "status": "degraded",
                    "result_sha256": digest,
                },
                {
                    "node_id": "worker-beta",
                    "status": "succeeded",
                    "result_sha256": digest,
                },
                {
                    "node_id": "aggregate-final",
                    "status": "failed",
                    "result_sha256": None,
                },
            ],
        )
        self.assertFalse(report.complete)
        self.assertIn("degraded", {finding.status for finding in report.findings})
        self.assertIn("failed", {finding.status for finding in report.findings})

    def test_unknown_capability_is_rejected(self):
        document = self._document()
        payload = self._payload(document)
        payload["nodes"][0]["capability_ids"].append("cap-invented")
        self.assert_ir_error("unknown_capability_id", payload, document)

    def test_document_digest_and_catalog_digest_are_exact_bindings(self):
        document = self._document()
        payload = self._payload(document)
        payload["documents"][0]["source_sha256"] = "f" * 64
        self.assert_ir_error("document_binding_mismatch", payload, document)

        payload = self._payload(document)
        payload["capability_catalog_sha256"] = "f" * 64
        self.assert_ir_error("capability_catalog_mismatch", payload, document)

    def test_duplicate_ids_cycles_unknown_dependencies_and_omissions_fail_closed(self):
        document = self._document()

        payload = self._payload(document)
        payload["nodes"][1]["id"] = "worker-alpha"
        self.assert_ir_error("duplicate_node_id", payload, document)

        payload = self._payload(document)
        payload["nodes"][0]["depends_on"] = ["aggregate-final"]
        self.assert_ir_error("workflow_cycle", payload, document)

        payload = self._payload(document)
        payload["nodes"][0]["depends_on"] = ["worker-missing"]
        self.assert_ir_error("unknown_dependency", payload, document)

        payload = self._payload(document)
        payload["coverage"].pop()
        payload["counts"]["coverage"] -= 1
        self.assert_ir_error("instruction_coverage_omission", payload, document)

    def test_bidirectional_instruction_and_output_mappings_are_enforced(self):
        document = self._document()

        payload = self._payload(document)
        payload["nodes"][0]["instruction_ids"].pop()
        self.assert_ir_error("instruction_node_coverage_mismatch", payload, document)

        payload = self._payload(document)
        payload["outputs"][0]["producer_node_ids"] = ["worker-alpha"]
        self.assert_ir_error("output_producer_mismatch", payload, document)

        payload = self._payload(document)
        output_instruction = payload["outputs"][0]["instruction_ids"][0]
        coverage = next(
            item
            for item in payload["coverage"]
            if item["instruction_id"] == output_instruction
        )
        coverage["output_ids"] = []
        self.assert_ir_error("instruction_output_coverage_mismatch", payload, document)

    def test_unknown_fields_counts_nonfinite_and_compacted_placeholders_fail_closed(
        self,
    ):
        document = self._document()

        payload = self._payload(document)
        payload["__proto__"] = {"polluted": True}
        self.assert_ir_error("unknown_field", payload, document)

        payload = self._payload(document)
        payload["counts"]["nodes"] = 999
        self.assert_ir_error("count_mismatch", payload, document)

        payload = self._payload(document)
        payload["nodes"][0]["result_schema"]["minimum"] = math.nan
        self.assert_ir_error("non_finite_number", payload, document)

        self.assert_ir_error(
            "unknown_field",
            {"_chatds_argument_omitted": True, "kind": "large_code_argument"},
            document,
        )

    def test_operational_and_graph_bounds_are_hard_limits(self):
        document = self._document()

        payload = self._payload(document)
        payload["policies"]["max_parallelism"] = 65
        self.assert_ir_error("integer_out_of_bounds", payload, document)

        payload = self._payload(document)
        payload["policies"]["max_iterations_per_node"] = 129
        self.assert_ir_error("integer_out_of_bounds", payload, document)

        payload = self._payload(document)
        template = payload["nodes"][0]
        payload["nodes"] = []
        for index in range(513):
            node = copy.deepcopy(template)
            node["id"] = f"bounded-node-{index}"
            node["result_id"] = f"bounded-result-{index}"
            payload["nodes"].append(node)
        payload["counts"]["nodes"] = len(payload["nodes"])
        self.assert_ir_error("too_many_nodes", payload, document)

    def test_strict_json_rejects_duplicates_and_truncated_streams(self):
        document = self._document()
        payload = self._payload(document)
        encoded = json.dumps(payload, ensure_ascii=False)
        workflow_ir = parse_and_validate_workflow_ir(
            encoded,
            documents=[document],
            skill_name="multilingual-workflow",
            capability_catalog_sha256=CATALOG_SHA256,
            available_capability_ids=CAPABILITIES,
        )
        self.assertRegex(workflow_ir.ir_sha256, r"^[0-9a-f]{64}$")
        round_tripped = self._validate(workflow_ir.to_dict(), document)
        self.assertEqual(round_tripped.ir_sha256, workflow_ir.ir_sha256)

        for raw, code in (
            ('{"schema_version":"1","schema_version":"1"}', "duplicate_json_key"),
            (encoded[:-5], "malformed_or_truncated_json"),
            ('{"value":NaN}', "malformed_or_truncated_json"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(WorkflowIRValidationError) as raised:
                    parse_and_validate_workflow_ir(
                        raw,
                        documents=[document],
                        skill_name="multilingual-workflow",
                        capability_catalog_sha256=CATALOG_SHA256,
                        available_capability_ids=CAPABILITIES,
                    )
                self.assertEqual(raised.exception.code, code)

    def test_ir_digest_and_backend_capability_ids_are_not_ambiguous(self):
        document = self._document()
        workflow_ir = self._validate(self._payload(document), document)
        payload = workflow_ir.to_dict()
        payload["ir_sha256"] = "f" * 64
        self.assert_ir_error("workflow_ir_digest_mismatch", payload, document)

        with self.assertRaises(WorkflowIRValidationError) as raised:
            validate_workflow_ir(
                self._payload(document),
                documents=[document],
                skill_name="multilingual-workflow",
                capability_catalog_sha256=CATALOG_SHA256,
                available_capability_ids=("cap-delegate", "cap-delegate"),
            )
        self.assertEqual(raised.exception.code, "duplicate_capability_id")

    def test_required_node_cannot_depend_on_optional_node(self):
        document = self._document()
        payload = self._payload(document)
        payload["nodes"][0]["required"] = False
        self.assert_ir_error("required_node_depends_on_optional", payload, document)

    def test_adapter_rejects_executor_rerouting_and_worker_after_aggregation(self):
        document = self._document()
        payload = self._payload(document)
        payload["nodes"][0]["kind"] = "tool"
        payload["nodes"][0]["executor"] = "native_tool"
        workflow_ir = self._validate(payload, document)
        with self.assertRaises(WorkflowPlanAdapterError) as raised:
            compile_worker_wave_plan(workflow_ir)
        self.assertEqual(raised.exception.code, "unsupported_executor_for_worker_plan")

        payload = self._payload(document)
        payload["nodes"][0]["depends_on"] = ["aggregate-final"]
        payload["nodes"][2]["depends_on"] = []
        workflow_ir = self._validate(payload, document)
        with self.assertRaises(WorkflowPlanAdapterError) as raised:
            compile_worker_wave_plan(workflow_ir)
        self.assertEqual(raised.exception.code, "worker_depends_on_aggregation")

    def test_duplicate_and_unknown_execution_receipts_fail_closed(self):
        workflow_ir = self._validate()
        receipt = {
            "node_id": "worker-alpha",
            "status": "succeeded",
            "result_sha256": "c" * 64,
        }
        with self.assertRaises(WorkflowIRValidationError) as raised:
            verify_instruction_execution_coverage(
                workflow_ir, [receipt, copy.deepcopy(receipt)]
            )
        self.assertEqual(raised.exception.code, "duplicate_node_receipt")

        receipt["node_id"] = "invented-node"
        with self.assertRaises(WorkflowIRValidationError) as raised:
            verify_instruction_execution_coverage(workflow_ir, [receipt])
        self.assertEqual(raised.exception.code, "unknown_receipt_node")


if __name__ == "__main__":
    unittest.main()
