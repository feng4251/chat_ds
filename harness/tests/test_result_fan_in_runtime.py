import asyncio
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import result_fan_in_runtime as fan_in_runtime
from result_fan_in import plan_persisted_result_fan_in
from result_fan_in_runtime import (
    FanInExecutionError,
    ReductionRequest,
    build_length_finalization_prompt,
    materialize_fan_in_plan,
)


def _audited_reduction(prompt: str) -> str:
    records = json.loads(prompt.split("UNTRUSTED_INPUT_RECORDS_JSON:\n", 1)[1])
    ledger = {
        "version": 1,
        "sources": [_coverage_record(record) for record in records],
    }
    return "\n".join([
        "Ordered evidence, citations, conflicts, gaps, and provenance were retained.",
        "FAN_IN_COVERAGE_JSON:"
        + json.dumps(ledger, ensure_ascii=False, separators=(",", ":")),
    ])


def _coverage_record(record: dict, *, status: str = "present") -> dict:
    value = {
        "input_id": record["input_id"],
        "status": status,
        "provenance": {
            "path": record["path"],
            "checksum_sha256": record["checksum_sha256"],
            "source_range": record["source_range"],
        },
        "segment_coverage": {
            "byte_start": 0,
            "byte_end": record["byte_size"],
        },
    }
    if status == "degraded":
        value["reason"] = "The semantic reducer explicitly marked this input degraded."
    return value


def _runtime_input(input_id: str, content: str = "evidence"):
    return fan_in_runtime._InputBody(
        input_id=input_id,
        path=f"results/{input_id}.md",
        checksum_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        source_start=0,
        source_end=1,
        content=content,
    )


def _footer_for_inputs(inputs, *, records=None) -> str:
    if records is None:
        records = [
            {
                "input_id": item.input_id,
                "status": "present",
                "provenance": {
                    "path": item.path,
                    "checksum_sha256": item.checksum_sha256,
                    "source_range": [item.source_start, item.source_end],
                },
                "segment_coverage": {
                    "byte_start": 0,
                    "byte_end": len(item.content.encode("utf-8")),
                },
            }
            for item in inputs
        ]
    return (
        "Faithful semantic body.\nFAN_IN_COVERAGE_JSON:"
        + json.dumps(
            {"version": 1, "sources": records},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


class ResultFanInRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_length_finalization_is_complete_replacement_with_same_inputs(self):
        original = (
            "[Harness internal bounded fan-in reduction]\n"
            "UNTRUSTED_INPUT_RECORDS_JSON:\n"
            '[{"input_id":"renamed-source","content":"value"}]'
        )
        request = ReductionRequest(
            request_id="request-renamed",
            step_id="step-renamed",
            prompt=original,
            max_output_tokens=32_768,
            max_output_bytes=32_768,
            minimum_output_bytes=2_048,
        )

        prompt = build_length_finalization_prompt(request)

        self.assertIn("discarded in full", prompt)
        self.assertIn("complete replacement", prompt)
        self.assertIn("exactly one complete terminal coverage ledger", prompt)
        self.assertTrue(prompt.endswith(original))
        self.assertEqual(1, prompt.count("renamed-source"))
        self.assertLess(
            len(prompt.encode("utf-8")) - len(original.encode("utf-8")),
            fan_in_runtime.REDUCTION_PROMPT_RESERVE_BYTES,
        )

    def test_runtime_defaults_bound_generic_reducer_outputs(self):
        self.assertEqual(fan_in_runtime.DEFAULT_REDUCTION_OUTPUT_TOKENS, 8 * 1024)
        self.assertEqual(fan_in_runtime.DEFAULT_REDUCTION_OUTPUT_BYTES, 32 * 1024)

    def test_reduction_prompt_is_domain_neutral_for_non_research_records(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"inventory-{index}",
                    "path": f"results/inventory-{index}.json",
                    "content": (
                        '{"sku":"A-%d","quantity":%d,"bins":[1,2,3]}\n'
                        % (index, index + 1)
                    ) * 800,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        self.assertTrue(plan.reduction_steps)
        step = plan.reduction_steps[0]
        inputs = [_runtime_input("inventory", '{"sku":"A","quantity":2}')]

        prompt = fan_in_runtime._reduction_prompt(
            plan,
            step_id=step.step_id,
            output=step.output,
            inputs=inputs,
            execution_manifest_path="results/execution.json",
            max_semantic_bytes=4_096,
        )

        self.assertIn("domain-neutral semantic-reduction", prompt)
        self.assertIn("actual schema and meanings", prompt)
        self.assertIn("Do not invent citations", prompt)
        self.assertNotIn("explicitly retains citations", prompt)
        self.assertNotIn("degraded/unavailable evidence", prompt)

    async def test_total_exact_source_memory_ceiling_fails_before_reducer(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"bounded-{index}",
                    "path": f"results/bounded-{index}.md",
                    "content": "generic evidence " * 2_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        reducer = AsyncMock()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            fan_in_runtime,
            "MAX_TOTAL_EXACT_RESULT_BYTES",
            1_000,
        ):
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(
                FanInExecutionError,
                "total exact-source ceiling",
            ):
                await materialize_fan_in_plan(
                    plan,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=5,
                )
        reducer.assert_not_awaited()

    async def test_rolling_plan_materializes_every_step_and_manifest(self):
        contents = [
            f"generic-source-{index}\n" + (f"evidence-{index} " * 800)
            for index in range(6)
        ]
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"source-{index}",
                    "path": f"results/source-{index}.md",
                    "content": body,
                    "provenance": {"ordinal": index},
                }
                for index, body in enumerate(contents)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        self.assertTrue(plan.requires_reduction)
        calls = []

        async def reducer(request):
            calls.append(request)
            return _audited_reduction(request.prompt)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            materialized = await materialize_fan_in_plan(
                plan,
                results_root=root,
                reducer=reducer,
                timeout_seconds=5,
            )
            final = root / materialized.final_path[len("results/") :]
            manifest = root / materialized.source_manifest_path[len("results/") :]
            execution = root / materialized.execution_manifest_path[len("results/") :]
            self.assertTrue(final.is_file())
            self.assertTrue(manifest.is_file())
            self.assertTrue(execution.is_file())
            self.assertEqual(
                hashlib.sha256(final.read_bytes()).hexdigest(),
                materialized.final_checksum_sha256,
            )
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                [record["path"] for record in manifest_data["sources"]],
                [f"results/source-{index}.md" for index in range(6)],
            )
            self.assertEqual(len(calls), len(plan.reduction_steps))
            self.assertEqual(
                (materialized.artifacts[-1].source_start,
                 materialized.artifacts[-1].source_end),
                (0, len(contents)),
            )
            self.assertEqual(
                materialized.source_ids,
                tuple(f"source-{index}" for index in range(6)),
            )
            execution_data = json.loads(execution.read_text(encoding="utf-8"))
            self.assertEqual(
                execution_data["final_source_ids"],
                [f"source-{index}" for index in range(6)],
            )
            self.assertTrue(all(
                receipt["source_ids"]
                for receipt in execution_data["artifacts"]
            ))

    async def test_balanced_waves_execute_concurrently_and_keep_deterministic_lineage(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"shipment-{index}",
                    "path": f"results/shipment-{index}.json",
                    "content": (f'{{"shipment":{index},"state":"ready"}}\n' * 180),
                }
                for index in range(4)
            ],
            token_allowance=2_200,
            byte_allowance=12_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        self.assertEqual(
            [len([step for step in plan.reduction_steps if step.wave == wave])
             for wave in sorted({step.wave for step in plan.reduction_steps})],
            [4, 2, 1],
        )
        active = 0
        maximum_active = 0
        requests = []
        lock = asyncio.Lock()

        async def reducer(request):
            nonlocal active, maximum_active
            requests.append(request)
            async with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.02)
                return _audited_reduction(request.prompt)
            finally:
                async with lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            materialized = await materialize_fan_in_plan(
                plan,
                results_root=root,
                reducer=reducer,
                timeout_seconds=5,
                step_timeout_seconds=1,
                max_wave_concurrency=4,
            )
        self.assertGreaterEqual(maximum_active, 4)
        self.assertEqual(len(requests), len(plan.reduction_steps))
        self.assertTrue(all(
            request.timeout_seconds is not None
            and 0 < request.timeout_seconds <= 1
            and request.minimum_output_bytes > 0
            for request in requests
        ))
        self.assertEqual(
            materialized.source_ids,
            tuple(f"shipment-{index}" for index in range(4)),
        )
        # Receipts are emitted by planned ordinal, not completion order.
        self.assertEqual(
            [item.artifact_id for item in materialized.artifacts],
            [step.output.artifact_id for step in plan.reduction_steps],
        )

    async def test_execution_namespace_prevents_concurrent_artifact_overwrite(self):
        records = [
            {
                "result_id": f"account-{index}",
                "path": f"results/account-{index}.json",
                "content": (f'{{"account":"A{index}","balance":-7.25}}\n' * 180),
            }
            for index in range(2)
        ]

        def build(namespace: str):
            return plan_persisted_result_fan_in(
                records,
                token_allowance=1_500,
                byte_allowance=8_000,
                reduction_output_tokens=800,
                reduction_output_bytes=4_000,
                execution_namespace=namespace,
            )

        first_plan = build("concurrent-a")
        second_plan = build("concurrent-b")

        async def first_reducer(request):
            return "FIRST EXECUTION\n" + _audited_reduction(request.prompt)

        async def second_reducer(request):
            return "SECOND EXECUTION\n" + _audited_reduction(request.prompt)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            first, second = await asyncio.gather(
                materialize_fan_in_plan(
                    first_plan,
                    results_root=root,
                    reducer=first_reducer,
                    timeout_seconds=5,
                ),
                materialize_fan_in_plan(
                    second_plan,
                    results_root=root,
                    reducer=second_reducer,
                    timeout_seconds=5,
                ),
            )

            self.assertNotEqual(first.final_path, second.final_path)
            for materialized in (first, second):
                disk = root / materialized.final_path[len("results/") :]
                self.assertEqual(
                    hashlib.sha256(disk.read_bytes()).hexdigest(),
                    materialized.final_checksum_sha256,
                )

    async def test_dag_cycle_is_rejected_before_manifest_or_reducer(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"node-{index}",
                    "path": f"results/node-{index}.md",
                    "content": "graph input " * 1_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        first, second, terminal = plan.reduction_steps
        first_output = replace(
            first.output,
            immediate_input_ids=(second.output.artifact_id,),
        )
        second_output = replace(
            second.output,
            immediate_input_ids=(first.output.artifact_id,),
        )
        first = replace(
            first,
            input_batch=replace(first.input_batch, items=(second_output,)),
            output=first_output,
        )
        second = replace(
            second,
            input_batch=replace(second.input_batch, items=(first_output,)),
            output=second_output,
        )
        terminal = replace(
            terminal,
            input_batch=replace(
                terminal.input_batch,
                items=(first_output, second_output),
            ),
        )
        cyclic = replace(plan, reduction_steps=(first, second, terminal))
        reducer = AsyncMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "contains a cycle"):
                await materialize_fan_in_plan(
                    cyclic,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=5,
                )
            manifest = root / cyclic.source_manifest.path[len("results/") :]
            self.assertFalse(manifest.exists())
            reducer.assert_not_awaited()

    async def test_reserved_execution_manifest_path_is_rejected_before_reducer(self):
        plan = plan_persisted_result_fan_in(
            [{
                "result_id": "forged-path-source",
                "path": "results/forged-path.md",
                "content": "immutable evidence " * 1_000,
            }],
            token_allowance=1_000,
            byte_allowance=4_000,
            reduction_output_tokens=400,
            reduction_output_bytes=2_000,
            execution_namespace="forged-path-test",
        )
        self.assertEqual(len(plan.reduction_steps), 1)
        step = plan.reduction_steps[0]
        plan_directory = f"results/.chatds/fan_in/{plan.plan_id}/"
        for suffix in (
            "execution_manifest.json",
            "failure.json",
            "stream_10000_segment_10000.md",
            "stream_10000_rolling_10000.md",
        ):
            with self.subTest(suffix=suffix):
                forged_output = replace(
                    step.output,
                    path=plan_directory + suffix,
                )
                forged_step = replace(step, output=forged_output)
                forged_plan = replace(
                    plan,
                    reduction_steps=(forged_step,),
                    final_artifact=forged_output,
                )
                reducer = AsyncMock()

                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir) / "results"
                    root.mkdir()
                    with self.assertRaisesRegex(
                        FanInExecutionError,
                        "reserved runtime path",
                    ):
                        await materialize_fan_in_plan(
                            forged_plan,
                            results_root=root,
                            reducer=reducer,
                            timeout_seconds=5,
                        )
                reducer.assert_not_awaited()

    async def test_orphan_step_is_rejected_before_reducer(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"build-{index}",
                    "path": f"results/build-{index}.log",
                    "content": "compiler output " * 2_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        template = plan.reduction_steps[0]
        orphan_output = replace(
            template.output,
            artifact_id=template.output.artifact_id + "-orphan",
            path=template.output.path.replace(".md", "_orphan.md"),
        )
        orphan = replace(
            template,
            step_id=template.step_id + "-orphan",
            ordinal=max(step.ordinal for step in plan.reduction_steps) + 1,
            output=orphan_output,
        )
        malformed = replace(
            plan,
            reduction_steps=plan.reduction_steps + (orphan,),
        )
        reducer = AsyncMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "outside final-artifact lineage"):
                await materialize_fan_in_plan(
                    malformed,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=5,
                )
        reducer.assert_not_awaited()

    async def test_single_oversize_source_has_contiguous_complete_byte_coverage(self):
        body = "α证据 citation-X conflict gap\n" * 1_200
        plan = plan_persisted_result_fan_in(
            [{
                "result_id": "large-generic-source",
                "path": "results/large.md",
                "content": body,
            }],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        self.assertEqual(plan.oversize_result_ids, ("large-generic-source",))

        async def reducer(request):
            return _audited_reduction(request.prompt)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            materialized = await materialize_fan_in_plan(
                plan,
                results_root=root,
                reducer=reducer,
                timeout_seconds=5,
            )
            coverage = list(materialized.segment_coverage)
            self.assertGreater(len(coverage), 1)
            self.assertEqual(coverage[0]["byte_start"], 0)
            self.assertEqual(coverage[-1]["byte_end"], len(body.encode("utf-8")))
            for previous, current in zip(coverage, coverage[1:]):
                self.assertEqual(previous["byte_end"], current["byte_start"])
            self.assertTrue(all(
                record["source_checksum_sha256"]
                == hashlib.sha256(body.encode("utf-8")).hexdigest()
                for record in coverage
            ))
            execution = json.loads(
                (root / materialized.execution_manifest_path[len("results/") :])
                .read_text(encoding="utf-8")
            )
            self.assertTrue(execution["lossy_semantic_reduction"])
            self.assertEqual(
                execution["coverage_scope"],
                "source_participation_and_provenance",
            )
            self.assertEqual(execution["segment_coverage"], coverage)
            self.assertIn(
                "\nFAN_IN_COVERAGE_JSON:",
                materialized.final_content,
            )

    def test_read_only_schedule_estimate_expands_exact_calls_and_wave_cohorts(self):
        body = "α证据 citation-X conflict gap\n" * 2_000
        plan = plan_persisted_result_fan_in(
            [{
                "result_id": "large-schedule-source",
                "path": "results/large-schedule.md",
                "content": body,
            }],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        with patch.object(fan_in_runtime, "_atomic_write_declared") as writer, patch.object(
            fan_in_runtime,
            "_reduce_and_write",
        ) as reducer_dispatch:
            estimate = fan_in_runtime.estimate_fan_in_reducer_schedule(
                plan,
                max_wave_concurrency=2,
            )

        writer.assert_not_called()
        reducer_dispatch.assert_not_called()
        self.assertEqual(len(estimate.steps), 1)
        step = estimate.steps[0]
        self.assertGreater(step.segment_call_count, 1)
        self.assertEqual(step.merge_call_count, step.segment_call_count - 1)
        self.assertEqual(
            step.reducer_call_count,
            step.segment_call_count + step.merge_call_count,
        )
        self.assertEqual(estimate.reducer_call_count, step.reducer_call_count)
        self.assertEqual(estimate.critical_call_cohorts, step.reducer_call_count)
        self.assertEqual(
            estimate.waves[0].cohort_critical_call_counts,
            (step.reducer_call_count,),
        )

        balanced = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"cohort-{index}",
                    "path": f"results/cohort-{index}.json",
                    "content": (f'{{"cohort":{index},"ready":true}}\n' * 180),
                }
                for index in range(4)
            ],
            token_allowance=2_200,
            byte_allowance=12_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        balanced_estimate = fan_in_runtime.estimate_fan_in_reducer_schedule(
            balanced,
            max_wave_concurrency=2,
        )
        self.assertEqual(
            [
                (wave.wave, wave.reducer_call_count, wave.critical_call_cohorts)
                for wave in balanced_estimate.waves
            ],
            [(1, 4, 2), (2, 2, 1), (3, 1, 1)],
        )
        self.assertEqual(balanced_estimate.critical_call_cohorts, 4)

    async def test_stream_reducer_calls_have_independent_runtime_timeouts(self):
        body = "α证据 citation-X conflict gap\n" * 2_000
        plan = plan_persisted_result_fan_in(
            [{
                "result_id": "independent-timeout-source",
                "path": "results/independent-timeout.md",
                "content": body,
            }],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        estimate = fan_in_runtime.estimate_fan_in_reducer_schedule(plan)
        expected_call_ids = tuple(
            call_id
            for step in estimate.steps
            for call_id in step.reducer_call_ids
        )
        self.assertGreater(len(expected_call_ids), 3)
        call_timeout = 0.05
        per_call_delay = 0.015
        self.assertGreater(len(expected_call_ids) * per_call_delay, call_timeout)
        requests = []

        async def reducer(request):
            requests.append(request)
            await asyncio.sleep(per_call_delay)
            return _audited_reduction(request.prompt)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            started = asyncio.get_running_loop().time()
            materialized = await materialize_fan_in_plan(
                plan,
                results_root=root,
                reducer=reducer,
                timeout_seconds=5,
                step_timeout_seconds=call_timeout,
            )
            elapsed = asyncio.get_running_loop().time() - started

        self.assertGreater(elapsed, call_timeout)
        self.assertTrue(materialized.final_content)
        self.assertEqual(
            tuple(request.step_id for request in requests),
            expected_call_ids,
        )
        self.assertTrue(all(
            request.timeout_seconds == call_timeout
            for request in requests
        ))

    async def test_missing_terminal_coverage_ledger_fails_closed(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"input-{index}",
                    "path": f"results/input-{index}.md",
                    "content": "generic evidence " * 2_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )

        async def reducer(_request):
            return "Unverified semantic reduction body."

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "terminal fan-in coverage"):
                await materialize_fan_in_plan(
                    plan,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=5,
                )
            failure = root / ".chatds" / "fan_in" / plan.plan_id / "failure.json"
            self.assertTrue(failure.is_file())
            self.assertEqual(
                json.loads(failure.read_text(encoding="utf-8"))["status"],
                "failed",
            )

    def test_exact_ids_do_not_accept_substring_a_as_aa(self):
        inputs = [_runtime_input("a"), _runtime_input("aa")]
        only_aa = json.loads(
            _footer_for_inputs(inputs).split("FAN_IN_COVERAGE_JSON:", 1)[1]
        )["sources"][1:]
        with self.assertRaisesRegex(FanInExecutionError, "omitted source IDs: a"):
            fan_in_runtime._validate_semantic_reduction(
                _footer_for_inputs(inputs, records=only_aa),
                inputs,
                64 * 1024,
                "substring-step",
            )

    def test_duplicate_source_id_and_duplicate_json_key_fail_closed(self):
        inputs = [_runtime_input("a"), _runtime_input("b")]
        valid_record = json.loads(
            _footer_for_inputs(inputs).split("FAN_IN_COVERAGE_JSON:", 1)[1]
        )["sources"][0]
        with self.assertRaisesRegex(FanInExecutionError, "duplicate source ID: a"):
            fan_in_runtime._validate_semantic_reduction(
                _footer_for_inputs(inputs, records=[valid_record, valid_record]),
                inputs,
                64 * 1024,
                "duplicate-id-step",
            )
        duplicate_key = (
            'Faithful body.\nFAN_IN_COVERAGE_JSON:{"version":1,"version":1,'
            '"sources":[]}'
        )
        with self.assertRaisesRegex(FanInExecutionError, "duplicate JSON key: version"):
            fan_in_runtime._validate_semantic_reduction(
                duplicate_key,
                inputs,
                64 * 1024,
                "duplicate-key-step",
            )

    def test_unknown_and_missing_source_ids_fail_closed(self):
        inputs = [_runtime_input("known")]
        valid_record = json.loads(
            _footer_for_inputs(inputs).split("FAN_IN_COVERAGE_JSON:", 1)[1]
        )["sources"][0]
        unknown = dict(valid_record)
        unknown["input_id"] = "unknown"
        with self.assertRaisesRegex(FanInExecutionError, "unknown source ID: unknown"):
            fan_in_runtime._validate_semantic_reduction(
                _footer_for_inputs(inputs, records=[unknown]),
                inputs,
                64 * 1024,
                "unknown-step",
            )
        with self.assertRaisesRegex(FanInExecutionError, "omitted source IDs: known"):
            fan_in_runtime._validate_semantic_reduction(
                _footer_for_inputs(inputs, records=[]),
                inputs,
                64 * 1024,
                "missing-step",
            )

    def test_valid_exact_ledger_cross_checks_receipts_and_degraded_reason(self):
        inputs = [_runtime_input("alpha", "证据"), _runtime_input("beta", "evidence")]
        records = json.loads(
            _footer_for_inputs(inputs).split("FAN_IN_COVERAGE_JSON:", 1)[1]
        )["sources"]
        records[1]["status"] = "degraded"
        records[1]["reason"] = "The input explicitly lacks a primary citation."
        fan_in_runtime._validate_semantic_reduction(
            _footer_for_inputs(inputs, records=records),
            inputs,
            64 * 1024,
            "valid-step",
        )
        records[0]["provenance"]["checksum_sha256"] = "0" * 64
        with self.assertRaisesRegex(FanInExecutionError, "checksum mismatch"):
            fan_in_runtime._validate_semantic_reduction(
                _footer_for_inputs(inputs, records=records),
                inputs,
                64 * 1024,
                "forged-receipt-step",
            )

    def test_coverage_ledger_is_type_depth_and_size_bounded(self):
        inputs = [_runtime_input("bounded")]
        wrong_type = (
            'Faithful body.\nFAN_IN_COVERAGE_JSON:{"version":1,"sources":{}}'
        )
        with self.assertRaisesRegex(FanInExecutionError, "sources must be an array"):
            fan_in_runtime._validate_semantic_reduction(
                wrong_type, inputs, 128 * 1024, "type-step"
            )
        deep = '{"version":1,"sources":[],"nested":' + ("[" * 10) + "0" + ("]" * 10) + "}"
        with self.assertRaisesRegex(FanInExecutionError, "maximum JSON depth"):
            fan_in_runtime._validate_semantic_reduction(
                "Faithful body.\nFAN_IN_COVERAGE_JSON:" + deep,
                inputs,
                128 * 1024,
                "depth-step",
            )
        decoder_deep = (
            '{"version":1,"sources":[],"nested":'
            + ("[" * 2_000)
            + "0"
            + ("]" * 2_000)
            + "}"
        )
        with self.assertRaisesRegex(
            FanInExecutionError,
            "malformed JSON|maximum JSON depth",
        ):
            fan_in_runtime._validate_semantic_reduction(
                "Faithful body.\nFAN_IN_COVERAGE_JSON:" + decoder_deep,
                inputs,
                128 * 1024,
                "decoder-depth-step",
            )
        oversized = (
            'Faithful body.\nFAN_IN_COVERAGE_JSON:{"version":1,"sources":[],'
            '"padding":"' + ("x" * (64 * 1024)) + '"}'
        )
        with self.assertRaisesRegex(FanInExecutionError, "exceeds 65536 bytes"):
            fan_in_runtime._validate_semantic_reduction(
                oversized,
                inputs,
                128 * 1024,
                "size-step",
            )

    async def test_minimum_coverage_budget_is_checked_before_reducer_dispatch(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"sensor-{index}",
                    "path": f"results/sensor-{index}.json",
                    "content": "telemetry " * 2_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        inputs = [
            fan_in_runtime._InputBody(
                input_id=f"sensor-input-{index:04d}",
                path=f"results/telemetry/{index:04d}.json",
                checksum_sha256=hashlib.sha256(b"x").hexdigest(),
                source_start=index,
                source_end=index + 1,
                content="x",
                source_ids=(f"sensor-{index:04d}",),
            )
            for index in range(30)
        ]
        reducer = AsyncMock()
        output = replace(plan.reduction_steps[0].output, max_bytes=5_000)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "minimum coverage ledger"):
                await fan_in_runtime._reduce_and_write(
                    plan,
                    step_id="bounded-coverage-step",
                    output=output,
                    inputs=inputs,
                    root=root,
                    reducer=reducer,
                    execution_manifest_path="results/execution.json",
                    reducer_call_timeout_seconds=1,
                )
        reducer.assert_not_awaited()

    async def test_path_only_source_contract_fails_before_manifest_and_reducer(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"catalog-{index}",
                    "path": f"results/catalog-{index}.md",
                    "content": "catalog entry " * 2_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        missing_body = replace(plan.source_results[0], content=None)
        path_only = replace(
            plan,
            source_results=(missing_body,) + plan.source_results[1:],
        )
        reducer = AsyncMock()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "content=None; exact-load"):
                await materialize_fan_in_plan(
                    path_only,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=5,
                )
            manifest = root / path_only.source_manifest.path[len("results/") :]
            self.assertFalse(manifest.exists())
        reducer.assert_not_awaited()

    async def test_step_timeout_is_independent_and_cancels_the_reducer(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"orbit-{index}",
                    "path": f"results/orbit-{index}.md",
                    "content": "orbital sample " * 1_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        stopped = asyncio.Event()

        async def reducer(_request):
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "reduction step .* exceeded"):
                await materialize_fan_in_plan(
                    plan,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=2,
                    step_timeout_seconds=0.05,
                )
            self.assertTrue(stopped.is_set())
            failure = json.loads(
                (root / ".chatds" / "fan_in" / plan.plan_id / "failure.json")
                .read_text(encoding="utf-8")
            )
            self.assertIn("reduction step", failure["error"])

    async def test_wave_failure_cancels_and_awaits_all_siblings(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"package-{index}",
                    "path": f"results/package-{index}.json",
                    "content": (f'{{"package":{index},"ok":true}}\n' * 180),
                }
                for index in range(4)
            ],
            token_allowance=2_200,
            byte_allowance=12_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        leaf_ids = {
            step.step_id for step in plan.reduction_steps if step.wave == 1
        }
        failing_id = min(leaf_ids)
        all_started = asyncio.Event()
        started: set[str] = set()
        stopped: set[str] = set()

        async def reducer(request):
            if request.step_id not in leaf_ids:
                return _audited_reduction(request.prompt)
            started.add(request.step_id)
            if started == leaf_ids:
                all_started.set()
            try:
                await all_started.wait()
                if request.step_id == failing_id:
                    raise RuntimeError("synthetic wave failure")
                await asyncio.sleep(60)
                return _audited_reduction(request.prompt)
            finally:
                stopped.add(request.step_id)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "synthetic wave failure"):
                await materialize_fan_in_plan(
                    plan,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=5,
                    step_timeout_seconds=2,
                    max_wave_concurrency=4,
                )
            self.assertEqual(started, leaf_ids)
            self.assertEqual(stopped, leaf_ids)
            self.assertFalse(any(
                task.get_name().startswith(f"chatds-fan-in:{plan.plan_id}:")
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            ))

    async def test_timeout_cancels_reducer_and_records_failure(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"timeout-{index}",
                    "path": f"results/timeout-{index}.md",
                    "content": "evidence " * 2_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        cancelled = asyncio.Event()

        async def reducer(_request):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(FanInExecutionError, "exceeded"):
                await materialize_fan_in_plan(
                    plan,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=0.02,
                )
            self.assertTrue(cancelled.is_set())
            failure = json.loads(
                (root / ".chatds" / "fan_in" / plan.plan_id / "failure.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(failure["error"], "timeout")

    async def test_plan_absolute_deadline_does_not_reset_for_queued_steps(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"queued-{index}",
                    "path": f"results/queued-{index}.json",
                    "content": (f'{{"queued":{index},"ready":true}}\n' * 180),
                }
                for index in range(4)
            ],
            token_allowance=2_200,
            byte_allowance=12_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        calls = 0
        queued_step_started = asyncio.Event()
        queued_step_cancelled = asyncio.Event()

        async def reducer(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.02)
                return _audited_reduction(request.prompt)
            queued_step_started.set()
            try:
                await asyncio.sleep(60)
            finally:
                queued_step_cancelled.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            with self.assertRaisesRegex(
                FanInExecutionError,
                "fan-in plan .* exceeded",
            ):
                await materialize_fan_in_plan(
                    plan,
                    results_root=root,
                    reducer=reducer,
                    timeout_seconds=0.15,
                    # A fresh per-call deadline is intentionally longer than
                    # the plan. Only the immutable plan deadline may terminate
                    # the queued second call here.
                    step_timeout_seconds=1.0,
                    max_wave_concurrency=1,
                )
        self.assertTrue(queued_step_started.is_set())
        self.assertTrue(queued_step_cancelled.is_set())
        self.assertEqual(2, calls)

    async def test_external_cancellation_propagates_and_stops_reducer(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"cancel-{index}",
                    "path": f"results/cancel-{index}.md",
                    "content": "evidence " * 2_000,
                }
                for index in range(2)
            ],
            token_allowance=5_000,
            byte_allowance=32_000,
            reduction_output_tokens=1_000,
            reduction_output_bytes=8_000,
        )
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def reducer(_request):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "results"
            root.mkdir()
            task = asyncio.create_task(materialize_fan_in_plan(
                plan,
                results_root=root,
                reducer=reducer,
                timeout_seconds=10,
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())
            failure = json.loads(
                (root / ".chatds" / "fan_in" / plan.plan_id / "failure.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(failure["error"], "cancelled")


if __name__ == "__main__":
    unittest.main()
