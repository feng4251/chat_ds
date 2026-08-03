import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from result_fan_in import (
    DEFAULT_PRELOAD_BYTE_ALLOWANCE,
    FAN_IN_CONTRACT_VERSION,
    FAN_IN_OUTPUT_POLICY_VERSION,
    FAN_IN_PLANNER_VERSION,
    ReductionArtifact,
    derive_fan_in_budget,
    plan_persisted_result_fan_in,
)


class ResultFanInPlannerTests(unittest.TestCase):
    def test_fifteen_source_long_context_shape_uses_byte_safe_output_policy(self):
        def build(prefix: str):
            return plan_persisted_result_fan_in(
                [
                    {
                        "result_id": f"{prefix}-node-{index:02d}",
                        "path": f"results/{prefix}/part-{index:02d}.md",
                        "content": (
                            f"{prefix}-record-{index} field=value evidence.\n"
                            * 1_000
                        ),
                    }
                    for index in range(15)
                ],
                provider_config={
                    "provider": "long-context-test",
                    "context_length": 303_872,
                },
                # The bounded semantic reducer output must itself fit the
                # final consumer. Reduction is forced by the independent byte
                # allowance, not by constructing an impossible 12K-in/32K-out
                # final contract.
                token_allowance=48_000,
                byte_allowance=64_000,
                reduction_provider_config={
                    "provider": "long-context-test",
                    "context_length": 303_872,
                },
                reduction_token_allowance=190_000,
                reduction_byte_allowance=1024 * 1024,
                reduction_output_reserve_tokens=32 * 1024,
                reduction_output_tokens=32 * 1024,
                reduction_output_bytes=32 * 1024,
            )

        current = build("ledger")
        renamed = build("inventory")

        for plan in (current, renamed):
            self.assertTrue(plan.requires_reduction)
            self.assertEqual(15, len(plan.source_results))
            self.assertEqual(1, len(plan.source_batches))
            self.assertEqual(1, len(plan.reduction_steps))
            self.assertGreater(
                plan.reduction_steps[0].input_batch.estimated_tokens,
                90_000,
            )
            self.assertEqual(32 * 1024, plan.output_policy.max_bytes)
            self.assertEqual(
                plan.output_policy.max_bytes,
                plan.output_policy.max_tokens,
            )
            self.assertEqual(
                32 * 1024,
                plan.reduction_budget.output_reserve_tokens,
            )
        self.assertNotEqual(current.plan_id, renamed.plan_id)
        self.assertEqual(
            [step.strategy for step in current.reduction_steps],
            [step.strategy for step in renamed.reduction_steps],
            "renaming IDs/paths/domain labels must not change lifecycle topology",
        )

    def test_direct_plan_retains_order_exact_content_and_provenance(self):
        first = "完整中文证据。" * 20
        second = "complete ascii evidence\n" * 20

        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": "research",
                    "path": "results/research.md",
                    "content": first,
                    "source_worker": "worker-research",
                    "provenance": {"query": "alpha", "rank": 1},
                },
                {
                    "result_id": "review",
                    "path": "results/review.md",
                    "content": second,
                    "source_worker": "worker-review",
                    "consumes": ["worker-research"],
                    "provenance": {"reviewed": True},
                },
            ],
            provider_config={"provider": "test", "context_length": 65_536},
            token_allowance=12_000,
            byte_allowance=64_000,
            target_worker="worker-final",
            target_consumes=["worker-review"],
        )

        self.assertEqual(plan.mode, "direct")
        self.assertFalse(plan.requires_reduction)
        self.assertEqual(plan.reduction_budget, plan.budget)
        self.assertEqual(
            [item.result_id for item in plan.source_batches[0].items],
            ["research", "review"],
        )
        self.assertEqual(plan.source_results[0].content, first)
        self.assertEqual(plan.source_results[1].content, second)
        self.assertEqual(
            plan.source_results[0].checksum_sha256,
            hashlib.sha256(first.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(plan.source_results[0].provenance["query"], "alpha")
        self.assertEqual(
            plan.dependency_edges,
            (("research", "review"), ("review", "worker-final")),
        )
        self.assertEqual(
            hashlib.sha256(plan.source_manifest.render().encode("utf-8")).hexdigest(),
            plan.source_manifest.checksum_sha256,
        )

    def test_large_fan_in_builds_ordered_bounded_rolling_reduction(self):
        contents = [(f"source-{index}:" + "evidence " * 260) for index in range(6)]
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": f"r{index}",
                    "path": f"results/r{index}.md",
                    "content": content,
                    "source_worker": f"worker-{index}",
                    "provenance": {"ordinal": index},
                }
                for index, content in enumerate(contents)
            ],
            token_allowance=2_000,
            byte_allowance=7_000,
            reduction_output_tokens=200,
            reduction_output_bytes=800,
        )

        self.assertEqual(plan.mode, "rolling_reduction")
        self.assertTrue(plan.requires_reduction)
        self.assertGreater(len(plan.source_batches), 1)
        self.assertIsNotNone(plan.final_artifact)
        self.assertEqual(plan.final_artifact.source_start, 0)
        self.assertEqual(plan.final_artifact.source_end, len(contents))

        leaf_steps = [step for step in plan.reduction_steps if step.wave == 1]
        flattened = [
            item.result_id
            for step in leaf_steps
            for item in step.input_batch.items
        ]
        self.assertEqual(flattened, [f"r{index}" for index in range(6)])
        self.assertTrue(all(step.input_batch.fits_budget for step in leaf_steps))
        self.assertTrue(
            all(
                step.input_batch.fits_budget
                for step in plan.reduction_steps
                if step.strategy == "rolling_reduce"
            )
        )
        self.assertEqual(
            [item.content for item in plan.source_results],
            contents,
            "planning must not shorten any supplied source body",
        )
        self.assertEqual(
            [record["path"] for record in plan.source_manifest.records],
            [f"results/r{index}.md" for index in range(6)],
        )
        self.assertTrue(
            all(
                step.output.provenance_manifest_checksum_sha256
                == plan.source_manifest.checksum_sha256
                for step in plan.reduction_steps
            )
        )
        serialized = plan.to_dict()
        self.assertEqual(serialized["mode"], "rolling_reduction")
        self.assertNotIn("content", json.dumps(serialized["source_results"]))

    def test_single_oversize_source_requests_streaming_reduction_not_failure(self):
        body = "不能截断。" * 3_000
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": "oversize",
                    "path": "results/oversize.md",
                    "content": body,
                    "source_worker": "large-worker",
                }
            ],
            token_allowance=2_000,
            byte_allowance=4_000,
            reduction_output_tokens=150,
            reduction_output_bytes=600,
        )

        self.assertEqual(plan.mode, "rolling_reduction")
        self.assertEqual(plan.oversize_result_ids, ("oversize",))
        self.assertEqual(len(plan.reduction_steps), 1)
        self.assertEqual(plan.reduction_steps[0].strategy, "stream_exact_source")
        self.assertFalse(plan.reduction_steps[0].input_batch.fits_budget)
        self.assertEqual(plan.source_results[0].content, body)
        self.assertTrue(any("checksum" in item for item in plan.reduction_steps[0].requirements))
        self.assertTrue(any("instead of truncating" in warning for warning in plan.warnings))

    def test_paths_are_fully_scanned_and_checksum_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = root / "results" / "worker.md"
            result.parent.mkdir()
            result.write_text("证据\n" * 1_000, encoding="utf-8")

            plan = plan_persisted_result_fan_in(
                ["results/worker.md"],
                workspace_root=root,
                token_allowance=5_000,
                byte_allowance=64_000,
            )
            self.assertEqual(
                plan.source_results[0].checksum_sha256,
                hashlib.sha256(result.read_bytes()).hexdigest(),
            )
            self.assertEqual(plan.source_results[0].byte_size, result.stat().st_size)

            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                plan_persisted_result_fan_in(
                    [
                        {
                            "path": "results/worker.md",
                            "checksum_sha256": "0" * 64,
                        }
                    ],
                    workspace_root=root,
                    token_allowance=5_000,
                    byte_allowance=64_000,
                )

    def test_budget_is_capped_by_provider_context_and_512kib_default(self):
        budget = derive_fan_in_budget(
            {"provider": "glm", "context_length": 100_000},
            token_allowance=90_000,
            base_prompt_tokens=10_000,
        )
        expected_context_allowance = (
            100_000 - 10_000 - 8_192 - 4_096 - 512 - 10_000
        )
        self.assertEqual(budget.input_token_allowance, expected_context_allowance)
        self.assertEqual(budget.token_allowance_source, "min(explicit,provider_context)")
        self.assertEqual(
            budget.input_byte_allowance,
            DEFAULT_PRELOAD_BYTE_ALLOWANCE,
        )
        self.assertEqual(budget.byte_allowance_source, "runtime_default_512kib")

        explicit = derive_fan_in_budget(token_allowance=2_500, byte_allowance=9_000)
        self.assertEqual(explicit.input_token_allowance, 2_500)
        self.assertEqual(explicit.input_byte_allowance, 9_000)
        with self.assertRaisesRegex(ValueError, "requires token_allowance"):
            derive_fan_in_budget({"provider": "unknown"})

    def test_unresolved_consumes_are_audited_without_filtering_inputs(self):
        plan = plan_persisted_result_fan_in(
            [
                {
                    "result_id": "a",
                    "path": "results/a.md",
                    "content": "a",
                    "source_worker": "worker-a",
                },
                {
                    "result_id": "b",
                    "path": "results/b.md",
                    "content": "b",
                    "source_worker": "worker-b",
                    "consumes": ["missing-worker"],
                },
            ],
            token_allowance=2_000,
            byte_allowance=20_000,
            target_consumes=["worker-a", "also-missing"],
        )
        self.assertEqual([item.result_id for item in plan.source_results], ["a", "b"])
        self.assertIn(("a", "__target__"), plan.dependency_edges)
        self.assertIn("b:missing-worker", plan.unresolved_consumes)
        self.assertIn("__target__:also-missing", plan.unresolved_consumes)
        self.assertTrue(any("did not silently remove" in item for item in plan.warnings))

    def test_final_child_budget_decides_direct_and_forces_one_reducer_pass(self):
        release_notes = [
            {
                "result_id": f"release-{index}",
                "path": f"releases/{index}.md",
                "content": f"Release {index}\n" + "compatibility note\n" * 90,
            }
            for index in range(3)
        ]

        # A deliberately smaller reducer budget is irrelevant when the exact
        # sources already fit the final child request.
        direct = plan_persisted_result_fan_in(
            release_notes,
            token_allowance=10_000,
            byte_allowance=30_000,
            reduction_token_allowance=500,
            reduction_byte_allowance=1_000,
        )
        self.assertEqual(direct.mode, "direct")
        self.assertEqual(direct.budget, direct.final_budget)
        self.assertNotEqual(direct.reduction_budget, direct.final_budget)

        # Conversely, a larger internal budget cannot turn this into direct
        # delivery. It permits one complete reducer preload, but at least one
        # semantic reduction is still required for the smaller final child.
        reduced = plan_persisted_result_fan_in(
            release_notes,
            token_allowance=2_000,
            byte_allowance=4_000,
            reduction_token_allowance=10_000,
            reduction_byte_allowance=30_000,
            reduction_output_tokens=200,
            reduction_output_bytes=800,
        )
        self.assertEqual(reduced.mode, "rolling_reduction")
        self.assertEqual(len(reduced.source_batches), 1)
        self.assertTrue(reduced.source_batches[0].fits_budget)
        self.assertEqual(len(reduced.reduction_steps), 1)
        self.assertEqual(reduced.reduction_steps[0].strategy, "complete_preload")
        self.assertEqual(reduced.final_artifact.source_start, 0)
        self.assertEqual(reduced.final_artifact.source_end, len(release_notes))
        self.assertLess(
            reduced.final_artifact.max_bytes,
            reduced.final_budget.input_byte_allowance,
        )
        self.assertTrue(any("reduction is mandatory" in item for item in reduced.warnings))

    def test_multi_batch_merge_is_stable_ordered_balanced_waves(self):
        supply_chain_events = [
            {
                "result_id": f"event-{index}",
                "path": f"events/{index}.md",
                "content": f"event {index}:" + " shipment evidence" * 160,
            }
            for index in range(8)
        ]
        plan = plan_persisted_result_fan_in(
            supply_chain_events,
            token_allowance=2_500,
            byte_allowance=4_500,
            reduction_output_tokens=100,
            reduction_output_bytes=400,
        )

        self.assertEqual(len(plan.source_batches), 8)
        merge_steps = [
            step for step in plan.reduction_steps if step.strategy == "rolling_reduce"
        ]
        self.assertEqual(
            {wave: sum(step.wave == wave for step in merge_steps) for wave in (2, 3, 4)},
            {2: 4, 3: 2, 4: 1},
        )
        self.assertEqual(max(step.wave for step in plan.reduction_steps), 4)
        for step in merge_steps:
            left, right = step.input_batch.items
            self.assertIsInstance(left, ReductionArtifact)
            self.assertIsInstance(right, ReductionArtifact)
            self.assertEqual(left.source_end, right.source_start)
            self.assertEqual(step.output.source_start, left.source_start)
            self.assertEqual(step.output.source_end, right.source_end)
            self.assertEqual(
                step.output.immediate_input_ids,
                (left.artifact_id, right.artifact_id),
            )
            self.assertTrue(step.input_batch.fits_budget)
        self.assertEqual(
            plan.output_policy.merge_topology,
            "stable_ordered_balanced_binary_waves",
        )

    def test_plan_id_binds_versions_and_effective_output_policy(self):
        observations = [
            {
                "result_id": f"observation-{index}",
                "path": f"observations/{index}.md",
                "content": f"observation {index}:" + " photon count" * 180,
            }
            for index in range(4)
        ]

        def make_plan(output_tokens):
            return plan_persisted_result_fan_in(
                observations,
                token_allowance=1_800,
                byte_allowance=4_000,
                reduction_output_tokens=output_tokens,
                reduction_output_bytes=output_tokens * 4,
            )

        first = make_plan(100)
        repeat = make_plan(100)
        changed = make_plan(120)
        self.assertEqual(first.plan_id, repeat.plan_id)
        self.assertNotEqual(first.plan_id, changed.plan_id)
        self.assertEqual(first.planner_version, FAN_IN_PLANNER_VERSION)
        self.assertEqual(first.contract_version, FAN_IN_CONTRACT_VERSION)
        self.assertEqual(first.output_policy.version, FAN_IN_OUTPUT_POLICY_VERSION)
        self.assertEqual(first.output_policy.max_tokens, 100)
        self.assertEqual(first.output_policy.max_bytes, 400)
        serialized = first.to_dict()
        self.assertEqual(serialized["planner_version"], FAN_IN_PLANNER_VERSION)
        self.assertEqual(serialized["contract_version"], FAN_IN_CONTRACT_VERSION)
        self.assertEqual(serialized["output_policy"], first.output_policy.to_dict())
        self.assertEqual(serialized["budget"], serialized["final_budget"])

    def test_execution_namespace_isolates_concurrent_physical_artifacts(self):
        records = [
            {
                "result_id": f"ledger-{index}",
                "path": f"results/ledger-{index}.json",
                "content": (f'{{"id":{index},"amount":-12.50}}\n' * 120),
            }
            for index in range(2)
        ]

        first = plan_persisted_result_fan_in(
            records,
            token_allowance=500,
            byte_allowance=2_000,
            reduction_output_tokens=100,
            reduction_output_bytes=400,
            execution_namespace="child-run-a",
        )
        repeat = plan_persisted_result_fan_in(
            records,
            token_allowance=500,
            byte_allowance=2_000,
            reduction_output_tokens=100,
            reduction_output_bytes=400,
            execution_namespace="child-run-a",
        )
        concurrent = plan_persisted_result_fan_in(
            records,
            token_allowance=500,
            byte_allowance=2_000,
            reduction_output_tokens=100,
            reduction_output_bytes=400,
            execution_namespace="child-run-b",
        )

        self.assertEqual(first.plan_id, repeat.plan_id)
        self.assertNotEqual(first.plan_id, concurrent.plan_id)
        self.assertNotEqual(first.final_artifact.path, concurrent.final_artifact.path)
        self.assertEqual(len(first.execution_namespace_sha256), 64)
        self.assertNotIn("child-run-a", json.dumps(first.to_dict()))


if __name__ == "__main__":
    unittest.main()
