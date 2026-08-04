from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_loop import (
    HarnessRunState,
    _build_standard_skill_capability_catalog,
    _standard_skill_declares_delegated_workflow,
    _workflow_ir_artifact_output_contract,
    _workflow_contract_findings,
)
from skill_capability_plan import (
    _bind_worker_plan_capabilities,
    build_capability_catalog,
    catalog_prompt_payload,
    validate_capability_plan,
)
from skills.loader import load_skill_content
from tools.context import ToolContext
from tools.delegation import (
    _instruction_source_boundary_error,
    _run_child,
)
from tools.skill_capability_plan import (
    SUBMIT_SKILL_CAPABILITY_PLAN_SCHEMA,
    submit_skill_capability_plan,
)
from workflow_ir import canonicalize_skill_markdown


def _reference_workflow_case(
    root: Path,
    *,
    line_ending: str = "\n",
) -> dict:
    """Build one generic ref-backed, delegate-only Workflow IR fixture."""

    references = root / "references"
    references.mkdir(parents=True)
    main = root / "SKILL.md"
    reference = references / "worker.md"
    main_text = """---
name: portable-workflow
description: A generic reference-backed workflow.
---
# Workflow

Execute the exact delegated procedure in [the worker authority](references/worker.md).
""".replace("\n", line_ending)
    reference_text = """# Worker authority

1. Produce the assigned result from the supplied context.
""".replace("\n", line_ending)
    main.write_bytes(main_text.encode("utf-8"))
    reference.write_bytes(reference_text.encode("utf-8"))

    package = load_skill_content(main, skill_dir=str(root))
    main_document = canonicalize_skill_markdown(
        main_text,
        source_path="SKILL.md",
    )
    reference_document = canonicalize_skill_markdown(
        reference_text,
        source_path="references/worker.md",
    )
    reference_digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    authority_documents = ({
        "resource_path": "references/worker.md",
        "sha256": reference_digest,
        "content": reference_text,
    },)
    catalog = build_capability_catalog(
        skill_name="portable-workflow",
        loaded_package=package,
        available_tools=["skill_view", "delegate_task"],
        authority_documents=authority_documents,
        instruction_documents=(main_document, reference_document),
        workflow_ir_required=True,
    )
    delegate_id = next(
        candidate["id"]
        for candidate in catalog["candidates"]
        if candidate.get("tool_name") == "delegate_task"
    )
    executable_units = [
        unit
        for document in (main_document, reference_document)
        for unit in document.units
        if unit.kind != "heading"
    ]
    node_id = "reference-worker"
    coverage = []
    for document in (main_document, reference_document):
        for unit in document.units:
            mapped = unit.kind != "heading"
            coverage.append({
                "instruction_id": unit.id,
                "requirement": "required" if mapped else "advisory",
                "disposition": "mapped" if mapped else "not_applicable",
                "node_ids": [node_id] if mapped else [],
                "output_ids": [],
                "reason": "" if mapped else "Structural heading.",
            })
    workflow_ir = {
        "schema_version": "1",
        "complete": True,
        "skill": {"name": "portable-workflow"},
        "documents": [
            main_document.binding_dict(),
            reference_document.binding_dict(),
        ],
        "capability_catalog_sha256": catalog["catalog_sha256"],
        "nodes": [{
            "id": node_id,
            "kind": "delegate",
            "executor": "child_agent",
            "title": "Reference-backed worker",
            "role": "Independent worker",
            "phase": "analysis",
            "round": 1,
            "required": True,
            "instruction_ids": [unit.id for unit in executable_units],
            "depends_on": [],
            "capability_ids": [delegate_id],
            "result_id": "reference-result",
            "result_schema": {
                "type": "object",
                "required": ["finding"],
                "properties": {"finding": {"type": "string"}},
            },
            "output_ids": [],
            "join_policy": "all",
        }],
        "coverage": coverage,
        "outputs": [],
        "policies": {
            "completion_policy": "all_required",
            "failure_policy": "fail_closed",
            "max_parallelism": 2,
            "max_iterations_per_node": 8,
        },
        "counts": {
            "documents": 2,
            "instruction_units": sum(
                len(document.units)
                for document in (main_document, reference_document)
            ),
            "nodes": 1,
            "coverage": len(coverage),
            "outputs": 0,
        },
    }
    accepted = validate_capability_plan(
        catalog,
        skill_name="portable-workflow",
        body_sha256=catalog["body_sha256"],
        catalog_sha256=catalog["catalog_sha256"],
        required=[delegate_id],
        optional=[],
        unsupported=[],
        workflow_ir=workflow_ir,
    )
    return {
        "root": root,
        "main": main,
        "reference": reference,
        "package": package,
        "documents": (main_document, reference_document),
        "authority_documents": authority_documents,
        "catalog": catalog,
        "delegate_id": delegate_id,
        "workflow_ir": workflow_ir,
        "accepted": accepted,
    }


class CapabilityWorkflowIRTests(unittest.TestCase):
    def _fixture(self, *, workflow_ir_required: bool = True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "portable-workflow"
        root.mkdir()
        main = root / "SKILL.md"
        main.write_text(
            """---
name: portable-workflow
description: A generic instruction-only workflow.
---
# 通用协作流程

1. Produce the first independent result.
2. independently produce the second result.
3. 汇总所有前置结果。
""",
            encoding="utf-8",
        )
        package = load_skill_content(main, skill_dir=str(root))
        document = canonicalize_skill_markdown(
            main.read_text(encoding="utf-8"),
            source_path="SKILL.md",
        )
        catalog = build_capability_catalog(
            skill_name="portable-workflow",
            loaded_package=package,
            available_tools=["skill_view", "delegate_task"],
            instruction_documents=(document,),
            workflow_ir_required=workflow_ir_required,
        )
        delegate_id = next(
            candidate["id"]
            for candidate in catalog["candidates"]
            if candidate.get("tool_name") == "delegate_task"
        )
        return root, main, document, catalog, delegate_id

    def _workflow_payload(self, document, catalog, delegate_id):
        executable = [unit for unit in document.units if unit.kind != "heading"]
        self.assertEqual(3, len(executable))
        first, second, aggregate = executable
        nodes = [
            {
                "id": "worker-first",
                "kind": "delegate",
                "executor": "child_agent",
                "title": "First independent result",
                "role": "Independent worker",
                "phase": "analysis",
                "round": 1,
                "required": True,
                "instruction_ids": [first.id],
                "depends_on": [],
                "capability_ids": [delegate_id],
                "result_id": "result-first",
                "result_schema": {
                    "type": "object",
                    "required": ["finding"],
                    "properties": {"finding": {"type": "string"}},
                },
                "output_ids": [],
                "join_policy": "all",
            },
            {
                "id": "worker-second",
                "kind": "delegate",
                "executor": "child_agent",
                "title": "Second independent result",
                "role": "Independent reviewer",
                "phase": "analysis",
                "round": 1,
                "required": True,
                "instruction_ids": [second.id],
                "depends_on": [],
                "capability_ids": [delegate_id],
                "result_id": "result-second",
                "result_schema": {
                    "type": "object",
                    "required": ["finding"],
                    "properties": {"finding": {"type": "string"}},
                },
                "output_ids": [],
                "join_policy": "all",
            },
            {
                "id": "aggregate-all",
                "kind": "aggregate",
                "executor": "child_agent",
                "title": "Aggregate all prerequisites",
                "role": "Coordinator",
                "phase": "aggregation",
                "round": 2,
                "required": True,
                "instruction_ids": [aggregate.id],
                "depends_on": ["worker-first", "worker-second"],
                "capability_ids": [delegate_id],
                "result_id": "result-aggregate",
                "result_schema": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
                "output_ids": [],
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
                        "reason": "Structural heading.",
                    }
                )
                continue
            node_id = {
                first.id: "worker-first",
                second.id: "worker-second",
                aggregate.id: "aggregate-all",
            }[unit.id]
            coverage.append(
                {
                    "instruction_id": unit.id,
                    "requirement": "required",
                    "disposition": "mapped",
                    "node_ids": [node_id],
                    "output_ids": [],
                    "reason": "",
                }
            )
        return {
            "schema_version": "1",
            "complete": True,
            "skill": {"name": "portable-workflow"},
            "documents": [document.binding_dict()],
            "capability_catalog_sha256": catalog["catalog_sha256"],
            "nodes": nodes,
            "coverage": coverage,
            "outputs": [],
            "policies": {
                "completion_policy": "all_required",
                "failure_policy": "fail_closed",
                "max_parallelism": 6,
                "max_iterations_per_node": 32,
            },
            "counts": {
                "documents": 1,
                "instruction_units": len(document.units),
                "nodes": 3,
                "coverage": len(document.units),
                "outputs": 0,
            },
        }

    def _workflow_plan(self, document):
        executable = [unit for unit in document.units if unit.kind != "heading"]
        first, second, aggregate = executable
        return {
            "schema_version": "1",
            "nodes": [
                {
                    "id": "worker-first",
                    "kind": "delegate",
                    "title": "First independent result",
                    "instruction_ranges": [{
                        "start_instruction_id": first.id,
                        "end_instruction_id": first.id,
                    }],
                    "depends_on": [],
                    "capability_ids": [],
                    "result_schema": {
                        "type": "object",
                        "required": ["finding"],
                        "properties": {"finding": {"type": "string"}},
                    },
                },
                {
                    "id": "worker-second",
                    "kind": "delegate",
                    "title": "Second independent result",
                    "instruction_ranges": [{
                        "start_instruction_id": second.id,
                        "end_instruction_id": second.id,
                    }],
                    "depends_on": [],
                    "capability_ids": [],
                },
                {
                    "id": "aggregate-all",
                    "kind": "aggregate",
                    "title": "Aggregate all prerequisites",
                    "instruction_ranges": [{
                        "start_instruction_id": aggregate.id,
                        "end_instruction_id": aggregate.id,
                    }],
                    "depends_on": ["worker-first", "worker-second"],
                    "capability_ids": [],
                },
            ],
        }

    def _ordinal_workflow_plan(self, document, catalog):
        plan = self._workflow_plan(document)
        catalog_document = catalog["instruction_plan_catalog"]["documents"][0]
        ordinal_by_id = {
            unit.id: unit_index + 1
            for unit_index, unit in enumerate(document.units)
        }
        for node in plan["nodes"]:
            node["instruction_ranges"] = [
                {
                    "document_id": catalog_document["document_id"],
                    "start_ordinal": ordinal_by_id[
                        item["start_instruction_id"]
                    ],
                    "end_ordinal": ordinal_by_id[item["end_instruction_id"]],
                }
                for item in node["instruction_ranges"]
            ]
        return plan

    def _validate(
        self,
        catalog,
        delegate_id,
        *,
        workflow_ir=None,
        workflow_plan=None,
        required=None,
        optional=None,
    ):
        return validate_capability_plan(
            catalog,
            skill_name="portable-workflow",
            body_sha256=catalog["body_sha256"],
            catalog_sha256=catalog["catalog_sha256"],
            required=[delegate_id] if required is None else required,
            optional=[] if optional is None else optional,
            unsupported=[],
            workflow_ir=workflow_ir,
            workflow_plan=workflow_plan,
        )

    def test_catalog_binds_and_projects_runtime_owned_instruction_units(self):
        _root, _main, document, catalog, _delegate_id = self._fixture()
        projection = catalog_prompt_payload(catalog)

        self.assertTrue(catalog["workflow_ir_required"])
        self.assertEqual(1, catalog["catalog_revision"])
        self.assertEqual((document,), catalog["instruction_documents"])
        self.assertTrue(projection["workflow_ir_required"])
        self.assertEqual(
            len(document.units),
            projection["workflow_plan_catalog"]["counts"]["instruction_units"],
        )
        self.assertNotIn(
            '"text":',
            json.dumps(projection["workflow_plan_catalog"], ensure_ascii=False),
        )
        self.assertNotIn(
            '"id":',
            json.dumps(projection["workflow_plan_catalog"], ensure_ascii=False),
        )
        self.assertIn(
            "document_id",
            projection["workflow_plan_catalog"]["documents"][0],
        )
        self.assertNotIn("instruction_documents", projection)
        self.assertIn("workflow_plan is mandatory", projection["instructions"])
        self.assertRegex(catalog["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(
            catalog["policy"]["workflow_plan_catalog_content_addressed"]
        )

    def test_compact_workflow_plan_is_compiled_before_grants_are_installed(self):
        _root, _main, document, catalog, delegate_id = self._fixture()

        result = self._validate(
            catalog,
            delegate_id,
            workflow_plan=self._workflow_plan(document),
        )

        self.assertTrue(result.valid, result.payload)
        self.assertEqual("accepted", result.payload["status"])
        self.assertNotIn("workflow_plan", result.payload)
        self.assertEqual(3, len(result.payload["workflow_ir"]["nodes"]))
        self.assertTrue(all(
            delegate_id in node["capability_ids"]
            for node in result.payload["workflow_ir"]["nodes"]
        ))
        self.assertEqual(
            ["worker-first", "worker-second"],
            result.payload["worker_plan"]["required_workers"],
        )

        ordinal_result = self._validate(
            catalog,
            delegate_id,
            workflow_plan=self._ordinal_workflow_plan(document, catalog),
        )
        self.assertTrue(ordinal_result.valid, ordinal_result.payload)
        self.assertEqual(
            result.payload["workflow_ir"]["ir_sha256"],
            ordinal_result.payload["workflow_ir"]["ir_sha256"],
        )

        conflict = self._validate(
            catalog,
            delegate_id,
            workflow_ir=self._workflow_payload(document, catalog, delegate_id),
            workflow_plan=self._workflow_plan(document),
        )
        self.assertFalse(conflict.valid)
        self.assertEqual(
            "capability_plan_workflow_payload_conflict",
            conflict.payload["error_code"],
        )

    def test_ordinal_workflow_plan_rejects_stale_outer_catalog_epoch(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        result = validate_capability_plan(
            catalog,
            skill_name="portable-workflow",
            body_sha256=catalog["body_sha256"],
            catalog_sha256="f" * 64,
            required=[delegate_id],
            optional=[],
            unsupported=[],
            workflow_plan=self._ordinal_workflow_plan(document, catalog),
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            "capability_plan_catalog_identity_mismatch",
            result.payload["error_code"],
        )

    def test_compact_plan_validation_error_exposes_stable_code_and_path(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        plan = self._workflow_plan(document)
        plan["nodes"][0]["instruction_ranges"][0][
            "start_instruction_id"
        ] = "iu-000000000000000000000000"

        result = self._validate(
            catalog,
            delegate_id,
            workflow_plan=plan,
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            "unknown_workflow_plan_instruction_id",
            result.payload["workflow_plan_error_code"],
        )
        self.assertIn(
            "instruction_ranges[0].start_instruction_id",
            result.payload["workflow_plan_error_path"],
        )

    def test_compact_plan_coverage_error_returns_model_writable_coordinate(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        plan = self._workflow_plan(document)
        executable = [unit for unit in document.units if unit.kind != "heading"]
        first, second, _aggregate = executable
        # Keep the graph structurally valid while leaving the first exact
        # instruction unmapped. The runtime error path contains an opaque ID
        # that the provider-facing schema deliberately cannot submit.
        plan["nodes"][0]["instruction_ranges"] = [{
            "start_instruction_id": second.id,
            "end_instruction_id": second.id,
        }]

        result = self._validate(
            catalog,
            delegate_id,
            workflow_plan=plan,
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            "runtime_required_instruction_unmapped",
            result.payload["workflow_plan_error_code"],
        )
        correction = result.payload["workflow_plan_correction"]
        expected_ordinal = list(document.units).index(first) + 1
        public_document = catalog["instruction_plan_catalog"]["documents"][0]
        self.assertEqual(public_document["document_id"], correction["document_id"])
        self.assertEqual(expected_ordinal, correction["start_ordinal"])
        self.assertEqual(expected_ordinal, correction["end_ordinal"])
        self.assertIn(first.text, correction["preview"])
        self.assertNotIn(
            first.id,
            result.payload["workflow_plan_error_path"],
        )
        self.assertEqual(
            f"coverage.{first.id}",
            result.payload["workflow_plan_internal_error_path"],
        )

    def test_agent_loop_catalog_requires_ir_for_declared_delegated_workflow(self):
        root, main, _document, _catalog, _delegate_id = self._fixture()
        main.write_text(
            """---
name: portable-workflow
description: A generic instruction-only workflow.
---
# 通用协作流程

1. Delegate the first result to an independent subagent.
2. Delegate the second result to another independent subagent.
3. 汇总所有前置结果。
""",
            encoding="utf-8",
        )
        package = load_skill_content(
            root / "SKILL.md",
            skill_dir=str(root),
        )

        catalog = _build_standard_skill_capability_catalog(
            "portable-workflow",
            package,
            ["skill_view", "submit_skill_capability_plan", "delegate_task"],
            (),
        )

        self.assertTrue(catalog["workflow_ir_required"])
        delegate = next(
            candidate
            for candidate in catalog["candidates"]
            if candidate.get("tool_name") == "delegate_task"
        )
        self.assertIn(
            [delegate["id"]],
            catalog["required_candidate_groups"],
        )
        self.assertEqual(
            package["skill_md_sha256"],
            catalog["instruction_documents"][0].source_sha256,
        )

    def test_delegated_workflow_activation_rejects_meta_and_description_prose(self):
        for content in (
            "# Usage\n\nExplain how to orchestrate agents.",
            "# Workflow\n\nThis skill coordinates agents for the user.",
            "# Steps\n\n- Ask how to delegate tasks to agents.",
        ):
            with self.subTest(content=content):
                self.assertFalse(
                    _standard_skill_declares_delegated_workflow(content)
                )

        for content in (
            "# Workflow\n\nYou must orchestrate independent agents.",
            "# Steps\n\n- Delegate the review to two workers.",
            "# 流程\n\n让子代理分别执行并复核结果。",
            """# Workflow

| Agent | Responsibilities | Output |
| --- | --- | --- |
| Evidence Agent | Review sources | Evidence table |
| Safety Agent | Review risks | Risk register |
| Coordinator Agent | Aggregate conclusions | Final report |

- Round 1: all Agents independently assess the inputs.
- Round 2: the Coordinator aggregates a consensus report.
""",
            """# 工作流

| Agent | 职责 | 核心输出 |
| --- | --- | --- |
| 证据 Agent | 复核资料 | 证据表 |
| 安全 Agent | 复核风险 | 风险清单 |
| Coordinator Agent | 汇总意见 | 最终报告 |

- 第一轮：各 Agent 独立评估。
- 第二轮：Coordinator Agent 汇总并生成最终报告。
""",
        ):
            with self.subTest(content=content):
                self.assertTrue(
                    _standard_skill_declares_delegated_workflow(content)
                )

        for content in (
            """# Workflow

This section explains that agents and coordinators exist in some systems.
It mentions Round 1 and Round 2 only as historical examples.
""",
            """# Steps

- Ask whether an Agent table or a second review round would be useful.
- Do not delegate any work.
""",
        ):
            with self.subTest(content=content):
                self.assertFalse(
                    _standard_skill_declares_delegated_workflow(content)
                )

    def test_workflow_ir_retains_loader_owned_artifact_authority(self):
        contract, required = _workflow_ir_artifact_output_contract({
            "artifact_patterns": [
                "reports/summary.md",
                "reports/summary.md",
            ],
            "requires_modular_artifacts": True,
        })

        self.assertTrue(required)
        self.assertEqual(
            ["reports/summary.md"],
            contract["declared_artifacts"],
        )
        self.assertTrue(contract["route_scoped"])
        self.assertEqual(
            ({}, False),
            _workflow_ir_artifact_output_contract({
                "artifact_patterns": [],
                "requires_modular_artifacts": False,
            }),
        )

    def test_required_ir_validates_selected_capabilities_and_returns_worker_plan(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        workflow = self._workflow_payload(document, catalog, delegate_id)

        result = self._validate(catalog, delegate_id, workflow_ir=workflow)

        self.assertTrue(result.valid, result.payload)
        self.assertTrue(result.payload["workflow_ir_required"])
        self.assertRegex(
            result.payload["workflow_ir"]["ir_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            ["worker-first", "worker-second"],
            result.payload["worker_plan"]["required_workers"],
        )
        self.assertEqual(
            "parallel",
            result.payload["worker_plan"]["waves"][0]["mode"],
        )
        self.assertEqual(
            ["aggregate-all"],
            [step["id"] for step in result.payload["worker_plan"]["aggregation_steps"]],
        )
        self.assertEqual(
            len(document.units),
            len(result.payload["instruction_coverage"]),
        )

    def test_reference_document_closes_delegate_only_instruction_authority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            case = _reference_workflow_case(
                Path(temp_dir) / "portable-workflow"
            )

            accepted = case["accepted"]
            self.assertTrue(accepted.valid, accepted.payload)
            worker = accepted.payload["worker_plan"]["workers"][
                "reference-worker"
            ]
            expected_sources = [
                {
                    "resource_path": document.source_path,
                    "sha256": document.source_sha256,
                }
                for document in case["documents"]
            ]
            self.assertEqual([], worker["tools"])
            self.assertEqual("SKILL.md", worker["file"])
            self.assertEqual(
                expected_sources,
                worker["instruction_source_bindings"],
            )
            self.assertEqual(
                expected_sources,
                accepted.payload[
                    "workflow_instruction_resource_bindings"
                ],
            )
            self.assertEqual(
                {
                    ("portable-workflow", "SKILL.md"),
                    ("portable-workflow", "references/worker.md"),
                },
                {
                    tuple(row)
                    for row in accepted.payload["allowed_skill_resources"]
                },
            )
            self.assertEqual(
                ["delegate_task"],
                [
                    binding["tool_name"]
                    for binding in worker["capability_bindings"]
                ],
            )

    def test_reference_instruction_document_requires_frozen_authority_grant(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            case = _reference_workflow_case(
                Path(temp_dir) / "portable-workflow"
            )
            with self.assertRaisesRegex(
                ValueError,
                "outside the exact content-addressed authority closure",
            ):
                build_capability_catalog(
                    skill_name="portable-workflow",
                    loaded_package=case["package"],
                    available_tools=["skill_view", "delegate_task"],
                    authority_documents=(),
                    instruction_documents=case["documents"],
                    workflow_ir_required=True,
                )

    def test_crlf_instruction_sources_use_raw_byte_digest_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            case = _reference_workflow_case(
                Path(temp_dir) / "portable-workflow",
                line_ending="\r\n",
            )
            accepted = case["accepted"]
            self.assertTrue(accepted.valid, accepted.payload)
            self.assertEqual(
                hashlib.sha256(case["main"].read_bytes()).hexdigest(),
                case["package"]["skill_md_sha256"],
            )
            context = ToolContext(
                user_id="u",
                session_id="s",
                skill_execution_resource_boundary=True,
                allowed_skill_resources=tuple(
                    tuple(row)
                    for row in accepted.payload["allowed_skill_resources"]
                ),
                skill_capability_catalog=case["catalog"],
            )
            bindings = accepted.payload["worker_plan"]["workers"][
                "reference-worker"
            ]["instruction_source_bindings"]
            with patch(
                "skills.scanner.resolve_skill_path",
                return_value=case["main"],
            ):
                self.assertIsNone(
                    _instruction_source_boundary_error(
                        bindings,
                        skill_name="portable-workflow",
                        context=context,
                    )
                )
                case["main"].write_bytes(
                    case["main"].read_bytes() + b"\r\nchanged\r\n"
                )
                self.assertIn(
                    "changed after capability-plan compilation",
                    _instruction_source_boundary_error(
                        bindings,
                        skill_name="portable-workflow",
                        context=context,
                    )
                    or "",
                )

    def test_worker_binding_preserves_exact_shared_bridge_authority(self):
        worker_plan = {
            "workers": {
                "worker-a": {
                    "capability_candidate_ids": [
                        "delegate",
                        "script-a",
                        "http-a",
                        "command-a",
                    ],
                    "local_resources": [],
                }
            },
            "aggregation_steps": [],
        }
        candidates = {
            "delegate": {
                "id": "delegate",
                "kind": "native_tool",
                "tool_name": "delegate_task",
            },
            "script-a": {
                "id": "script-a",
                "kind": "skill_script",
                "skill_name": "portable-workflow",
                "resource_path": "scripts/a.py",
                "sha256": "a" * 64,
                "package_sha256": "b" * 64,
                "tool_names": ["run_skill_script", "run_skill_process"],
                "sandbox_egress_url_prefixes": [
                    "https://script.example.test/v1/",
                ],
            },
            "http-a": {
                "id": "http-a",
                "kind": "skill_http_prefix",
                "skill_name": "portable-workflow",
                "tool_name": "skill_http_get",
                "url_prefix": "https://example.test/a/",
            },
            "command-a": {
                "id": "command-a",
                "kind": "declared_command",
                "skill_name": "portable-workflow",
                "command_id": "command-a",
                "executable": "tool-a",
                "fixed_argv": ["--json"],
                "sandbox_egress_url_prefixes": [
                    "https://command.example.test/v1/",
                ],
            },
        }

        result = _bind_worker_plan_capabilities(
            worker_plan,
            candidates=candidates,
            skill_name="portable-workflow",
        )

        worker = result["workers"]["worker-a"]
        bindings = {
            item["candidate_id"]: item
            for item in worker["capability_bindings"]
        }
        self.assertNotIn(
            "delegate_task",
            [item["tool"] for item in worker["tools"]],
        )
        self.assertEqual("scripts/a.py", bindings["script-a"]["resource_path"])
        self.assertEqual("a" * 64, bindings["script-a"]["sha256"])
        self.assertEqual(
            "https://example.test/a/",
            bindings["http-a"]["url_prefix"],
        )
        self.assertEqual(
            ["--json"],
            bindings["command-a"]["fixed_argv"],
        )
        self.assertEqual(
            ["https://script.example.test/v1/"],
            bindings["script-a"]["sandbox_egress_url_prefixes"],
        )
        self.assertEqual(
            ["https://command.example.test/v1/"],
            bindings["command-a"]["sandbox_egress_url_prefixes"],
        )
        self.assertRegex(
            worker["capability_bindings_sha256"],
            r"^[0-9a-f]{64}$",
        )

    def test_required_ir_omission_always_fails_without_implicit_cache(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        missing = self._validate(catalog, delegate_id)
        self.assertFalse(missing.valid)
        self.assertEqual(
            "capability_plan_workflow_ir_required",
            missing.payload["error_code"],
        )

        workflow = self._workflow_payload(document, catalog, delegate_id)
        first = self._validate(catalog, delegate_id, workflow_ir=workflow)
        self.assertTrue(first.valid, first.payload)
        second = self._validate(catalog, delegate_id)
        self.assertFalse(second.valid)
        self.assertEqual(
            "capability_plan_workflow_ir_required",
            second.payload["error_code"],
        )

    def test_ir_cannot_use_an_unselected_catalog_capability(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        workflow = self._workflow_payload(document, catalog, delegate_id)

        result = self._validate(
            catalog,
            delegate_id,
            workflow_ir=workflow,
            required=[],
            optional=[],
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            "capability_plan_workflow_ir_unselected_capability",
            result.payload["error_code"],
        )
        self.assertEqual(
            "unknown_capability_id",
            result.payload["workflow_ir_error_code"],
        )

    def test_mandatory_delegate_cannot_be_only_optional(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        workflow = self._workflow_payload(document, catalog, delegate_id)

        result = self._validate(
            catalog,
            delegate_id,
            workflow_ir=workflow,
            required=[],
            optional=[delegate_id],
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            "capability_plan_workflow_delegate_not_required",
            result.payload["error_code"],
        )

    def test_cycle_and_instruction_coverage_omission_remain_fail_closed(self):
        _root, _main, document, catalog, delegate_id = self._fixture()

        workflow = self._workflow_payload(document, catalog, delegate_id)
        workflow["nodes"][0]["depends_on"] = ["aggregate-all"]
        cyclic = self._validate(catalog, delegate_id, workflow_ir=workflow)
        self.assertFalse(cyclic.valid)
        self.assertEqual("workflow_cycle", cyclic.payload["workflow_ir_error_code"])

        workflow = self._workflow_payload(document, catalog, delegate_id)
        workflow["coverage"].pop()
        workflow["counts"]["coverage"] -= 1
        omitted = self._validate(catalog, delegate_id, workflow_ir=workflow)
        self.assertFalse(omitted.valid)
        self.assertEqual(
            "instruction_coverage_omission",
            omitted.payload["workflow_ir_error_code"],
        )

    def test_mandatory_ir_cannot_dismiss_all_executable_units(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        workflow = self._workflow_payload(document, catalog, delegate_id)
        workflow["nodes"] = []
        workflow["outputs"] = []
        workflow["counts"]["nodes"] = 0
        workflow["counts"]["outputs"] = 0
        for item in workflow["coverage"]:
            item.update(
                {
                    "requirement": "advisory",
                    "disposition": "not_applicable",
                    "node_ids": [],
                    "output_ids": [],
                    "reason": "Planner claims this does not apply.",
                }
            )

        result = self._validate(
            catalog,
            delegate_id,
            workflow_ir=workflow,
        )

        self.assertFalse(result.valid)
        self.assertIn(
            result.payload["workflow_ir_error_code"],
            {"missing_nodes", "runtime_required_instruction_unmapped"},
        )

    def test_every_required_node_must_bind_required_delegate_controller(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        workflow = self._workflow_payload(document, catalog, delegate_id)
        workflow["nodes"][0]["capability_ids"] = []

        result = self._validate(
            catalog,
            delegate_id,
            workflow_ir=workflow,
        )

        self.assertFalse(result.valid)
        self.assertEqual(
            "capability_plan_workflow_ir_invalid",
            result.payload["error_code"],
        )
        self.assertEqual(
            "missing_capability_binding",
            result.payload["workflow_ir_error_code"],
        )

    def test_runtime_schedules_and_verifies_every_ir_node_receipt(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        workflow = self._workflow_payload(document, catalog, delegate_id)
        accepted = self._validate(
            catalog,
            delegate_id,
            workflow_ir=workflow,
        )
        self.assertTrue(accepted.valid, accepted.payload)
        plan = accepted.payload["worker_plan"]
        state = HarnessRunState(
            user_id="u",
            session_id="s",
            available_tools={"delegate_task"},
        )
        state.session_skill_names = {"portable-workflow"}
        state.viewed_skill_names = {"portable-workflow"}
        state.skill_capability_catalogs["portable-workflow"] = catalog
        state.skill_capability_plans["portable-workflow"] = accepted.payload
        state.skill_execution_plans["portable-workflow"] = plan
        state.skill_workflow_contracts["portable-workflow"] = {
            "source": "workflow_ir",
            "workflow_ir_sha256": accepted.payload["workflow_ir"][
                "ir_sha256"
            ],
        }

        pending, reason = state.needs_more_skill_workflow()
        self.assertTrue(pending)
        self.assertIn("delegate workflow stage", reason)
        self.assertIn("worker-first", reason)
        self.assertIn("worker-second", reason)
        blockers = _workflow_contract_findings(state)
        self.assertTrue(any(
            item["category"] == "workflow_ir_execution_coverage"
            and item["severity"] == "blocker"
            for item in blockers
        ))

        digest = "a" * 64
        state.skill_completed_workers["portable-workflow"] = {
            "worker-first",
            "worker-second",
        }
        state.skill_worker_results["portable-workflow"] = {
            worker_id: {
                "status": "completed",
                "completion_quality": "complete",
                "result_sha256": digest,
            }
            for worker_id in ("worker-first", "worker-second")
        }
        pending, reason = state.needs_more_skill_workflow()
        self.assertTrue(pending)
        self.assertIn("delegate aggregation step", reason)

        state.skill_completed_aggregation["portable-workflow"] = {
            "aggregate-all"
        }
        state.skill_aggregation_results["portable-workflow"] = {
            "aggregate-all": {
                "status": "completed",
                "completion_quality": "complete",
                "result_sha256": "b" * 64,
            }
        }
        self.assertEqual((False, ""), state.needs_more_skill_workflow())
        self.assertFalse(any(
            item["category"].startswith("workflow_ir")
            for item in _workflow_contract_findings(state)
        ))

        plan["requires_artifact_output"] = True
        plan["output_contract"] = {
            "declared_artifacts": ["reports/final.md"],
            "route_scoped": True,
        }
        state.skill_workflow_contracts["portable-workflow"].update({
            "output_contract": copy.deepcopy(plan["output_contract"]),
            "requires_modular_artifacts": True,
        })
        pending, reason = state.needs_more_skill_workflow()
        self.assertTrue(pending)
        self.assertIn("generate declared artifacts", reason)
        self.assertTrue(any(
            item["category"] == "workflow_ir_artifact_plan"
            and item["severity"] == "blocker"
            for item in _workflow_contract_findings(state)
        ))

    def test_valid_but_non_child_graph_is_not_silently_rerouted(self):
        _root, _main, document, catalog, delegate_id = self._fixture()
        workflow = self._workflow_payload(document, catalog, delegate_id)
        workflow["nodes"][0]["kind"] = "tool"
        workflow["nodes"][0]["executor"] = "native_tool"

        result = self._validate(catalog, delegate_id, workflow_ir=workflow)

        self.assertFalse(result.valid)
        self.assertEqual(
            "capability_plan_workflow_ir_not_lowerable",
            result.payload["error_code"],
        )
        self.assertEqual(
            "unsupported_executor_for_worker_plan",
            result.payload["workflow_ir_error_code"],
        )

    def test_simple_catalog_remains_compatible_and_cannot_accept_unissued_ir(self):
        _root, _main, document, workflow_catalog, delegate_id = self._fixture()
        simple = build_capability_catalog(
            skill_name="portable-workflow",
            loaded_package={
                "content": "Do the task.",
                "frontmatter": {"name": "portable-workflow"},
                "linked_files": {},
            },
            available_tools=["skill_view", "delegate_task"],
        )
        simple_delegate = next(
            candidate["id"]
            for candidate in simple["candidates"]
            if candidate.get("tool_name") == "delegate_task"
        )
        accepted = validate_capability_plan(
            simple,
            skill_name="portable-workflow",
            body_sha256=simple["body_sha256"],
            required=[simple_delegate],
            optional=[],
            unsupported=[],
        )
        self.assertTrue(accepted.valid, accepted.payload)
        self.assertNotIn("workflow_ir", accepted.payload)
        self.assertNotIn("instruction_catalog", catalog_prompt_payload(simple))

        unauthorized = validate_capability_plan(
            simple,
            skill_name="portable-workflow",
            body_sha256=simple["body_sha256"],
            required=[simple_delegate],
            optional=[],
            unsupported=[],
            workflow_ir=self._workflow_payload(document, workflow_catalog, delegate_id),
        )
        self.assertFalse(unauthorized.valid)
        self.assertEqual(
            "capability_plan_workflow_ir_not_authorized",
            unauthorized.payload["error_code"],
        )

    def test_instruction_documents_must_match_exact_skill_authority(self):
        _root, _main, _document, catalog, _delegate_id = self._fixture()
        different = canonicalize_skill_markdown(
            "# Different instructions\n\n1. do something else\n",
            source_path="SKILL.md",
        )
        with self.assertRaises(ValueError):
            build_capability_catalog(
                skill_name="portable-workflow",
                loaded_package={
                    "content": "body",
                    "frontmatter": {"name": "portable-workflow"},
                    "linked_files": {},
                    "skill_md_sha256": catalog["body_sha256"],
                },
                available_tools=["skill_view", "delegate_task"],
                instruction_documents=(different,),
                workflow_ir_required=True,
            )

        with self.assertRaises(ValueError):
            build_capability_catalog(
                skill_name="portable-workflow",
                loaded_package={
                    "content": "body",
                    "frontmatter": {"name": "portable-workflow"},
                    "linked_files": {},
                    "skill_md_sha256": catalog["body_sha256"],
                },
                available_tools=["skill_view"],
                workflow_ir_required=True,
            )

    def test_submit_tool_schema_is_optional_at_root_and_strict_when_present(self):
        parameters = SUBMIT_SKILL_CAPABILITY_PLAN_SCHEMA["parameters"]
        self.assertNotIn("workflow_ir", parameters["required"])
        self.assertNotIn("workflow_plan", parameters["required"])
        compact_schema = parameters["properties"]["workflow_plan"]
        self.assertFalse(compact_schema["additionalProperties"])
        self.assertNotIn("coverage", compact_schema["properties"])
        workflow_schema = parameters["properties"]["workflow_ir"]
        self.assertFalse(workflow_schema["additionalProperties"])
        self.assertEqual(512, workflow_schema["properties"]["nodes"]["maxItems"])
        self.assertFalse(
            workflow_schema["properties"]["nodes"]["items"]["additionalProperties"]
        )
        self.assertFalse(
            workflow_schema["properties"]["coverage"]["items"]["additionalProperties"]
        )
        self.assertEqual(16, workflow_schema["properties"]["documents"]["maxItems"])
        self.assertEqual(
            64,
            workflow_schema["properties"]["policies"]["properties"]["max_parallelism"][
                "maximum"
            ],
        )


class CapabilityWorkflowIRToolTests(unittest.IsolatedAsyncioTestCase):
    def _planned_worker_task_and_context(
        self,
        case: dict,
    ) -> tuple[dict, ToolContext]:
        accepted = case["accepted"]
        self.assertTrue(accepted.valid, accepted.payload)
        worker = accepted.payload["worker_plan"]["workers"][
            "reference-worker"
        ]
        task = {
            "goal": "execute the exact reference-backed worker",
            "skill_name": "portable-workflow",
            "worker_id": "reference-worker",
            "worker_file": worker["file"],
            "step_type": "worker",
            "step_id": "reference-worker",
            "workflow_stage": "analysis",
            "tools": [],
            "required_skill_files_to_inspect": worker["local_resources"],
            "required_instruction_source_bindings": worker[
                "instruction_source_bindings"
            ],
            "capability_bindings": worker["capability_bindings"],
            "capability_bindings_sha256": worker[
                "capability_bindings_sha256"
            ],
        }
        context = ToolContext(
            user_id="u",
            session_id="s",
            model_id="model",
            provider_config={
                "base_url": "http://example",
                "api_model": "model",
                "context_length": 303_872,
            },
            enabled_tools=(),
            skill_execution_resource_boundary=True,
            allowed_skill_resources=tuple(
                tuple(row)
                for row in accepted.payload["allowed_skill_resources"]
            ),
            skill_capability_catalog=case["catalog"],
        )
        return task, context

    async def test_skill_main_mutation_after_plan_fails_before_child_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            case = _reference_workflow_case(
                Path(temp_dir) / "portable-workflow"
            )
            task, context = self._planned_worker_task_and_context(case)
            case["main"].write_bytes(
                case["main"].read_bytes() + b"\nchanged after plan\n"
            )
            with (
                patch(
                    "skills.scanner.resolve_skill_path",
                    return_value=case["main"],
                ),
                patch("agent_loop.run_stream") as run_stream,
                patch(
                    "tools.delegation._load_complete_skill_view_preload",
                    new_callable=AsyncMock,
                ) as preload,
                patch(
                    "tools.delegation.persist_result_for_history"
                ) as persist,
            ):
                result = await _run_child(task, context, 0)

        self.assertEqual("error", result["status"])
        self.assertIn(
            "SKILL.md changed after capability-plan compilation",
            result["error"],
        )
        run_stream.assert_not_called()
        preload.assert_not_called()
        persist.assert_not_called()

    async def test_authority_reference_mutation_after_plan_fails_before_child_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            case = _reference_workflow_case(
                Path(temp_dir) / "portable-workflow"
            )
            task, context = self._planned_worker_task_and_context(case)
            case["reference"].write_bytes(
                case["reference"].read_bytes() + b"\nchanged after plan\n"
            )
            with (
                patch(
                    "skills.scanner.resolve_skill_path",
                    return_value=case["main"],
                ),
                patch("agent_loop.run_stream") as run_stream,
                patch(
                    "tools.delegation._load_complete_skill_view_preload",
                    new_callable=AsyncMock,
                ) as preload,
                patch(
                    "tools.delegation.persist_result_for_history"
                ) as persist,
            ):
                result = await _run_child(task, context, 0)

        self.assertEqual("error", result["status"])
        self.assertIn(
            (
                "references/worker.md changed after capability-plan "
                "compilation"
            ),
            result["error"],
        )
        run_stream.assert_not_called()
        preload.assert_not_called()
        persist.assert_not_called()

    async def test_submit_tool_revalidates_and_returns_compiled_workflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-workflow"
            root.mkdir()
            main = root / "SKILL.md"
            main.write_text(
                """---
name: portable-workflow
description: Generic workflow.
---
# Workflow

1. Produce one result.
2. Produce another result.
3. Aggregate both results.
""",
                encoding="utf-8",
            )
            package = load_skill_content(main, skill_dir=str(root))
            document = canonicalize_skill_markdown(main.read_text(encoding="utf-8"))
            catalog = build_capability_catalog(
                skill_name="portable-workflow",
                loaded_package=package,
                available_tools=["skill_view", "delegate_task"],
                instruction_documents=(document,),
                workflow_ir_required=True,
            )
            delegate_id = next(
                candidate["id"]
                for candidate in catalog["candidates"]
                if candidate.get("tool_name") == "delegate_task"
            )
            helper = CapabilityWorkflowIRTests()
            helper.maxDiff = self.maxDiff
            workflow = helper._workflow_payload(document, catalog, delegate_id)
            context = ToolContext(
                user_id="u",
                session_id="s",
                enabled_tools=("submit_skill_capability_plan",),
                skill_capability_catalog=catalog,
            )
            with patch("skills.scanner.resolve_skill_path", return_value=main):
                raw = await submit_skill_capability_plan(
                    skill_name="portable-workflow",
                    body_sha256=catalog["body_sha256"],
                    catalog_sha256=catalog["catalog_sha256"],
                    required=[delegate_id],
                    optional=[],
                    unsupported=[],
                    workflow_ir=workflow,
                    context=context,
                )

        result = json.loads(raw)
        self.assertEqual("accepted", result["status"])
        self.assertEqual(
            ["worker-first", "worker-second"],
            result["worker_plan"]["required_workers"],
        )
        self.assertRegex(result["workflow_ir"]["ir_sha256"], r"^[0-9a-f]{64}$")

    async def test_submit_tool_detects_instruction_document_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "portable-workflow"
            root.mkdir()
            main = root / "SKILL.md"
            main.write_text(
                """---
name: portable-workflow
description: Generic workflow.
---
# Workflow

1. Produce one result.
2. Produce another result.
3. Aggregate both results.
""",
                encoding="utf-8",
            )
            package = load_skill_content(main, skill_dir=str(root))
            document = canonicalize_skill_markdown(main.read_text(encoding="utf-8"))
            catalog = build_capability_catalog(
                skill_name="portable-workflow",
                loaded_package=package,
                available_tools=["skill_view", "delegate_task"],
                instruction_documents=(document,),
                workflow_ir_required=True,
            )
            delegate_id = next(
                candidate["id"]
                for candidate in catalog["candidates"]
                if candidate.get("tool_name") == "delegate_task"
            )
            helper = CapabilityWorkflowIRTests()
            helper.maxDiff = self.maxDiff
            workflow = helper._workflow_payload(document, catalog, delegate_id)
            context = ToolContext(
                user_id="u",
                session_id="s",
                enabled_tools=("submit_skill_capability_plan",),
                skill_capability_catalog=catalog,
            )
            main.write_text(
                main.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
            )
            with patch("skills.scanner.resolve_skill_path", return_value=main):
                raw = await submit_skill_capability_plan(
                    skill_name="portable-workflow",
                    body_sha256=catalog["body_sha256"],
                    catalog_sha256=catalog["catalog_sha256"],
                    required=[delegate_id],
                    optional=[],
                    unsupported=[],
                    workflow_ir=workflow,
                    context=context,
                )

        result = json.loads(raw)
        self.assertEqual("error", result["status"])
        self.assertEqual("capability_plan_document_changed", result["error_code"])


if __name__ == "__main__":
    unittest.main()
