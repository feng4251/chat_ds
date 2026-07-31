from __future__ import annotations

import json
import unittest

from retrieval_completeness import (
    RETRIEVAL_QUALITY_IMPACT_ADVISORY,
    RETRIEVAL_QUALITY_IMPACT_DEGRADED,
    RetrievalCompletenessTracker,
    build_http_retrieval_receipt,
    retrieval_receipt_affects_completion_quality,
)
from delegated_result_contract import audit_result_fields
from tools.delegation import (
    _inject_unresolved_retrieval_gap,
    _normalized_unresolved_retrieval,
)


def _receipt(
    url: str,
    body: str,
    *,
    number: int,
    truncated: bool = False,
    max_chars: int = 40_000,
    method: str = "GET",
    request_body: dict | None = None,
    full_body: str | None = None,
    wire_complete: bool | None = None,
    hard_max_chars: int | None = None,
) -> dict:
    scan_body = full_body if full_body is not None else body
    return build_http_retrieval_receipt(
        method=method,
        request_url=url,
        request_body=request_body,
        response_body=body,
        pagination_scan_body=(
            scan_body if wire_complete is not False else None
        ),
        body_truncated=truncated,
        wire_body_complete=wire_complete,
        response_bytes_read=len(scan_body.encode("utf-8")),
        response_byte_limit=400_000,
        response_chars_read=len(scan_body),
        response_chars_returned=len(body),
        response_char_limit=max_chars,
        response_char_hard_limit=hard_max_chars,
        request_timeout=20,
        request_number=number,
        request_run_hop_limit=16,
        request_elapsed_ms=10,
    )


class RetrievalCompletenessReceiptTests(unittest.TestCase):
    def test_truncated_response_requires_exact_request_identity_retry(self):
        tracker = RetrievalCompletenessTracker()
        original = "https://api.vendor.test/search?q=x&pageSize=20"
        smaller = "https://api.vendor.test/search?q=x&pageSize=5"

        first = tracker.observe(_receipt(
            original,
            '{"items":[1',
            number=1,
            truncated=True,
            max_chars=10,
        ))
        self.assertEqual(1, first["open_chain_count"])

        # A smaller page without an explicit continuation does not prove that
        # the original 20-record request was covered.
        second = tracker.observe(_receipt(
            smaller,
            '{"items":[1,2,3,4,5]}',
            number=2,
            max_chars=20_000,
        ))
        self.assertEqual(1, second["open_chain_count"])

        closed = tracker.observe(_receipt(
            original,
            '{"items":[1,2,3]}',
            number=3,
            max_chars=100_000,
        ))
        self.assertEqual(0, closed["open_chain_count"])

    def test_post_truncation_identity_includes_full_request_body(self):
        tracker = RetrievalCompletenessTracker()
        url = "https://api.vendor.test/graphql"
        first_body = {
            "query": "query($first:Int!){records(first:$first){id}}",
            "variables": {"first": 20},
        }
        changed_body = {
            "query": "query($first:Int!){records(first:$first){id}}",
            "variables": {"first": 5},
        }
        tracker.observe(_receipt(
            url,
            '{"data":{"records":[',
            number=1,
            truncated=True,
            max_chars=20,
            method="POST",
            request_body=first_body,
        ))
        still_open = tracker.observe(_receipt(
            url,
            '{"data":{"records":[{"id":"1"}]}}',
            number=2,
            max_chars=100_000,
            method="POST",
            request_body=changed_body,
        ))
        self.assertEqual(1, still_open["open_chain_count"])

        closed = tracker.observe(_receipt(
            url,
            '{"data":{"records":[{"id":"1"}]}}',
            number=3,
            max_chars=100_000,
            method="POST",
            request_body=first_body,
        ))
        self.assertEqual(0, closed["open_chain_count"])

    def test_next_page_token_chain_closes_only_at_explicit_end(self):
        tracker = RetrievalCompletenessTracker()
        root = "https://api.vendor.test/search?q=x"
        page2 = root + "&pageToken=A"
        page3 = root + "&pageToken=B"

        first = tracker.observe(_receipt(
            root,
            '{"items":[1],"nextPageToken":"A"}',
            number=1,
        ))
        self.assertEqual(1, first["open_frontier_count"])
        second = tracker.observe(_receipt(
            page2,
            '{"items":[2],"nextPageToken":"B"}',
            number=2,
        ))
        self.assertEqual(1, second["open_frontier_count"])
        third = tracker.observe(_receipt(
            page3,
            '{"items":[3],"nextPageToken":null}',
            number=3,
        ))
        self.assertEqual(0, third["open_chain_count"])

    def test_clean_cursor_is_optional_bounded_but_required_exhaustive(self):
        tracker = RetrievalCompletenessTracker()
        tracker.observe(_receipt(
            "https://api.museum.test/catalog?q=bronze",
            '{"items":[1],"nextPageToken":"A"}',
            number=1,
        ))

        self.assertFalse(tracker.requires_mandatory_continuation("bounded"))
        self.assertTrue(tracker.has_optional_pagination_frontier("bounded"))
        self.assertEqual(
            RETRIEVAL_QUALITY_IMPACT_ADVISORY,
            tracker.closure_quality_impact("bounded"),
        )
        self.assertTrue(
            tracker.requires_mandatory_continuation("exhaustive")
        )
        exhaustive_action = tracker.next_continuation_action(
            "exhaustive",
            mandatory_only=True,
        )
        self.assertIsNotNone(exhaustive_action)
        self.assertEqual(
            "follow_pagination_cursor",
            exhaustive_action["kind"],
        )
        self.assertIsNone(tracker.next_continuation_action(
            "bounded",
            mandatory_only=True,
        ))
        self.assertEqual(
            RETRIEVAL_QUALITY_IMPACT_DEGRADED,
            tracker.closure_quality_impact("exhaustive"),
        )
        frontier = tracker.frontier_receipt()
        self.assertFalse(frontier["raw_cursor_or_url_persisted"])
        self.assertEqual(1, frontier["next_cursor_count"])
        self.assertEqual(64, len(frontier["next_cursor_sha256s"][0]))
        self.assertNotIn('"A"', json.dumps(frontier))
        self.assertEqual(1, frontier["families"][0]["pages_observed"])
        self.assertEqual(1, frontier["families"][0]["items_observed"])

    def test_body_truncation_remains_mandatory_under_bounded_policy(self):
        tracker = RetrievalCompletenessTracker()
        tracker.observe(_receipt(
            "https://api.museum.test/catalog?q=bronze",
            '{"items":[1',
            number=1,
            truncated=True,
            max_chars=10,
        ))

        self.assertTrue(tracker.requires_mandatory_continuation("bounded"))
        self.assertFalse(tracker.has_optional_pagination_frontier("bounded"))
        self.assertEqual(
            RETRIEVAL_QUALITY_IMPACT_DEGRADED,
            tracker.closure_quality_impact("bounded"),
        )

    def test_bounded_mandatory_repair_preempts_older_optional_cursor(self):
        tracker = RetrievalCompletenessTracker()
        optional_url = "https://api.museum.test/catalog?q=bronze"
        mandatory_url = "https://api.museum.test/catalog?q=ceramic"
        tracker.observe(_receipt(
            optional_url,
            '{"items":[1],"nextPageToken":"A"}',
            number=1,
        ))
        tracker.observe(_receipt(
            mandatory_url,
            '{"items":[1',
            number=2,
            truncated=True,
            max_chars=10,
            full_body='{"items":[1,2,3,4,5]}',
            wire_complete=True,
            hard_max_chars=100,
        ))

        bounded_action = tracker.next_continuation_action("bounded")
        self.assertIsNotNone(bounded_action)
        self.assertEqual(
            "retry_with_larger_visible_limit",
            bounded_action["kind"],
        )
        self.assertEqual(mandatory_url, bounded_action["args"]["url"])
        self.assertEqual(
            bounded_action,
            tracker.next_continuation_action(),
        )
        self.assertEqual(
            bounded_action,
            tracker.next_continuation_action(
                "bounded",
                mandatory_only=True,
            ),
        )

        # Exhaustive policy promotes the older clean cursor to mandatory, so
        # the deterministic ready order still begins with that family.
        exhaustive_action = tracker.next_continuation_action("exhaustive")
        self.assertIsNotNone(exhaustive_action)
        self.assertEqual(
            "follow_pagination_cursor",
            exhaustive_action["kind"],
        )
        self.assertIn("pageToken=A", exhaustive_action["args"]["url"])

    def test_mandatory_frontiers_rotate_after_linked_progress(self):
        tracker = RetrievalCompletenessTracker()
        first_url = "https://api.archive.test/search?q=alpha"
        second_url = "https://api.archive.test/search?q=beta"
        complete_wire_body = (
            '{"items":[' + ",".join(str(i) for i in range(40)) + "]}"
        )
        for number, url in enumerate((first_url, second_url), start=1):
            tracker.observe(_receipt(
                url,
                complete_wire_body[:12],
                number=number,
                truncated=True,
                max_chars=10,
                full_body=complete_wire_body,
                wire_complete=True,
                hard_max_chars=200,
            ))

        first_action = tracker.next_continuation_action("bounded")
        self.assertEqual(first_url, first_action["args"]["url"])
        # Inspection is pure; retries in the caller cannot silently rotate
        # the scheduler until a linked receipt actually advances this family.
        self.assertEqual(
            first_action,
            tracker.next_continuation_action("bounded"),
        )

        tracker.observe(_receipt(
            first_url,
            complete_wire_body[:30],
            number=3,
            truncated=True,
            max_chars=first_action["args"]["max_chars"],
            full_body=complete_wire_body,
            wire_complete=True,
            hard_max_chars=200,
        ))
        second_action = tracker.next_continuation_action("bounded")
        self.assertEqual(second_url, second_action["args"]["url"])

        tracker.observe(_receipt(
            second_url,
            complete_wire_body[:30],
            number=4,
            truncated=True,
            max_chars=second_action["args"]["max_chars"],
            full_body=complete_wire_body,
            wire_complete=True,
            hard_max_chars=200,
        ))
        rotated_action = tracker.next_continuation_action("bounded")
        self.assertEqual(first_url, rotated_action["args"]["url"])

    def test_unactionable_mandatory_chain_blocks_optional_fallback(self):
        tracker = RetrievalCompletenessTracker()
        tracker.observe(_receipt(
            "https://api.library.test/books?q=history",
            '{"items":[1],"nextPageToken":"A"}',
            number=1,
        ))
        # A legacy receipt without a proven schema ceiling has no safe
        # machine-generated repair action. It remains mandatory and must not
        # be bypassed by an unrelated advisory cursor.
        tracker.observe(_receipt(
            "https://api.library.test/books?q=science",
            '{"items":[1',
            number=2,
            truncated=True,
            max_chars=10,
        ))

        self.assertTrue(tracker.requires_mandatory_continuation("bounded"))
        self.assertIsNone(tracker.next_continuation_action("bounded"))
        self.assertIsNone(tracker.next_continuation_action(
            "bounded",
            mandatory_only=True,
        ))

    def test_only_explicit_advisory_receipt_is_quality_neutral(self):
        self.assertFalse(retrieval_receipt_affects_completion_quality(None))
        self.assertFalse(retrieval_receipt_affects_completion_quality({
            "quality_impact": "advisory",
        }))
        self.assertTrue(retrieval_receipt_affects_completion_quality({
            "quality_impact": "degraded",
        }))
        self.assertTrue(retrieval_receipt_affects_completion_quality({}))
        self.assertTrue(retrieval_receipt_affects_completion_quality({
            "quality_impact": "future-value",
        }))

    def test_multiple_cursor_frontier_does_not_close_after_one_branch(self):
        tracker = RetrievalCompletenessTracker()
        root = "https://api.vendor.test/graphql"
        query = (
            "query Both($afterA:String,$afterB:String){"
            "a(after:$afterA){id} b(after:$afterB){id}}"
        )
        tracker.observe(_receipt(
            root,
            (
                '{"data":{"a":{"nextPageToken":"A"},'
                '"b":{"nextPageToken":"B"}}}'
            ),
            number=1,
            method="POST",
            request_body={
                "query": query,
                "variables": {"afterA": None, "afterB": None},
            },
        ))

        after_a = tracker.observe(_receipt(
            root,
            '{"data":{"a":{"items":[1]}}}',
            number=2,
            method="POST",
            request_body={
                "query": query,
                "variables": {"afterA": "A", "afterB": None},
            },
        ))
        self.assertEqual(1, after_a["open_chain_count"])
        self.assertEqual(1, after_a["open_frontier_count"])

        after_b = tracker.observe(_receipt(
            root,
            '{"data":{"b":{"items":[2]}}}',
            number=3,
            method="POST",
            request_body={
                "query": query,
                "variables": {"afterA": None, "afterB": "B"},
            },
        ))
        self.assertEqual(0, after_b["open_chain_count"])

    def test_frontier_overflow_is_machine_marked_instead_of_silently_closed(self):
        branches = ",".join(
            f'"branch{index}":{{"nextPageToken":"T{index}"}}'
            for index in range(8)
        )
        receipt = _receipt(
            "https://api.vendor.test/graphql",
            '{"data":{' + branches + "}}",
            number=1,
        )
        self.assertEqual("incomplete", receipt["state"])
        self.assertIn(
            "pagination_frontier_truncated",
            receipt["incomplete_reasons"],
        )
        self.assertTrue(receipt["pagination"]["frontier_truncated"])
        self.assertGreater(receipt["pagination"]["hint_overflow_count"], 0)

        snapshot = RetrievalCompletenessTracker().observe(receipt)
        self.assertEqual("pagination_frontier_limit", snapshot["terminal_failure"])
        self.assertGreater(snapshot["open_frontier_count"], 0)

    def test_bounded_pagination_scan_fails_closed(self):
        nested: dict = {"records": {}}
        cursor = nested["records"]
        for index in range(12):
            cursor["nested"] = {"level": index}
            cursor = cursor["nested"]
        receipt = _receipt(
            "https://api.vendor.test/deep",
            json.dumps(nested),
            number=1,
        )
        self.assertIn(
            "pagination_scan_bounded",
            receipt["incomplete_reasons"],
        )
        snapshot = RetrievalCompletenessTracker().observe(receipt)
        self.assertEqual("pagination_scan_limit", snapshot["terminal_failure"])

    def test_unlinked_complete_pages_converge_to_terminal_gap(self):
        tracker = RetrievalCompletenessTracker(max_unlinked_attempts=2)
        root = "https://api.vendor.test/search?q=x"
        tracker.observe(_receipt(
            root,
            '{"items":[1],"nextPageToken":"A"}',
            number=1,
        ))
        first_unlinked = tracker.observe(_receipt(
            root + "&page=9",
            '{"items":[9]}',
            number=2,
        ))
        self.assertIsNone(first_unlinked["terminal_failure"])
        second_unlinked = tracker.observe(_receipt(
            root + "&page=10",
            '{"items":[10]}',
            number=3,
        ))
        self.assertEqual(
            "pagination_cursor_not_consumed",
            second_unlinked["terminal_failure"],
        )
        self.assertEqual(1, second_unlinked["open_chain_count"])

    def test_repeated_cursor_is_terminal(self):
        tracker = RetrievalCompletenessTracker()
        root = "https://api.vendor.test/search?q=x"
        tracker.observe(_receipt(
            root,
            '{"items":[1],"nextPageToken":"A"}',
            number=1,
        ))
        repeated = tracker.observe(_receipt(
            root + "&pageToken=A",
            '{"items":[2],"nextPageToken":"A"}',
            number=2,
        ))
        self.assertEqual(
            "pagination_cursor_repeated",
            repeated["terminal_failure"],
        )

    def test_independent_hard_limits_are_machine_visible(self):
        url = "https://api.vendor.test/search?q=x"
        open_receipt = _receipt(
            url,
            '{"items":[1],"nextPageToken":"A"}',
            number=1,
        )
        cases = (
            (
                RetrievalCompletenessTracker(max_total_requests=1),
                "retrieval_request_limit",
            ),
            (
                RetrievalCompletenessTracker(max_total_response_bytes=1),
                "retrieval_cumulative_byte_limit",
            ),
            (
                RetrievalCompletenessTracker(max_total_request_elapsed_ms=1),
                "retrieval_total_time_limit",
            ),
            (
                RetrievalCompletenessTracker(max_pages_per_chain=1),
                "pagination_page_limit",
            ),
        )
        for tracker, expected in cases:
            with self.subTest(expected=expected):
                snapshot = tracker.observe(open_receipt)
                self.assertEqual(expected, snapshot["terminal_failure"])
                self.assertEqual(1, snapshot["open_chain_count"])

    def test_domain_filter_names_do_not_collapse_into_pagination_family(self):
        first = _receipt(
            "https://api.vendor.test/search?q=x&startDate=2020-01-01",
            '{"items":[]}',
            number=1,
        )
        second = _receipt(
            "https://api.vendor.test/search?q=x&startDate=2021-01-01",
            '{"items":[]}',
            number=2,
        )
        self.assertNotEqual(first["family_sha256"], second["family_sha256"])

        graphql_url = "https://api.vendor.test/graphql"
        body_a = {
            "query": "query($afterA:String){records(after:$afterA){id}}",
            "variables": {"afterA": "A", "startDate": "2020-01-01"},
        }
        body_b = {
            **body_a,
            "variables": {"afterA": "B", "startDate": "2020-01-01"},
        }
        body_filter_changed = {
            **body_a,
            "variables": {"afterA": "A", "startDate": "2021-01-01"},
        }
        receipt_a = _receipt(
            graphql_url, '{"data":{}}', number=3,
            method="POST", request_body=body_a,
        )
        receipt_b = _receipt(
            graphql_url, '{"data":{}}', number=4,
            method="POST", request_body=body_b,
        )
        receipt_filter_changed = _receipt(
            graphql_url, '{"data":{}}', number=5,
            method="POST", request_body=body_filter_changed,
        )
        self.assertEqual(
            receipt_a["family_sha256"], receipt_b["family_sha256"]
        )
        self.assertNotEqual(
            receipt_a["family_sha256"],
            receipt_filter_changed["family_sha256"],
        )

    def test_generic_numeric_next_matches_page_parameter(self):
        tracker = RetrievalCompletenessTracker()
        root = "https://api.vendor.test/search?q=x"
        first_receipt = _receipt(
            root,
            '{"items":[1],"next":2}',
            number=1,
        )
        hint = first_receipt["pagination"]["next_hints"][0]
        self.assertEqual("cursor", hint["kind"])
        tracker.observe(first_receipt)
        closed = tracker.observe(_receipt(
            root + "&page=2",
            '{"items":[2],"next":null}',
            number=2,
        ))
        self.assertEqual(0, closed["open_chain_count"])

    def test_opaque_next_page_token_does_not_overwrite_numeric_page(self):
        receipt = _receipt(
            "https://api.vendor.test/search?q=x&page=1&pageSize=10",
            '{"items":[1],"nextPageToken":"opaque-A"}',
            number=1,
        )

        action = receipt["continuation_action"]
        self.assertEqual("follow_pagination_cursor", action["kind"])
        self.assertIn("page=1", action["args"]["url"])
        self.assertIn("pageToken=opaque-A", action["args"]["url"])
        self.assertNotIn("page=opaque-A", action["args"]["url"])

    def test_non_paginated_response_is_complete_without_followup(self):
        tracker = RetrievalCompletenessTracker()
        snapshot = tracker.observe(_receipt(
            "https://api.vendor.test/item/1",
            '{"id":"1","status":"ok"}',
            number=1,
        ))
        self.assertEqual(0, snapshot["open_chain_count"])
        self.assertIsNone(snapshot["terminal_failure"])

    def test_complete_wire_body_exposes_hint_without_closing_visible_truncation(self):
        root = "https://api.vendor.test/search?q=x&pageSize=50"
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": "A",
        })
        receipt = _receipt(
            root,
            full[:100],
            number=1,
            truncated=True,
            max_chars=100,
            full_body=full,
            wire_complete=True,
            hard_max_chars=100,
        )

        self.assertTrue(receipt["wire_body_complete"])
        self.assertFalse(receipt["visible_body_complete"])
        self.assertEqual(
            "complete_wire_body",
            receipt["pagination"]["scan_source"],
        )
        self.assertEqual(
            "A", receipt["pagination"]["next_hints"][0]["value"]
        )
        self.assertEqual("incomplete", receipt["state"])
        self.assertIn("body_truncated", receipt["incomplete_reasons"])
        self.assertEqual(
            "restart_with_smaller_page",
            receipt["continuation_action"]["kind"],
        )

        tracker = RetrievalCompletenessTracker()
        first = tracker.observe(receipt)
        self.assertEqual(1, first["open_chain_count"])

        # Consuming the hidden cursor directly does not make the missing tail
        # of the first page visible and therefore cannot close the chain.
        cursor_only = tracker.observe(_receipt(
            root + "&pageToken=A",
            '{"items":[2],"nextPageToken":null}',
            number=2,
            max_chars=100,
            hard_max_chars=100,
        ))
        self.assertEqual(1, cursor_only["open_chain_count"])
        self.assertIsNone(cursor_only["terminal_failure"])

    def test_machine_repage_action_closes_same_family_cursor_chain(self):
        root = "https://api.vendor.test/search?q=x&pageSize=50"
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": "A",
        })
        tracker = RetrievalCompletenessTracker()
        tracker.observe(_receipt(
            root,
            full[:100],
            number=1,
            truncated=True,
            max_chars=100,
            full_body=full,
            wire_complete=True,
            hard_max_chars=100,
        ))
        repage = tracker.next_continuation_action()
        self.assertIsNotNone(repage)
        self.assertEqual("restart_with_smaller_page", repage["kind"])
        smaller_url = repage["args"]["url"]
        self.assertNotEqual(root, smaller_url)

        restarted = tracker.observe(_receipt(
            smaller_url,
            '{"items":[1],"nextPageToken":"B"}',
            number=2,
            max_chars=100,
            hard_max_chars=100,
        ))
        self.assertEqual(1, restarted["open_chain_count"])
        follow = tracker.next_continuation_action()
        self.assertEqual("follow_pagination_cursor", follow["kind"])
        self.assertIn("pageToken=B", follow["args"]["url"])

        closed = tracker.observe(_receipt(
            follow["args"]["url"],
            '{"items":[2],"nextPageToken":null}',
            number=3,
            max_chars=100,
            hard_max_chars=100,
        ))
        self.assertEqual(0, closed["open_chain_count"])
        self.assertIsNone(closed["terminal_failure"])

    def test_post_limit_repage_preserves_body_and_narrows_only_window(self):
        url = "https://api.vendor.test/graphql"
        request_body = {
            "query": "query($limit:Int!){records(limit:$limit){id}}",
            "variables": {"limit": 50, "filter": "stable"},
        }
        full = json.dumps({
            "data": {"records": [{"text": "x" * 300}]},
            "nextPageToken": "A",
        })
        receipt = _receipt(
            url,
            full[:100],
            number=1,
            truncated=True,
            max_chars=100,
            method="POST",
            request_body=request_body,
            full_body=full,
            wire_complete=True,
            hard_max_chars=100,
        )

        action = receipt["continuation_action"]
        self.assertEqual("restart_with_smaller_page", action["kind"])
        self.assertEqual("skill_http_post_json", action["tool_name"])
        narrowed = action["args"]["body"]
        self.assertLess(narrowed["variables"]["limit"], 50)
        self.assertEqual("stable", narrowed["variables"]["filter"])
        self.assertEqual(request_body["query"], narrowed["query"])

    def test_schema_max_without_safe_page_window_degrades_immediately(self):
        full = json.dumps({"items": [{"text": "x" * 300}]})
        for url in (
            "https://api.vendor.test/item/1",
            "https://api.vendor.test/search?q=x&pageSize=1",
        ):
            with self.subTest(url=url):
                receipt = _receipt(
                    url,
                    full[:100],
                    number=1,
                    truncated=True,
                    max_chars=100,
                    full_body=full,
                    wire_complete=True,
                    hard_max_chars=100,
                )
                self.assertEqual(
                    "response_exceeds_visible_limit_no_safe_page_window",
                    receipt["continuation_action"]["reason"],
                )
                snapshot = RetrievalCompletenessTracker().observe(receipt)
                self.assertEqual(1, snapshot["open_chain_count"])
                self.assertEqual(
                    "response_exceeds_visible_limit_no_safe_page_window",
                    snapshot["terminal_failure"],
                )

    def test_byte_truncated_wire_is_not_scanned_but_can_repage(self):
        partial = '{"items":[{"text":"' + ("x" * 200)
        receipt = _receipt(
            "https://api.vendor.test/search?q=x&pageSize=50",
            partial[:100],
            number=1,
            truncated=True,
            max_chars=100,
            full_body=partial,
            wire_complete=False,
            hard_max_chars=100,
        )
        self.assertFalse(receipt["wire_body_complete"])
        self.assertFalse(receipt["pagination"]["detected"])
        self.assertEqual(
            "none_partial_wire", receipt["pagination"]["scan_source"]
        )
        self.assertEqual(
            "restart_with_smaller_page",
            receipt["continuation_action"]["kind"],
        )
        self.assertEqual("incomplete", receipt["state"])

    def test_exact_same_truncated_identity_and_limit_is_immediate_no_progress(self):
        root = "https://api.vendor.test/search?q=x&pageSize=50"
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": "A",
        })
        tracker = RetrievalCompletenessTracker()
        tracker.observe(_receipt(
            root,
            full[:100],
            number=1,
            truncated=True,
            max_chars=100,
            full_body=full,
            wire_complete=True,
            hard_max_chars=100,
        ))
        repeated = tracker.observe(_receipt(
            root,
            full[:100],
            number=2,
            truncated=True,
            max_chars=100,
            full_body=full,
            wire_complete=True,
            hard_max_chars=100,
        ))
        self.assertEqual(
            "body_truncation_no_progress_at_limit",
            repeated["terminal_failure"],
        )
        self.assertEqual(2, repeated["total_requests"])

    def test_one_terminal_family_does_not_close_independent_family(self):
        tracker = RetrievalCompletenessTracker()
        url_a = "https://api.a.test/search?q=a&pageSize=50"
        url_b = "https://api.b.test/search?q=b&pageSize=50"
        full_a = json.dumps({"items": [{"text": "a" * 300}]})
        full_b = json.dumps({"items": [{"text": "b" * 300}]})

        tracker.observe(_receipt(
            url_a,
            full_a[:100],
            number=1,
            truncated=True,
            max_chars=100,
            full_body=full_a,
            wire_complete=True,
            hard_max_chars=100,
        ))
        tracker.observe(_receipt(
            url_b,
            full_b[:100],
            number=2,
            truncated=True,
            max_chars=100,
            full_body=full_b,
            wire_complete=True,
            hard_max_chars=100,
        ))
        snapshot = tracker.observe(_receipt(
            url_a,
            full_a[:100],
            number=3,
            truncated=True,
            max_chars=100,
            full_body=full_a,
            wire_complete=True,
            hard_max_chars=100,
        ))

        self.assertIsNone(snapshot["terminal_failure"])
        self.assertEqual(2, snapshot["open_chain_count"])
        self.assertEqual(1, snapshot["terminal_chain_count"])
        self.assertEqual(1, snapshot["runnable_chain_count"])
        action = tracker.next_continuation_action()
        self.assertIsNotNone(action)
        self.assertIn("api.b.test", action["args"]["url"])

    def test_complete_wire_explicit_end_does_not_complete_truncated_evidence(self):
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": None,
        })
        receipt = _receipt(
            "https://api.vendor.test/search?q=x&pageSize=50",
            full[:100],
            number=1,
            truncated=True,
            max_chars=100,
            full_body=full,
            wire_complete=True,
            hard_max_chars=100,
        )
        self.assertTrue(receipt["pagination"]["explicit_end"])
        self.assertEqual("incomplete", receipt["state"])
        snapshot = RetrievalCompletenessTracker().observe(receipt)
        self.assertEqual(1, snapshot["open_chain_count"])

    def test_primary_collection_receipt_counts_nested_array_without_inference(self):
        receipt = _receipt(
            "https://api.vendor.test/search?q=nested",
            json.dumps({
                "data": {
                    "payload": {
                        "records": [{"id": 1}, {"id": 2}, {"id": 3}],
                        "totalCount": 9,
                    }
                }
            }),
            number=1,
        )

        evidence = receipt["collection_evidence"]
        self.assertEqual("observed", evidence["status"])
        primary = evidence["primary_collection"]
        self.assertEqual("$/data/payload/records", primary["path"])
        self.assertEqual(3, primary["observed_items"])
        for key in (
            "path_sha256", "collection_sha256", "page_observation_sha256"
        ):
            self.assertEqual(64, len(primary[key]))
        self.assertEqual(
            {
                "status": "observed",
                "value": 9,
                "path": "$/data/payload/totalCount",
                "path_sha256": evidence["source_declared_total"][
                    "path_sha256"
                ],
            },
            evidence["source_declared_total"],
        )
        # Receipts retain only scalar counts/paths/hashes, never raw records.
        self.assertTrue(all(
            not isinstance(value, (list, dict))
            for value in primary.values()
        ))

    def test_ambiguous_collections_and_non_numeric_total_fail_safe(self):
        ambiguous = _receipt(
            "https://api.vendor.test/search?q=ambiguous",
            json.dumps({
                "alpha": [1, 2],
                "beta": [3, 4],
                "totalCount": 4,
            }),
            number=1,
        )["collection_evidence"]
        self.assertEqual("ambiguous", ambiguous["status"])
        self.assertNotIn("primary_collection", ambiguous)
        self.assertNotIn("source_declared_total", ambiguous)

        non_numeric = _receipt(
            "https://api.vendor.test/search?q=string-total",
            '{"items":[1,2],"totalCount":"200"}',
            number=2,
        )["collection_evidence"]
        self.assertEqual("observed", non_numeric["status"])
        self.assertEqual(
            {"status": "absent"}, non_numeric["source_declared_total"]
        )

    def test_ancestor_total_requires_unambiguous_collection_scope(self):
        pure_envelope = _receipt(
            "https://api.vendor.test/search?q=pure-envelope",
            '{"totalCount":7,"data":{"records":[1,2]}}',
            number=1,
        )["collection_evidence"]
        self.assertEqual(
            7, pure_envelope["source_declared_total"]["value"]
        )

        ambiguous_scope = _receipt(
            "https://api.vendor.test/search?q=ambiguous-scope",
            json.dumps({
                "totalCount": 99,
                "data": {"records": [1, 2]},
                "grouping": {"bucketCount": 4},
            }),
            number=2,
        )["collection_evidence"]
        self.assertEqual("observed", ambiguous_scope["status"])
        self.assertEqual(
            "ambiguous_scope",
            ambiguous_scope["source_declared_total"]["status"],
        )
        self.assertNotIn(
            "value", ambiguous_scope["source_declared_total"]
        )

        nearest_scope = _receipt(
            "https://api.vendor.test/search?q=nearest",
            '{"totalCount":99,"data":{"records":[1,2],"totalCount":7}}',
            number=3,
        )["collection_evidence"]
        self.assertEqual(
            7, nearest_scope["source_declared_total"]["value"]
        )

    def test_completed_two_family_ledger_retains_pages_items_and_explicit_total(self):
        tracker = RetrievalCompletenessTracker()
        family_a_root = "https://api.vendor.test/search?q=alpha"
        family_a_page2 = family_a_root + "&pageToken=A"
        family_b_root = "https://api.vendor.test/search?q=beta"
        first_a = _receipt(
            family_a_root,
            '{"items":[1,2],"nextPageToken":"A"}',
            number=1,
        )
        second_a = _receipt(
            family_a_page2,
            '{"items":[3],"nextPageToken":null}',
            number=2,
        )
        only_b = _receipt(
            family_b_root,
            '{"data":{"records":[4,5],"totalCount":8}}',
            number=3,
        )
        tracker.observe(first_a)
        tracker.observe(second_a)
        snapshot = tracker.observe(only_b)

        self.assertEqual(0, snapshot["open_chain_count"])
        ledger = snapshot["evidence_ledger"]
        self.assertEqual(2, ledger["family_count"])
        families = {
            item["family_sha256"]: item for item in ledger["families"]
        }
        family_a = families[first_a["family_sha256"]]
        self.assertEqual(2, family_a["pages_observed"])
        self.assertEqual(3, family_a["items_observed"])
        self.assertEqual(
            {"status": "absent"}, family_a["source_declared_total"]
        )
        family_b = families[only_b["family_sha256"]]
        self.assertEqual(1, family_b["pages_observed"])
        self.assertEqual(2, family_b["items_observed"])
        self.assertEqual(8, family_b["source_declared_total"]["value"])
        self.assertTrue(
            ledger["quantification_rules"]
            ["items_observed_are_not_source_declared_total"]
        )

    def test_two_families_without_total_use_exact_array_lengths_only(self):
        tracker = RetrievalCompletenessTracker()
        first = _receipt(
            "https://api.vendor.test/search?q=first-family",
            json.dumps({"items": list(range(135))}),
            number=1,
        )
        second = _receipt(
            "https://api.vendor.test/search?q=second-family",
            json.dumps({"data": {"records": list(range(202))}}),
            number=2,
        )
        tracker.observe(first)
        snapshot = tracker.observe(second)
        families = {
            item["family_sha256"]: item
            for item in snapshot["evidence_ledger"]["families"]
        }

        self.assertEqual(135, families[first["family_sha256"]]["items_observed"])
        self.assertEqual(202, families[second["family_sha256"]]["items_observed"])
        self.assertEqual(
            {"status": "absent"},
            families[first["family_sha256"]]["source_declared_total"],
        )
        self.assertEqual(
            {"status": "absent"},
            families[second["family_sha256"]]["source_declared_total"],
        )

    def test_same_request_retry_changed_body_replaces_instead_of_summing(self):
        tracker = RetrievalCompletenessTracker()
        url = "https://api.vendor.test/search?q=mutable"
        first = _receipt(
            url,
            '{"items":[1,2],"nextPageToken":null}',
            number=1,
        )
        changed_retry = _receipt(
            url,
            '{"items":[1,2,3,4,5],"nextPageToken":null}',
            number=2,
        )
        first_snapshot = tracker.observe(first)
        first_observation_hash = first_snapshot[
            "evidence_ledger"
        ]["families"][0]["observations_sha256"]
        snapshot = tracker.observe(changed_retry)
        family = snapshot["evidence_ledger"]["families"][0]

        self.assertEqual(2, family["http_responses_observed"])
        self.assertEqual(1, family["pages_observed"])
        self.assertEqual(5, family["items_observed"])
        self.assertEqual(1, family["replaced_request_observations"])
        self.assertEqual(0, family["deduplicated_page_observations"])
        self.assertNotEqual(
            first_observation_hash, family["observations_sha256"]
        )

    def test_partial_wire_and_scan_ambiguity_never_create_item_count(self):
        partial = _receipt(
            "https://api.vendor.test/search?q=partial",
            '{"items":[1,2',
            number=1,
            truncated=True,
            wire_complete=False,
        )
        tracker = RetrievalCompletenessTracker()
        snapshot = tracker.observe(partial)
        family = snapshot["evidence_ledger"]["families"][0]
        self.assertEqual(0, family["pages_observed"])
        self.assertEqual(0, family["items_observed"])
        self.assertEqual(
            {"unavailable_partial_wire": 1},
            family["uncounted_statuses"],
        )

    def test_machine_gap_is_persisted_before_typed_footer(self):
        raw = {
            "status": "unresolved",
            "source": "harness_http_retrieval_completeness",
            "terminal_reason": "pagination_page_limit",
            "open_chain_count": 1,
            "open_frontier_count": 2,
            "total_requests": 12,
            "open_reasons": {"pagination_more_available": 1},
        }
        normalized = _normalized_unresolved_retrieval(raw)
        self.assertIsNotNone(normalized)
        footer = (
            'RESULT_FIELDS_JSON: {"evidence":{"status":"degraded",'
            '"reason":"coverage unresolved","provenance":"authorized API"}}'
        )
        persisted = _inject_unresolved_retrieval_gap(
            "Evidence summary.\n" + footer,
            normalized,
        )

        marker = "[HARNESS_UNRESOLVED_HTTP_RETRIEVAL]"
        self.assertIn(marker, persisted)
        self.assertLess(persisted.index(marker), persisted.index(footer))
        audit = audit_result_fields(
            persisted,
            ["evidence"],
            {
                "evidence": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "reason": {"type": "string"},
                        "provenance": {"type": "string"},
                    },
                },
            },
        )
        self.assertTrue(audit["footer_valid"])
        self.assertEqual(["evidence"], audit["degraded"])

    def test_advisory_frontier_is_persisted_without_degraded_status(self):
        raw = {
            "status": "unresolved",
            "source": "harness_http_retrieval_completeness",
            "quality_impact": "advisory",
            "retrieval_completeness_policy": "bounded",
            "coverage_status": "partial",
            "open_chain_count": 1,
            "open_reasons": {"pagination_more_available": 1},
        }
        normalized = _normalized_unresolved_retrieval(raw)
        self.assertEqual("advisory", normalized["quality_impact"])
        persisted = _inject_unresolved_retrieval_gap(
            "Observed-page synthesis.",
            normalized,
        )

        self.assertIn("Coverage: bounded HTTP acquisition", persisted)
        self.assertIn("[HARNESS_UNRESOLVED_HTTP_RETRIEVAL]", persisted)
        self.assertNotIn("WARN/degraded", persisted)


if __name__ == "__main__":
    unittest.main()
