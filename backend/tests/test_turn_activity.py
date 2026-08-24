import unittest

from turn_activity import TurnActivityBuilder, safe_agent_event


class TurnActivityProtocolTests(unittest.TestCase):
    def test_interleaving_and_contiguous_stream_nodes_are_engine_independent(self):
        builder = TurnActivityBuilder("a" * 32, "b" * 32)
        first = builder.stream_text("reasoning", "one")
        second = builder.stream_text("reasoning", "two")
        tool = builder.agent({
            "event_type": "tool.started",
            "run_id": "a" * 32,
            "tool_name": "RenamedLookup",
            "tool_call_id": "call-1",
        })
        content = builder.stream_text("content", "answer")
        self.assertEqual(first["node_id"], second["node_id"])
        self.assertEqual([first["seq"], second["seq"], tool["seq"], content["seq"]], [1, 2, 3, 4])
        self.assertNotEqual(second["node_id"], content["node_id"])

    def test_agent_projection_drops_arbitrary_tool_arguments_and_secrets(self):
        safe = safe_agent_event({
            "event_type": "tool.started",
            "run_id": "c" * 32,
            "seq": 7,
            "tool_name": "GenericTool",
            "tool_call_id": "call",
            "input": {"api_key": "secret"},
            "payload": {
                "goal": "cross-domain goal",
                "arguments": {"token": "secret"},
                "code": "print('secret')",
            },
        })
        self.assertEqual(safe["payload"], {"goal": "cross-domain goal"})
        self.assertEqual(safe["seq"], 7)
        self.assertNotIn("input", safe)
        self.assertNotIn("arguments", safe["payload"])

    def test_child_output_is_visible_without_projecting_tool_input(self):
        safe = safe_agent_event({
            "event_type": "agent.delta",
            "run_id": "f" * 32,
            "payload": {
                "content": "cross-domain worker progress",
                "input": {"private": "never project"},
            },
        })
        self.assertEqual(
            safe["payload"], {"content": "cross-domain worker progress"}
        )

    def test_tool_and_approval_updates_reuse_stable_nodes(self):
        builder = TurnActivityBuilder("d" * 32, "e" * 32)
        started = builder.agent({
            "event_type": "tool.started", "run_id": "d" * 32,
            "tool_name": "MutatedTool", "tool_call_id": "mutated-call",
        })
        completed = builder.agent({
            "event_type": "tool.completed", "run_id": "d" * 32,
            "tool_name": "MutatedTool", "tool_call_id": "mutated-call",
        })
        asked = builder.approval(
            request_id="request-1", status="pending",
            details={"tool_name": "MutatedTool", "request_seq": 42},
        )
        decided = builder.approval(
            request_id="request-1", status="allowed",
            details={"tool_name": "MutatedTool"},
        )
        self.assertEqual(started["node_id"], completed["node_id"])
        self.assertEqual(asked["node_id"], decided["node_id"])
        self.assertEqual(asked["payload"]["request_seq"], 42)

    def test_payload_tool_identity_and_invisible_workflow_updates_are_stable(self):
        builder = TurnActivityBuilder("8" * 32, "9" * 32)
        first = builder.stream_text("reasoning", "inspect ")
        workflow = builder.agent({
            "event_type": "run.progress",
            "run_id": "8" * 32,
            "payload": {"workflow_stage": "renamed-museum-audit"},
        })
        second = builder.stream_text("reasoning", "receipts")
        started = builder.agent({
            "event_type": "tool.started",
            "run_id": "8" * 32,
            "payload": {
                "tool_name": "RenamedCatalogLookup",
                "tool_call_id": "catalog-call",
            },
        })
        completed = builder.agent({
            "event_type": "tool.completed",
            "run_id": "8" * 32,
            "payload": {
                "tool_name": "RenamedCatalogLookup",
                "tool_call_id": "catalog-call",
            },
        })
        after_tool = builder.stream_text("reasoning", "summarize")

        self.assertEqual(workflow["kind"], "workflow")
        self.assertEqual(first["node_id"], second["node_id"])
        self.assertEqual(started["node_id"], completed["node_id"])
        self.assertNotEqual(second["node_id"], after_tool["node_id"])
        self.assertEqual(
            started["payload"]["event"]["payload"]["tool_call_id"],
            "catalog-call",
        )
        self.assertEqual(
            started["payload"]["event"]["tool_call_id"],
            "catalog-call",
        )
        self.assertEqual(
            completed["payload"]["event"]["tool_name"],
            "RenamedCatalogLookup",
        )

    def test_native_question_projection_is_bounded_and_rename_invariant(self):
        builder = TurnActivityBuilder("6" * 32, "7" * 32)
        question = builder.approval(
            request_id="museum-question",
            status="pending",
            details={
                "interaction_kind": "question",
                "request_seq": 91,
                "questions": [{
                    "question": "Which gallery should be reviewed?",
                    "header": "Gallery",
                    "multi_select": False,
                    "options": [
                        {
                            "label": "East",
                            "description": "Review the east gallery",
                            "preview": "must not cross the I/O boundary",
                        },
                        {
                            "label": "West",
                            "description": "Review the west gallery",
                        },
                    ],
                }],
                "input": {"credential": "must-not-project"},
            },
        )
        self.assertEqual(question["payload"]["interaction_kind"], "question")
        self.assertEqual(question["payload"]["request_seq"], 91)
        self.assertNotIn(
            "preview", question["payload"]["questions"][0]["options"][0]
        )
        self.assertNotIn("must-not-project", str(question))

        mutated = builder.approval(
            request_id="factory-question",
            status="pending",
            details={
                "interaction_kind": "question",
                "questions": [{
                    "question": "Which line should be reviewed?",
                    "header": "Line",
                    "options": [
                        {"label": "A", "description": "First"},
                        {"label": "A", "description": "Duplicate"},
                    ],
                }],
            },
        )
        self.assertNotIn("interaction_kind", mutated["payload"])
        self.assertNotIn("questions", mutated["payload"])

    def test_projection_commit_is_last_and_non_content(self):
        builder = TurnActivityBuilder("1" * 32, "2" * 32)
        content = builder.stream_text("content", "done")
        committed = builder.commit()
        self.assertGreater(committed["seq"], content["seq"])
        self.assertEqual(committed["kind"], "projection")
        self.assertEqual(committed["payload"], {"status": "committed"})

    def test_coalesced_long_content_is_not_silently_shortened(self):
        builder = TurnActivityBuilder("3" * 32, "4" * 32)
        content = "x" * 100_000
        event = builder.stream_text("content", content)
        self.assertEqual(event["payload"]["text"], content)


if __name__ == "__main__":
    unittest.main()
