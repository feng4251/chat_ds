from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agent_loop import (
    _bounded_skill_execution_exposure,
    _declared_child_tools,
)
from skill_capability_plan import (
    build_capability_catalog,
    capability_call_satisfies_candidate,
    validate_capability_plan,
)
from skills.http_grants import (
    compile_loaded_skill_http_grants,
    compile_loaded_skill_http_post_grants,
)
from tools import skill_http
from tools.context import ToolContext
from tools.delegation import _exact_capability_skill_http_grants
from tools.delegation import (
    _exact_capability_skill_http_post_grants,
    _tool_allowed_in_child,
)
from tools.registry import get_metadata, registry


class _FakeContent:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b'{"data":{"ok":true}}',
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.headers = headers or {
            "Content-Type": "application/json; charset=utf-8"
        }
        self.content = _FakeContent([body])
        self.charset = "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    response = _FakeResponse()
    responses: list[_FakeResponse] = []
    constructor_kwargs: list[dict] = []
    posts: list[tuple[str, dict]] = []

    def __init__(self, **kwargs):
        self.connector = kwargs.get("connector")
        self.constructor_kwargs.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self.connector is not None:
            closed = self.connector.close()
            if inspect.isawaitable(closed):
                await closed
        return False

    def post(self, url: str, **kwargs):
        self.posts.append((url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return self.response


class SkillHttpPostJsonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        with skill_http._request_count_lock:
            skill_http._request_counts.clear()
            skill_http._root_request_counts.clear()
            skill_http._user_request_windows.clear()
            skill_http._active_by_host.clear()
            skill_http._active_by_root.clear()
            skill_http._active_by_user.clear()
            skill_http._active_request_total = 0
        _FakeSession.responses.clear()
        _FakeSession.posts.clear()
        _FakeSession.constructor_kwargs.clear()
        _FakeSession.response = _FakeResponse()
        self.context = ToolContext(
            user_id="post-user",
            session_id="post-session",
            run_id="post-run",
            root_run_id="post-root",
            allowed_skill_http_prefixes=((
                "json-api",
                "https://api.vendor.test/v1/graphql",
            ),),
            allowed_skill_http_post_prefixes=((
                "json-api",
                "https://api.vendor.test/v1/graphql",
            ),),
        )

    def _network_patches(self):
        return (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
        )

    async def test_success_uses_fixed_json_headers_and_bounded_audit_receipt(self):
        request_body = {
            "query": "query Record($id: ID!) { record(id: $id) { id } }",
            "variables": {"id": "R-123"},
        }
        dns_patch, session_patch = self._network_patches()
        with dns_patch, session_patch:
            result = json.loads(await skill_http.skill_http_post_json(
                "https://api.vendor.test/v1/graphql",
                request_body,
                max_chars=10_000,
                context=self.context,
            ))

        encoded = json.dumps(
            request_body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual("success", result["status"])
        self.assertEqual("POST", result["request_method"])
        self.assertEqual(len(encoded), result["request_body_bytes"])
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            result["request_body_sha256"],
        )
        self.assertEqual("json-api", result["matched_skill"])
        self.assertEqual(
            hashlib.sha256(
                b"https://api.vendor.test/v1/graphql"
            ).hexdigest(),
            result["matched_prefix_sha256"],
        )
        self.assertEqual(1, result["request_number"])
        self.assertEqual("complete", result["retrieval"]["state"])
        self.assertEqual("POST", result["retrieval"]["request_method"])
        self.assertEqual(
            64, len(result["retrieval"]["request_identity_sha256"])
        )
        self.assertEqual(1, len(_FakeSession.posts))
        _, post_kwargs = _FakeSession.posts[0]
        self.assertEqual(encoded, post_kwargs["data"])
        self.assertFalse(post_kwargs["allow_redirects"])
        headers = _FakeSession.constructor_kwargs[0]["headers"]
        self.assertEqual("application/json", headers["Content-Type"])
        self.assertEqual("application/json", headers["Accept"])
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    async def test_host_path_query_and_missing_grant_fail_before_network(self):
        cases = (
            (
                ToolContext(),
                "https://api.vendor.test/v1/graphql",
                "missing_skill_http_grant",
            ),
            (
                self.context,
                "https://other.vendor.test/v1/graphql",
                "skill_http_boundary_violation",
            ),
            (
                self.context,
                "https://api.vendor.test/v1/graphql/adjacent",
                "skill_http_boundary_violation",
            ),
            (
                self.context,
                "https://api.vendor.test/v1/graphql?api_key=secret",
                "invalid_url",
            ),
        )
        for context, url, expected in cases:
            with self.subTest(url=url):
                result = json.loads(await skill_http.skill_http_post_json(
                    url, {"query": "{ ping }"}, context=context
                ))
                self.assertEqual(expected, result["error_code"])
                self.assertFalse(result["request_sent"])
        self.assertEqual([], _FakeSession.posts)

    async def test_invalid_json_shape_depth_nodes_size_numbers_and_credentials(self):
        deep: dict = {}
        cursor = deep
        for _ in range(skill_http.MAX_JSON_BODY_DEPTH):
            child: dict = {}
            cursor["next"] = child
            cursor = child
        cyclic: dict = {}
        cyclic["self"] = cyclic
        invalid_bodies = (
            ["not", "an", "object"],
            {"number": float("nan")},
            {"number": float("inf")},
            {"blob": b"not-json"},
            {"text": chr(0xD800)},
            deep,
            cyclic,
            {"wide": list(range(skill_http.MAX_JSON_BODY_NODES))},
            {"text": "x" * (skill_http.MAX_JSON_BODY_BYTES + 1)},
            {"variables": {"access_token": "secret"}},
            {"headers": {"Authorization": "Bearer secret"}},
            {"headers": {"Cookie": "session=secret"}},
        )
        for body in invalid_bodies:
            with self.subTest(kind=type(body).__name__):
                result = json.loads(await skill_http.skill_http_post_json(
                    "https://api.vendor.test/v1/graphql",
                    body,  # type: ignore[arg-type]
                    context=self.context,
                ))
                self.assertEqual("invalid_json_body", result["error_code"])
                self.assertFalse(result["request_sent"])
        self.assertEqual([], _FakeSession.posts)

    async def test_post_redirects_fail_closed_without_method_rewrite_or_replay(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.setUp()
                _FakeSession.response = _FakeResponse(
                    status=status,
                    headers={"Location": "https://api.vendor.test/v1/graphql"},
                )
                dns_patch, session_patch = self._network_patches()
                with dns_patch, session_patch:
                    result = json.loads(await skill_http.skill_http_post_json(
                        "https://api.vendor.test/v1/graphql",
                        {"query": "{ ping }"},
                        context=self.context,
                    ))
                self.assertEqual("unsafe_post_redirect", result["error_code"])
                self.assertTrue(result["request_sent"])
                self.assertEqual(0, result["redirects_followed"])
                self.assertEqual(1, len(_FakeSession.posts))

    async def test_shared_quota_and_response_bounds_are_enforced(self):
        run_identity, _root_identity, _user_identity = (
            skill_http._quota_identities(self.context)
        )
        skill_http._request_counts[run_identity] = skill_http.MAX_REQUESTS_PER_RUN
        blocked = json.loads(await skill_http.skill_http_post_json(
            "https://api.vendor.test/v1/graphql",
            {"query": "{ ping }"},
            context=self.context,
        ))
        self.assertEqual("skill_http_request_limit", blocked["error_code"])
        self.assertFalse(blocked["request_sent"])

        self.setUp()
        _FakeSession.response = _FakeResponse(body=b'{"long":"response"}')
        dns_patch, session_patch = self._network_patches()
        with dns_patch, session_patch:
            bounded = json.loads(await skill_http.skill_http_post_json(
                "https://api.vendor.test/v1/graphql",
                {"query": "{ ping }"},
                max_chars=5,
                context=self.context,
            ))
        self.assertTrue(bounded["body_truncated"])
        self.assertEqual(5, bounded["body_chars"])
        self.assertEqual("incomplete", bounded["retrieval"]["state"])
        self.assertIn(
            "body_truncated",
            bounded["retrieval"]["incomplete_reasons"],
        )

    async def test_post_complete_wire_scan_builds_exact_limit_repage(self):
        full = json.dumps({
            "data": {"records": [{"text": "x" * 300}]},
            "nextPageToken": "A",
        }).encode("utf-8")
        _FakeSession.response = _FakeResponse(body=full)
        request_body = {
            "query": "query($limit:Int!){records(limit:$limit){id}}",
            "variables": {"limit": 50, "filter": "stable"},
        }
        dns_patch, session_patch = self._network_patches()
        with (
            dns_patch,
            session_patch,
            patch.object(skill_http, "MAX_CHARS", 100),
        ):
            result = json.loads(await skill_http.skill_http_post_json(
                "https://api.vendor.test/v1/graphql",
                request_body,
                max_chars=100,
                context=self.context,
            ))

        receipt = result["retrieval"]
        self.assertTrue(receipt["wire_body_complete"])
        self.assertFalse(receipt["visible_body_complete"])
        self.assertEqual(
            "complete_wire_body", receipt["pagination"]["scan_source"]
        )
        evidence = receipt["collection_evidence"]
        self.assertEqual("observed", evidence["status"])
        self.assertEqual(
            1, evidence["primary_collection"]["observed_items"]
        )
        self.assertEqual(
            "$/data/records", evidence["primary_collection"]["path"]
        )
        action = receipt["continuation_action"]
        self.assertEqual("restart_with_smaller_page", action["kind"])
        self.assertEqual("skill_http_post_json", action["tool_name"])
        self.assertLess(action["args"]["body"]["variables"]["limit"], 50)
        self.assertEqual(
            "stable", action["args"]["body"]["variables"]["filter"]
        )
        self.assertEqual(request_body["query"], action["args"]["body"]["query"])

    async def test_non_json_response_is_rejected_after_one_audited_attempt(self):
        _FakeSession.response = _FakeResponse(
            body=b"<html>not json</html>",
            headers={"Content-Type": "text/html"},
        )
        dns_patch, session_patch = self._network_patches()
        with dns_patch, session_patch:
            result = json.loads(await skill_http.skill_http_post_json(
                "https://api.vendor.test/v1/graphql",
                {"query": "{ ping }"},
                context=self.context,
            ))
        self.assertEqual("unsupported_content_type", result["error_code"])
        self.assertTrue(result["request_sent"])

    async def test_dns_lookup_obeys_total_post_deadline(self):
        async def slow_dns(_hostname: str):
            await asyncio.sleep(2)
            return (("203.0.113.10", 2),)

        started = asyncio.get_running_loop().time()
        with patch.object(skill_http, "_public_addresses", slow_dns):
            result = json.loads(await skill_http.skill_http_post_json(
                "https://api.vendor.test/v1/graphql",
                {"query": "{ ping }"},
                timeout=1,
                context=self.context,
            ))
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual("skill_http_timeout", result["error_code"])
        self.assertFalse(result["request_sent"])
        self.assertLess(elapsed, 1.5)


class SkillHttpPostCapabilityTests(unittest.TestCase):
    def test_method_authority_requires_explicit_post_or_graphql(self):
        ordinary = {
            "_chatds_scope": "session",
            "content": "API endpoint: https://api.vendor.test/v1/records",
        }
        explicit = {
            "_chatds_scope": "session",
            "content": "POST JSON to https://api.vendor.test/v1/records",
        }
        negated = {
            "_chatds_scope": "session",
            "content": (
                "Do not POST to https://api.vendor.test/v1/records\n"
                "不要 POST 到 https://api.vendor.test/v1/other"
            ),
        }

        self.assertEqual(
            (("json-api", "https://api.vendor.test/v1/records"),),
            compile_loaded_skill_http_grants("json-api", ordinary),
        )
        self.assertEqual(
            (), compile_loaded_skill_http_post_grants("json-api", ordinary)
        )
        self.assertEqual(
            (("json-api", "https://api.vendor.test/v1/records"),),
            compile_loaded_skill_http_post_grants("json-api", explicit),
        )
        self.assertEqual(
            (), compile_loaded_skill_http_post_grants("json-api", negated)
        )

    def test_opentargets_style_endpoint_and_browser_are_method_isolated(self):
        package = {
            "_chatds_scope": "session",
            "content": (
                # Exact V2.3 OpenTargets Skill excerpt: both URLs share the
                # same paragraph, so paragraph-wide method inference would be
                # unsafe. The endpoint path is self-declaring; the browser is not.
                "## GraphQL API Details\n\n"
                "Key information:\n"
                "- **Endpoint:** "
                "`https://api.platform.opentargets.org/api/v4/graphql`\n"
                "- **Interactive browser:** "
                "`https://api.platform.opentargets.org/api/v4/graphql/browser`\n"
                "- **No authentication required**\n"
            ),
        }

        self.assertEqual(
            ((
                "opentargets",
                "https://api.platform.opentargets.org/api/v4/graphql",
            ),),
            compile_loaded_skill_http_post_grants("opentargets", package),
        )

    @staticmethod
    def _structured_exposure(
        capability_package: dict,
        pipeline_root: Path | None = None,
    ):
        capability_package = dict(capability_package)
        capability_root_value = (
            capability_package.get("skill_dir")
            or (
                capability_package.get("resource_graph") or {}
            ).get("skill_root")
        )
        if capability_root_value:
            capability_root = Path(str(capability_root_value))
            capability_main = capability_root / "SKILL.md"
            if capability_main.is_file():
                capability_package["skill_dir"] = str(capability_root)
                capability_package["skill_md_sha256"] = hashlib.sha256(
                    capability_main.read_bytes()
                ).hexdigest()
        pipeline_package = {
            "name": "pipeline",
            "_chatds_scope": "session",
            "content": "Execute the declared pipeline.",
        }
        if pipeline_root is not None:
            pipeline_root.mkdir(parents=True, exist_ok=True)
            pipeline_main = pipeline_root / "SKILL.md"
            pipeline_main.write_text(
                "Execute the declared pipeline.",
                encoding="utf-8",
            )
            pipeline_package.update({
                "skill_dir": str(pipeline_root),
                "skill_md_sha256": hashlib.sha256(
                    pipeline_main.read_bytes()
                ).hexdigest(),
            })
        worker = {
            "id": "collector",
            "file": "workers/collector.yaml",
            "skills": ["json-api"],
        }
        workflow = {
            "execution_contract": {
                "workers": [worker],
                "routes": [{
                    "id": "report",
                    "patterns": ["report"],
                    "workers": ["collector"],
                }],
            },
            "workers": [worker],
        }
        return _bounded_skill_execution_exposure(
            "use pipeline to produce report",
            [
                "skills_list", "skill_view", "delegate_task",
                "skill_http_get", "skill_http_post_json",
            ],
            {"pipeline", "json-api"},
            {
                "pipeline": {
                    **pipeline_package,
                    "workflow_contract": workflow,
                },
                "json-api": capability_package,
            },
            {},
            selected_skill_names=("pipeline",),
        )

    def test_registry_schema_has_no_header_or_credential_input(self):
        schema = registry.get_schema("skill_http_post_json")
        self.assertIsInstance(schema, dict)
        properties = schema["parameters"]["properties"]
        self.assertEqual(
            {"url", "body", "max_chars", "timeout"},
            set(properties),
        )
        self.assertFalse(schema["parameters"]["additionalProperties"])
        metadata = get_metadata("skill_http_post_json")
        self.assertFalse(metadata["read_only"])
        self.assertTrue(metadata["destructive"])
        self.assertTrue(metadata["allow_in_parallel_child"])
        self.assertFalse(metadata["mutates_workspace"])
        self.assertTrue(_tool_allowed_in_child(
            "skill_http_post_json", parallel_child=True
        ))

    def test_standard_planner_issues_method_specific_exact_candidates(self):
        prefix = "https://api.vendor.test/v1/graphql"
        catalog = build_capability_catalog(
            skill_name="json-api",
            loaded_package={
                "content": (
                    "POST JSON to https://api.vendor.test/v1/graphql "
                    "and use the returned object."
                ),
                "frontmatter": {"name": "json-api"},
            },
            available_tools=[
                "skill_view", "skill_http_get", "skill_http_post_json",
            ],
            http_prefixes=(("json-api", prefix),),
            http_post_prefixes=(("json-api", prefix),),
        )
        by_tool = {
            item["tool_name"]: item
            for item in catalog["candidates"]
            if item.get("kind") == "skill_http_prefix"
        }
        self.assertEqual(
            {"skill_http_get", "skill_http_post_json"}, set(by_tool)
        )
        post = by_tool["skill_http_post_json"]
        plan = validate_capability_plan(
            catalog,
            skill_name="json-api",
            body_sha256=catalog["body_sha256"],
            required=[post["id"]],
            optional=[],
            unsupported=[],
        )
        self.assertTrue(plan.valid, plan.payload)
        self.assertEqual(
            ["skill_view", "skill_http_post_json"],
            plan.payload["selected_tools"],
        )
        self.assertEqual(
            [],
            plan.payload["allowed_skill_http_prefixes"],
        )
        self.assertEqual(
            [["json-api", prefix]],
            plan.payload["allowed_skill_http_post_prefixes"],
        )
        grant = (("json-api", prefix),)
        args = {
            "url": prefix,
            "body": {"query": "{ ping }"},
        }
        self.assertFalse(capability_call_satisfies_candidate(
            post,
            tool_name="skill_http_get",
            args={"url": prefix},
            result_data={"request_sent": True},
            allowed_skill_http_post_prefixes=grant,
        ))
        self.assertFalse(capability_call_satisfies_candidate(
            post,
            tool_name="skill_http_post_json",
            args=args,
            result_data={"request_sent": False, "error_code": "invalid_json_body"},
            outcome="error",
            allowed_skill_http_post_prefixes=grant,
        ))
        self.assertTrue(capability_call_satisfies_candidate(
            post,
            tool_name="skill_http_post_json",
            args=args,
            result_data={"request_sent": True, "status": "success"},
            allowed_skill_http_post_prefixes=grant,
        ))
        safe_prefix_hash = hashlib.sha256(
            prefix.encode("utf-8")
        ).hexdigest()
        self.assertTrue(capability_call_satisfies_candidate(
            post,
            tool_name="skill_http_post_json",
            args={},
            result_data={
                "request_sent": True,
                "status": "success",
                "matched_skill": "json-api",
                "matched_prefix_sha256": safe_prefix_hash,
            },
            allowed_skill_http_post_prefixes=grant,
        ))
        self.assertFalse(capability_call_satisfies_candidate(
            post,
            tool_name="skill_http_post_json",
            args={},
            result_data={
                "request_sent": True,
                "status": "success",
                "matched_skill": "json-api",
                "matched_prefix_sha256": hashlib.sha256(
                    b"https://api.vendor.test/v1/"
                ).hexdigest(),
            },
            allowed_skill_http_post_prefixes=grant,
        ))

    def test_declared_child_gets_bridges_only_for_its_exact_capability_skill(self):
        available = [
            "skill_view", "skill_http_get", "skill_http_post_json",
        ]
        declared = {"skills": ["json-api"]}
        child = _declared_child_tools(
            available,
            declared,
            http_capability_skills=["json-api"],
            http_post_capability_skills=["json-api"],
        )
        unrelated = _declared_child_tools(
            available,
            declared,
            http_capability_skills=["other-api"],
            http_post_capability_skills=["other-api"],
        )
        self.assertIn("skill_http_get", child)
        self.assertIn("skill_http_post_json", child)
        self.assertNotIn("skill_http_get", unrelated)
        self.assertNotIn("skill_http_post_json", unrelated)

        grants = _exact_capability_skill_http_grants(
            ["json-api"],
            context=ToolContext(
                skill_execution_resource_boundary=True,
                allowed_skill_http_prefixes=(
                    ("json-api", "https://api.vendor.test/v1/graphql"),
                    ("other-api", "https://other.vendor.test/v2/"),
                ),
                allowed_skill_http_post_prefixes=(
                    ("json-api", "https://api.vendor.test/v1/graphql"),
                    ("other-api", "https://other.vendor.test/v2/"),
                ),
            ),
        )
        self.assertEqual(
            [("json-api", "https://api.vendor.test/v1/graphql")], grants
        )
        post_grants = _exact_capability_skill_http_post_grants(
            ["json-api"],
            context=ToolContext(
                skill_execution_resource_boundary=True,
                allowed_skill_http_post_prefixes=(
                    ("json-api", "https://api.vendor.test/v1/graphql"),
                    ("other-api", "https://other.vendor.test/v2/"),
                ),
            ),
        )
        self.assertEqual(
            [("json-api", "https://api.vendor.test/v1/graphql")],
            post_grants,
        )

    def test_capability_reference_graph_mints_only_referenced_template_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "json-api"
            references = root / "references"
            references.mkdir(parents=True)
            (root / "SKILL.md").write_text(
                "Read references/api.md before making the request.\n",
                encoding="utf-8",
            )
            (references / "api.md").write_text(
                "POST JSON to "
                "https://api.vendor.test/v2/graphql/{operation}.\n",
                encoding="utf-8",
            )
            (references / "hidden.md").write_text(
                "https://hidden.vendor.test/private/\n",
                encoding="utf-8",
            )
            package = {
                "name": "json-api",
                "_chatds_scope": "session",
                "content": "Read references/api.md before making the request.",
                "linked_files": {
                    "references": [
                        "references/api.md", "references/hidden.md",
                    ],
                },
                "resource_graph": {"skill_root": str(root)},
                "workflow_contract": None,
            }
            exposure = self._structured_exposure(
                package,
                root.parent / "pipeline",
            )

        self.assertIn("skill_http_post_json", exposure.tools)
        self.assertEqual(
            (("json-api", "https://api.vendor.test/v2/graphql/"),),
            exposure.allowed_skill_http_prefixes,
        )
        self.assertEqual(
            (("json-api", "https://api.vendor.test/v2/graphql/"),),
            exposure.allowed_skill_http_post_prefixes,
        )
        self.assertNotIn(
            ("json-api", "references/api.md"),
            exposure.allowed_skill_resources,
            "HTTP scan inventory must not become child read authority",
        )
        self.assertIn(
            ("json-api", "SKILL.md"), exposure.allowed_skill_resources
        )

    def test_structured_plain_literal_is_get_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "json-api"
            root.mkdir()
            (root / "SKILL.md").write_text(
                "API endpoint: https://api.vendor.test/v1/records",
                encoding="utf-8",
            )
            package = {
                "name": "json-api",
                "_chatds_scope": "session",
                "content": (
                    "API endpoint: "
                    "https://api.vendor.test/v1/records"
                ),
                "linked_files": {},
                "resource_graph": {"skill_root": str(root)},
                "workflow_contract": None,
            }
            exposure = self._structured_exposure(
                package,
                root.parent / "pipeline",
            )

        self.assertIn("skill_http_get", exposure.tools)
        self.assertNotIn("skill_http_post_json", exposure.tools)
        self.assertEqual(
            (("json-api", "https://api.vendor.test/v1/records"),),
            exposure.allowed_skill_http_prefixes,
        )
        self.assertEqual((), exposure.allowed_skill_http_post_prefixes)

    def test_capability_http_inventory_overflow_fails_closed(self):
        from skills.http_grants import MAX_HTTP_GRANT_RESOURCES

        resources = [
            f"references/resource-{index}.md"
            for index in range(MAX_HTTP_GRANT_RESOURCES + 1)
        ]
        package = {
            "name": "json-api",
            "_chatds_scope": "session",
            "content": "POST https://api.vendor.test/v1/graphql",
            "linked_files": {"references": resources},
            "resource_graph": {"skill_root": "/not/read/on-overflow"},
            "workflow_contract": None,
        }
        exposure = self._structured_exposure(package)

        self.assertEqual((), exposure.allowed_skill_http_prefixes)
        self.assertEqual((), exposure.allowed_skill_http_post_prefixes)
        self.assertNotIn("skill_http_get", exposure.tools)
        self.assertNotIn("skill_http_post_json", exposure.tools)


if __name__ == "__main__":
    unittest.main()
