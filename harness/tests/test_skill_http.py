from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from skills.http_grants import (
    canonical_sandbox_http_prefix,
    canonical_https_prefix,
    canonical_https_request_url,
    compile_loaded_skill_http_grants,
    compile_loaded_skill_sandbox_egress_rules,
    compile_user_sandbox_egress_rules,
    compile_user_sandbox_egress_urls,
    extract_literal_https_prefixes,
    extract_literal_sandbox_egress_rules,
)
from tools.context import ToolContext
from tools import skill_http
from agent_loop import _bounded_skill_execution_exposure, _declared_child_tools


class SkillHttpGrantTests(unittest.TestCase):
    def test_current_user_urls_intersect_only_manifest_bound_methods(
        self,
    ) -> None:
        urls = compile_user_sandbox_egress_urls(
            (
                "请打开 https://News.Example.test/story?id=42#comments，"
                "并检查 http://10.10.132.126:18443/dashboard。"
            )
        )
        rules = compile_user_sandbox_egress_rules(
            urls,
            [{
                "source": "argv",
                "selector": "--url",
                "methods": ["GET", "HEAD"],
                "scope": "url",
            }],
            invocation={
                "source": "argv",
                "args": [
                    "--url",
                    "https://News.Example.test/story?id=42#comments",
                ],
            },
        )

        self.assertEqual(
            ((
                "https://news.example.test:443/story?id=42",
                ("GET", "HEAD"),
            ),),
            rules,
        )

    def test_origin_scope_is_explicit_and_model_text_is_not_a_source(
        self,
    ) -> None:
        binding = [{
            "source": "stdin_json",
            "selector": "url",
            "command": "goto",
            "methods": ["GET"],
            "scope": "origin",
        }]

        self.assertEqual(
            ((
                "https://portal.vendor.test:443/",
                ("GET",),
            ),),
            compile_user_sandbox_egress_rules(
                compile_user_sandbox_egress_urls(
                    "浏览 https://portal.vendor.test/path/page"
                ),
                binding,
                invocation={
                    "source": "stdin_json",
                    "command": "goto",
                    "payload": {
                        "url": "https://portal.vendor.test/path/page",
                    },
                },
            ),
        )
        self.assertEqual(
            (),
            compile_user_sandbox_egress_rules(
                (),
                binding,
                invocation={
                    "source": "stdin_json",
                    "command": "goto",
                    "payload": {
                        "url": "https://portal.vendor.test/path/page",
                    },
                },
            ),
        )
        self.assertEqual(
            (),
            compile_user_sandbox_egress_rules(
                compile_user_sandbox_egress_urls(
                    "浏览 https://portal.vendor.test/path/page"
                ),
                [{
                    **binding[0],
                    "methods": ["CONNECT"],
                }],
                invocation={
                    "source": "stdin_json",
                    "command": "goto",
                    "payload": {
                        "url": "https://portal.vendor.test/path/page",
                    },
                },
            ),
        )

    def test_user_url_bindings_match_only_actual_selector_and_invocation(
        self,
    ) -> None:
        urls = compile_user_sandbox_egress_urls(
            "use https://one.example.test/a and "
            "https://two.example.test/b"
        )
        bindings = [
            {
                "source": "argv",
                "selector": 0,
                "methods": ["GET"],
                "scope": "url",
            },
            {
                "source": "argv",
                "selector": "--target",
                "methods": ["POST"],
                "scope": "origin",
            },
            {
                "source": "python",
                "selector": "url",
                "callable": "fetch",
                "methods": ["HEAD"],
                "scope": "url",
            },
        ]

        self.assertEqual(
            ((
                "https://one.example.test:443/a",
                ("GET",),
            ),),
            compile_user_sandbox_egress_rules(
                urls,
                bindings,
                invocation={
                    "source": "argv",
                    "args": ["https://one.example.test/a"],
                },
            ),
        )
        self.assertEqual(
            ((
                "https://two.example.test:443/",
                ("POST",),
            ),),
            compile_user_sandbox_egress_rules(
                urls,
                bindings,
                invocation={
                    "source": "argv",
                    "args": [
                        "--target=https://two.example.test/b",
                    ],
                },
            ),
        )
        self.assertEqual(
            ((
                "https://two.example.test:443/b",
                ("HEAD",),
            ),),
            compile_user_sandbox_egress_rules(
                urls,
                bindings,
                invocation={
                    "source": "python",
                    "callable": "fetch",
                    "parameters": {
                        "url": "https://two.example.test/b",
                    },
                },
            ),
        )
        # A different callable/path or an ambiguous repeated flag cannot
        # borrow either binding's method or scope.
        for invocation in (
            {
                "source": "python",
                "callable": "other",
                "parameters": {
                    "url": "https://two.example.test/b",
                },
            },
            {
                "source": "argv",
                "args": [
                    "--target",
                    "https://one.example.test/a",
                    "--target",
                    "https://two.example.test/b",
                ],
            },
            {
                "source": "argv",
                "args": ["https://one.example.test/changed"],
            },
        ):
            self.assertEqual(
                (),
                compile_user_sandbox_egress_rules(
                    urls,
                    bindings,
                    invocation=invocation,
                ),
            )
        self.assertEqual(
            (),
            compile_user_sandbox_egress_rules(
                urls,
                [{
                    "source": "argv",
                    "selector": -1,
                    "methods": ["GET"],
                    "scope": "url",
                }],
                invocation={
                    "source": "argv",
                    "args": ["https://one.example.test/a"],
                },
            ),
        )

    def test_sandbox_compiler_retains_exact_scheme_query_and_methods(
        self,
    ) -> None:
        rules = extract_literal_sandbox_egress_rules([
            "curl -X PUT "
            "http://api.vendor.test:8080/v1/items?tenant=alpha\n"
            "requests.patch("
            "\"https://api.vendor.test/v1/items/42?mode=strict\", "
            "json=payload)\n"
            "Use DELETE request at "
            "https://api.vendor.test/v1/items/42\n"
            "OPTIONS https://api.vendor.test/v1/capabilities\n"
            "POST JSON to https://api.vendor.test/v1/graphql\n"
        ])

        self.assertEqual(
            (
                (
                    "http://api.vendor.test:8080/v1/items?tenant=alpha",
                    ("GET", "HEAD", "PUT"),
                ),
                (
                    "https://api.vendor.test:443/v1/capabilities",
                    ("GET", "HEAD", "OPTIONS"),
                ),
                (
                    "https://api.vendor.test:443/v1/graphql",
                    ("GET", "HEAD", "POST"),
                ),
                (
                    "https://api.vendor.test:443/v1/items/42",
                    ("GET", "HEAD", "DELETE"),
                ),
                (
                    "https://api.vendor.test:443/v1/items/42?mode=strict",
                    ("GET", "HEAD", "PATCH"),
                ),
            ),
            rules,
        )
        self.assertEqual(
            "http://api.vendor.test:8080/v1/items?tenant=alpha",
            canonical_sandbox_http_prefix(
                "HTTP://API.VENDOR.TEST:8080/v1/items?tenant=alpha"
            ),
        )

    def test_sandbox_method_compiler_rejects_negated_or_ambiguous_prose(
        self,
    ) -> None:
        rules = extract_literal_sandbox_egress_rules([
            "Do not DELETE "
            "https://api.vendor.test/v1/items/42\n"
            "PATCH may refer to either "
            "https://api.vendor.test/v1/a or "
            "https://api.vendor.test/v1/b\n"
            "Never use OPTIONS at "
            "http://api.vendor.test:8080/v1/private\n"
        ])

        self.assertEqual(
            (
                (
                    "http://api.vendor.test:8080/v1/private",
                    ("GET", "HEAD"),
                ),
                (
                    "https://api.vendor.test:443/v1/a",
                    ("GET", "HEAD"),
                ),
                (
                    "https://api.vendor.test:443/v1/b",
                    ("GET", "HEAD"),
                ),
                (
                    "https://api.vendor.test:443/v1/items/42",
                    ("GET", "HEAD"),
                ),
            ),
            rules,
        )

    def test_sandbox_default_retrieval_drops_explicitly_negated_methods(
        self,
    ) -> None:
        rules = extract_literal_sandbox_egress_rules([
            "Never GET https://api.vendor.test/v1/head-only\n"
            "Never HEAD https://api.vendor.test/v1/get-only\n"
            "Do not GET or HEAD "
            "https://api.vendor.test/v1/negative-example\n"
        ])

        self.assertEqual(
            (
                (
                    "https://api.vendor.test:443/v1/get-only",
                    ("GET",),
                ),
                (
                    "https://api.vendor.test:443/v1/head-only",
                    ("HEAD",),
                ),
            ),
            rules,
        )

    def test_loaded_session_skill_compiles_method_rules_not_catalog_text(
        self,
    ) -> None:
        package = {
            "_chatds_scope": "session",
            "content": (
                "curl --request=DELETE "
                "https://api.vendor.test/v2/jobs/42?confirm=true"
            ),
            "description": (
                "curl -X PUT https://ambient.vendor.test/admin"
            ),
        }

        self.assertEqual(
            ((
                "maintenance-api",
                "https://api.vendor.test:443/v2/jobs/42?confirm=true",
                ("GET", "HEAD", "DELETE"),
            ),),
            compile_loaded_skill_sandbox_egress_rules(
                "maintenance-api",
                package,
            ),
        )

    def test_literal_https_compiler_is_bounded_and_drops_examples(self) -> None:
        prefixes = extract_literal_https_prefixes([
            "API https://api.vendor.test/v1/ and "
            "example https://example.com/v1/ and http://plain.invalid/x"
        ])

        self.assertEqual(("https://api.vendor.test/v1/",), prefixes)
        self.assertEqual(
            "https://api.vendor.test/v1/search",
            canonical_https_prefix("https://API.VENDOR.TEST/v1/search?demo=1"),
        )
        self.assertIsNone(canonical_https_prefix("https://user:pw@api.vendor.test/v1/"))

    def test_complete_path_segment_uri_template_compiles_literal_prefix(self) -> None:
        template = "https://api.vendor.test/v2/studies/{nct_id}"

        self.assertEqual(
            "https://api.vendor.test/v2/studies/",
            canonical_https_prefix(template),
        )
        self.assertEqual(
            ("https://api.vendor.test/v2/studies/",),
            extract_literal_https_prefixes([
                f"Fetch one record with GET `{template}`."
            ]),
        )

    def test_uri_template_rejects_malformed_or_non_segment_braces(self) -> None:
        malformed = (
            "https://api.vendor.test/v2/studies/{nct_id",
            "https://api.vendor.test/v2/studies/nct_id}",
            "https://api.vendor.test/v2/studies/{nct-id}",
            "https://api.vendor.test/v2/studies/prefix-{nct_id}",
            "https://api.vendor.test/v2/studies/{nct_id}.json",
            "https://api.vendor.test/v2/studies/{nct_id}?view={format}",
            "https://api.vendor.test/v2/studies/{nct_id}/../admin",
            "https://api.vendor.test/v2/studies/{nct_id}//admin",
            "https://api.vendor.test/v2/studies/{nct_id}/%2e%2e/admin",
            "https://{tenant}.vendor.test/v2/studies/id",
        )
        for value in malformed:
            with self.subTest(value=value):
                self.assertIsNone(canonical_https_prefix(value))
                self.assertIsNone(canonical_https_request_url(value))
                self.assertEqual((), extract_literal_https_prefixes([value]))

        # Runtime request canonicalization never treats template syntax as a
        # concrete path, even when the template itself is a valid grant source.
        self.assertIsNone(canonical_https_request_url(
            "https://api.vendor.test/v2/studies/{nct_id}"
        ))

    def test_loaded_package_scans_only_literal_reference_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SKILL.md").write_text("https://main.vendor.test/api/", encoding="utf-8")
            (root / "references").mkdir()
            (root / "references" / "api.md").write_text(
                "https://support.vendor.test/v2/search\n"
                "See references/nested.md for its response schema.",
                encoding="utf-8",
            )
            (root / "references" / "nested.md").write_text(
                "GET https://nested.vendor.test/v3/lookup",
                encoding="utf-8",
            )
            (root / "references" / "hidden.md").write_text(
                "https://hidden.vendor.test/", encoding="utf-8"
            )
            loaded = {
                "_chatds_scope": "session",
                "content": (
                    "GET https://main.vendor.test/api/. "
                    "Read references/api.md before calling it."
                ),
                "resource_graph": {"skill_root": str(root)},
            }

            grants = compile_loaded_skill_http_grants(
                "rest-helper",
                loaded,
                [
                    "references/hidden.md",
                    "references/nested.md",
                    "references/api.md",
                ],
            )

        self.assertEqual(
            {
                ("rest-helper", "https://main.vendor.test/api/"),
                ("rest-helper", "https://support.vendor.test/v2/search"),
                ("rest-helper", "https://nested.vendor.test/v3/lookup"),
            },
            set(grants),
        )
        self.assertNotIn(
            ("rest-helper", "https://hidden.vendor.test/"),
            grants,
        )

    def test_inventory_entry_without_literal_main_reference_mints_no_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            references = root / "references"
            references.mkdir()
            (references / "incidental.md").write_text(
                "Citation: https://incidental.vendor.test/private/",
                encoding="utf-8",
            )
            loaded = {
                "_chatds_scope": "session",
                "content": "This Skill has no external API endpoint.",
                "resource_graph": {"skill_root": str(root)},
            }

            grants = compile_loaded_skill_http_grants(
                "rest-helper",
                loaded,
                ["references/incidental.md"],
            )

        self.assertEqual((), grants)

    def test_catalog_description_cannot_seed_network_authority(self) -> None:
        loaded = {
            "_chatds_scope": "session",
            "content": "No external endpoint is declared by this Skill.",
            "description": "UI summary https://incidental.vendor.test/api/",
        }

        self.assertEqual(
            (),
            compile_loaded_skill_http_grants("rest-helper", loaded),
        )

    def test_request_and_grant_share_strict_path_canonicalization(self) -> None:
        unsafe = (
            "https://api.vendor.test/v1/%5c..%5cadmin",
            "https://api.vendor.test/v1/%255c..%255cadmin",
            "https://api.vendor.test/v1/%252e%252e%252fadmin",
            "https://api.vendor.test/v1/%2fadmin",
            "https://api.vendor.test/v1/bad%escape",
            "https://api.vendor.test/v1/a b",
            "https://127.0.0.1/v1/search",
            "https://api.vendor.test/v1/search?access%255Ftoken=secret",
            "https://api.vendor.test/v1/search?bad%=value",
            "https://api.vendor.test/v1/search?safe=1;api_key=secret",
        )
        for value in unsafe:
            with self.subTest(value=value):
                self.assertIsNone(canonical_https_request_url(value))
                self.assertIsNone(canonical_https_prefix(value))

        self.assertEqual(
            "https://[2606:4700:4700::1111]/dns-query",
            canonical_https_prefix(
                "https://[2606:4700:4700::1111]/dns-query"
            ),
        )

    def test_non_session_package_never_mints_http_authority(self) -> None:
        self.assertEqual(
            (),
            compile_loaded_skill_http_grants(
                "global-helper",
                {"_chatds_scope": "global", "content": "https://api.vendor.test/"},
            ),
        )

    def test_skill_execution_and_child_surfaces_require_compiled_http_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipeline_root = Path(tmp) / "pipeline"
            pipeline_root.mkdir()
            pipeline_skill_md = pipeline_root / "SKILL.md"
            pipeline_skill_md.write_text(
                "Execute the declared pipeline.",
                encoding="utf-8",
            )
            rest_root = Path(tmp) / "rest-helper"
            rest_root.mkdir()
            rest_skill_md = rest_root / "SKILL.md"
            rest_skill_md.write_text(
                "Use GET https://api.vendor.test/v1/ for searches.",
                encoding="utf-8",
            )
            worker = {
                "id": "collector",
                "file": "workers/collector.yaml",
                "skills": ["rest-helper"],
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
            exposure = _bounded_skill_execution_exposure(
                "use pipeline to produce report",
                ["skills_list", "skill_view", "delegate_task", "skill_http_get"],
                {"pipeline", "rest-helper"},
                {
                    "pipeline": {
                        "name": "pipeline",
                        "_chatds_scope": "session",
                        "workflow_contract": workflow,
                        "skill_dir": str(pipeline_root),
                        "skill_md_sha256": hashlib.sha256(
                            pipeline_skill_md.read_bytes()
                        ).hexdigest(),
                    },
                    "rest-helper": {
                        "name": "rest-helper",
                        "_chatds_scope": "session",
                        "content": "Use GET https://api.vendor.test/v1/ for searches.",
                        "resource_graph": {"skill_root": str(rest_root)},
                        "workflow_contract": None,
                        "skill_dir": str(rest_root),
                        "skill_md_sha256": hashlib.sha256(
                            rest_skill_md.read_bytes()
                        ).hexdigest(),
                    },
                },
                {},
                selected_skill_names=("pipeline",),
            )

        self.assertIn("skill_http_get", exposure.tools)
        self.assertEqual(
            (("rest-helper", "https://api.vendor.test/v1/"),),
            exposure.allowed_skill_http_prefixes,
        )
        child = _declared_child_tools(
            exposure.tools,
            {"skills": ["rest-helper"]},
            http_capability_skills=["rest-helper"],
        )
        self.assertIn("skill_http_get", child)
        self.assertNotIn(
            "skill_http_get",
            _declared_child_tools(
                exposure.tools,
                {"skills": ["rest-helper"]},
                http_capability_skills=[],
            ),
        )

    def test_registered_bridge_does_not_override_explicit_tool_omission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rest_root = Path(tmp) / "rest-helper"
            rest_root.mkdir()
            loaded = {
                "rest-helper": {
                    "name": "rest-helper",
                    "_chatds_scope": "session",
                    "content": "Use GET https://api.vendor.test/v1/search.",
                    "resource_graph": {"skill_root": str(rest_root)},
                    "workflow_contract": None,
                },
            }
            exposure = _bounded_skill_execution_exposure(
                "execute rest-helper",
                ["skills_list", "skill_view"],
                {"rest-helper"},
                loaded,
                {},
                selected_skill_names=("rest-helper",),
            )

        self.assertNotIn("skill_http_get", exposure.tools)
        self.assertEqual((), exposure.allowed_skill_http_prefixes)


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
        body: bytes = b'{"ok":true}',
        headers=None,
        enter_delay: float = 0,
    ):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json; charset=utf-8"}
        self.content = _FakeContent([body])
        self.charset = "utf-8"
        self.enter_delay = enter_delay

    async def __aenter__(self):
        if self.enter_delay:
            await asyncio.sleep(self.enter_delay)
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    response = _FakeResponse()
    responses: list[_FakeResponse] = []
    calls: list[str] = []

    def __init__(self, **kwargs):
        self.connector = kwargs.get("connector")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self.connector is not None:
            closed = self.connector.close()
            if inspect.isawaitable(closed):
                await closed
        return False

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        if self.responses:
            return self.responses.pop(0)
        return self.response


class SkillHttpToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        with skill_http._request_count_lock:
            skill_http._request_counts.clear()
            skill_http._root_request_counts.clear()
            skill_http._user_request_windows.clear()
            skill_http._active_by_host.clear()
            skill_http._active_by_root.clear()
            skill_http._active_by_user.clear()
            skill_http._active_request_total = 0
        _FakeSession.calls.clear()
        _FakeSession.responses.clear()
        _FakeSession.response = _FakeResponse()
        self.context = ToolContext(
            user_id="u",
            session_id="s",
            run_id="run-1",
            root_run_id="root-1",
            allowed_skill_http_prefixes=(("rest-helper", "https://api.vendor.test/v1/"),),
        )

    async def test_missing_or_outside_grant_fails_before_request(self) -> None:
        missing = json.loads(await skill_http.skill_http_get(
            "https://api.vendor.test/v1/search", context=ToolContext()
        ))
        outside = json.loads(await skill_http.skill_http_get(
            "https://other.vendor.test/v1/search", context=self.context
        ))
        secret = json.loads(await skill_http.skill_http_get(
            "https://api.vendor.test/v1/search?x-api-key=secret", context=self.context
        ))

        self.assertFalse(missing["request_sent"])
        self.assertEqual("skill_http_boundary_violation", outside["error_code"])
        self.assertEqual("invalid_url", secret["error_code"])

        for query in (
            "client_secret=value",
            "Authorization=BearerValue",
            "access%5Ftoken=value",
            "access%255Ftoken=value",
            "bad%=value",
            "safe=1;api_key=value",
        ):
            with self.subTest(query=query):
                blocked = json.loads(await skill_http.skill_http_get(
                    f"https://api.vendor.test/v1/search?{query}",
                    context=self.context,
                ))
                self.assertEqual("invalid_url", blocked["error_code"])
                self.assertFalse(blocked["request_sent"])

    async def test_uri_template_grant_preserves_host_and_segment_boundary(self) -> None:
        prefix = canonical_https_prefix(
            "https://api.vendor.test/v2/studies/{nct_id}"
        )
        self.assertEqual("https://api.vendor.test/v2/studies/", prefix)
        context = ToolContext(
            user_id="u",
            session_id="s",
            run_id="template-run",
            root_run_id="template-root",
            allowed_skill_http_prefixes=(("rest-helper", prefix),),
        )

        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
        ):
            allowed = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v2/studies/NCT01234567",
                context=context,
            ))
        adjacent = json.loads(await skill_http.skill_http_get(
            "https://api.vendor.test/v2/study/NCT01234567",
            context=context,
        ))
        other_host = json.loads(await skill_http.skill_http_get(
            "https://other.vendor.test/v2/studies/NCT01234567",
            context=context,
        ))

        self.assertEqual("success", allowed["status"])
        self.assertEqual(prefix, allowed["matched_prefix"])
        for blocked in (adjacent, other_host):
            self.assertEqual("skill_http_boundary_violation", blocked["error_code"])
            self.assertFalse(blocked["request_sent"])

    async def test_authorized_get_returns_auditable_bounded_receipt(self) -> None:
        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search?q=target",
                max_chars=100,
                context=self.context,
            ))

        self.assertEqual("success", result["status"])
        self.assertTrue(result["request_sent"])
        self.assertEqual("rest-helper", result["matched_skill"])
        self.assertEqual("https://api.vendor.test/v1/", result["matched_prefix"])
        self.assertEqual(
            hashlib.sha256(
                b"https://api.vendor.test/v1/"
            ).hexdigest(),
            result["matched_prefix_sha256"],
        )
        self.assertEqual('{"ok":true}', result["body"])
        self.assertEqual(1, result["request_number"])
        self.assertEqual(1, result["root_request_number"])
        self.assertEqual("complete", result["retrieval"]["state"])
        self.assertEqual(
            skill_http.DEFAULT_MAX_REQUESTS_PER_RUN,
            result["retrieval"]["request_run_hop_limit"],
        )

    async def test_get_retries_one_transient_dns_failure_within_same_budget(self) -> None:
        resolver = AsyncMock(side_effect=[
            socket.gaierror(-5, "temporary resolver failure"),
            (("203.0.113.10", 2),),
        ])
        with (
            patch.object(skill_http, "_public_addresses", resolver),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search?q=retry",
                timeout=5,
                context=self.context,
            ))

        self.assertEqual("success", result["status"])
        self.assertEqual(2, resolver.await_count)
        self.assertEqual(2, result["request_number"])
        self.assertEqual(2, result["root_request_number"])
        self.assertEqual(1, result["transport_retry_count"])

    async def test_get_transport_retry_has_one_owner_and_one_attempt(self) -> None:
        resolver = AsyncMock(
            side_effect=socket.gaierror(-5, "persistent resolver failure")
        )
        with patch.object(skill_http, "_public_addresses", resolver):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search?q=bounded",
                timeout=5,
                context=self.context,
            ))

        self.assertEqual("skill_http_transport_error", result["error_code"])
        self.assertEqual(2, resolver.await_count)
        self.assertEqual(2, result["request_number"])
        self.assertEqual(1, result["transport_retry_count"])

    async def test_visible_truncation_scans_complete_wire_and_builds_repage(self) -> None:
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": "A",
        }).encode("utf-8")
        _FakeSession.response = _FakeResponse(body=full)
        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
            patch.object(skill_http, "MAX_CHARS", 100),
            patch.object(
                skill_http,
                "persist_tool_result_spill",
                return_value=None,
            ),
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search?q=x&pageSize=50",
                max_chars=100,
                context=self.context,
            ))

        receipt = result["retrieval"]
        self.assertTrue(result["body_truncated"])
        self.assertTrue(receipt["wire_body_complete"])
        self.assertFalse(receipt["visible_body_complete"])
        self.assertEqual(
            "complete_wire_body", receipt["pagination"]["scan_source"]
        )
        self.assertEqual(
            "A", receipt["pagination"]["next_hints"][0]["value"]
        )
        evidence = receipt["collection_evidence"]
        self.assertEqual("observed", evidence["status"])
        self.assertEqual(
            1, evidence["primary_collection"]["observed_items"]
        )
        self.assertEqual("$/items", evidence["primary_collection"]["path"])
        action = receipt["continuation_action"]
        self.assertEqual("restart_with_smaller_page", action["kind"])
        self.assertIn("pageSize=", action["args"]["url"])
        self.assertNotIn("pageSize=50", action["args"]["url"])

    async def test_complete_large_wire_body_is_losslessly_spilled(self) -> None:
        full = json.dumps({
            "items": [{"text": "x" * 300}],
        }).encode("utf-8")
        _FakeSession.response = _FakeResponse(body=full)
        handle = "tool-result:complete-http-body.txt"
        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
            patch.object(skill_http, "MAX_CHARS", 100),
            patch.object(
                skill_http,
                "persist_tool_result_spill",
                return_value=handle,
            ) as persist,
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search?q=x",
                max_chars=100,
                context=self.context,
            ))

        receipt = result["retrieval"]
        self.assertTrue(result["body_truncated"])
        self.assertTrue(result["body_spilled_complete"])
        self.assertEqual(handle, result["body_result_handle"])
        self.assertEqual("complete", receipt["state"])
        self.assertEqual([], receipt["incomplete_reasons"])
        self.assertTrue(receipt["body_retrievable_complete"])
        self.assertIsNone(receipt["continuation_action"])
        persist.assert_called_once_with(
            full.decode("utf-8"),
            "skill_http_get_body",
            user_id="u",
            session_id="s",
        )

    async def test_wire_byte_truncation_is_not_parsed_and_reduces_page_size(self) -> None:
        full = json.dumps({
            "items": [{"text": "x" * 300}],
            "nextPageToken": "A",
        }).encode("utf-8")
        _FakeSession.response = _FakeResponse(body=full)
        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
            patch.object(skill_http, "MAX_RESPONSE_BYTES", 64),
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search?q=x&pageSize=50",
                max_chars=100,
                context=self.context,
            ))

        receipt = result["retrieval"]
        self.assertFalse(receipt["wire_body_complete"])
        self.assertEqual(
            "none_partial_wire", receipt["pagination"]["scan_source"]
        )
        self.assertFalse(receipt["pagination"]["detected"])
        self.assertEqual(
            "restart_with_smaller_page",
            receipt["continuation_action"]["kind"],
        )

    async def test_configured_default_allows_ninth_hop_and_rejects_n_plus_one(self) -> None:
        run_identity, _root_identity, _user_identity = (
            skill_http._quota_identities(self.context)
        )
        with patch.object(
            skill_http.settings,
            "skill_http_max_requests_per_run",
            16,
        ):
            skill_http._request_counts[run_identity] = 8
            with (
                patch.object(
                    skill_http,
                    "_public_addresses",
                    AsyncMock(return_value=(("203.0.113.10", 2),)),
                ),
                patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
            ):
                ninth = json.loads(await skill_http.skill_http_get(
                    "https://api.vendor.test/v1/search?page=9",
                    context=self.context,
                ))
            self.assertEqual("success", ninth["status"])
            self.assertEqual(9, ninth["request_number"])

            skill_http._request_counts[run_identity] = 16
            blocked = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search?page=17",
                context=self.context,
            ))
            self.assertEqual(
                "skill_http_request_limit", blocked["error_code"]
            )
            self.assertEqual(17, blocked["request_number"])
            self.assertFalse(blocked["request_sent"])

    async def test_request_quota_is_per_runtime_owned_run(self) -> None:
        run_identity, _root_identity, _user_identity = (
            skill_http._quota_identities(self.context)
        )
        skill_http._request_counts[run_identity] = (
            skill_http.MAX_REQUESTS_PER_RUN
        )
        result = json.loads(await skill_http.skill_http_get(
            "https://api.vendor.test/v1/search", context=self.context
        ))

        self.assertEqual("skill_http_request_limit", result["error_code"])
        self.assertFalse(result["request_sent"])

    async def test_redirect_consumes_another_actual_hop_quota(self) -> None:
        run_identity, _root_identity, _user_identity = (
            skill_http._quota_identities(self.context)
        )
        skill_http._request_counts[run_identity] = (
            skill_http.MAX_REQUESTS_PER_RUN - 1
        )
        _FakeSession.responses[:] = [
            _FakeResponse(
                status=302,
                headers={"Location": "/v1/next"},
            ),
            _FakeResponse(),
        ]
        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search",
                context=self.context,
            ))

        self.assertEqual("skill_http_request_limit", result["error_code"])
        self.assertTrue(result["request_sent"])
        self.assertEqual(
            skill_http.MAX_REQUESTS_PER_RUN + 1,
            result["request_number"],
        )
        self.assertEqual(1, len(_FakeSession.calls))

    async def test_safe_redirect_stays_in_grant_and_reports_two_hops(self) -> None:
        _FakeSession.responses[:] = [
            _FakeResponse(
                status=302,
                headers={"Location": "/v1/next"},
            ),
            _FakeResponse(body=b'{"page":2}'),
        ]
        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search",
                context=self.context,
            ))

        self.assertEqual("success", result["status"])
        self.assertEqual(2, result["request_number"])
        self.assertEqual(2, result["root_request_number"])
        self.assertEqual(1, result["redirects_followed"])
        self.assertEqual(2, len(_FakeSession.calls))

    async def test_dns_lookup_obeys_the_total_deadline(self) -> None:
        async def slow_dns(_hostname: str):
            await asyncio.sleep(2)
            return (("203.0.113.10", 2),)

        started = asyncio.get_running_loop().time()
        with patch.object(skill_http, "_public_addresses", slow_dns):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search",
                timeout=1,
                context=self.context,
            ))
        elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual("skill_http_timeout", result["error_code"])
        self.assertFalse(result["request_sent"])
        self.assertEqual("rest-helper", result["matched_skill"])
        self.assertEqual(
            hashlib.sha256(
                b"https://api.vendor.test/v1/"
            ).hexdigest(),
            result["matched_prefix_sha256"],
        )
        self.assertLess(elapsed, 1.5)

    async def test_redirects_do_not_reset_the_total_deadline(self) -> None:
        _FakeSession.responses[:] = [
            _FakeResponse(
                status=302,
                headers={"Location": "/v1/next"},
                enter_delay=0.65,
            ),
            _FakeResponse(enter_delay=0.65),
        ]
        started = asyncio.get_running_loop().time()
        with (
            patch.object(
                skill_http,
                "_public_addresses",
                AsyncMock(return_value=(("203.0.113.10", 2),)),
            ),
            patch.object(skill_http.aiohttp, "ClientSession", _FakeSession),
        ):
            result = json.loads(await skill_http.skill_http_get(
                "https://api.vendor.test/v1/search",
                timeout=1,
                context=self.context,
            ))
        elapsed = asyncio.get_running_loop().time() - started

        self.assertEqual("skill_http_timeout", result["error_code"])
        self.assertTrue(result["request_sent"])
        self.assertEqual("rest-helper", result["matched_skill"])
        self.assertEqual(
            hashlib.sha256(
                b"https://api.vendor.test/v1/"
            ).hexdigest(),
            result["matched_prefix_sha256"],
        )
        self.assertEqual(1, result["redirects_followed"])
        self.assertEqual(2, result["request_number"])
        self.assertLess(elapsed, 1.5)

    async def test_dns_rejects_mixed_or_transition_addresses(self) -> None:
        records = [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (10, 1, 6, "", ("64:ff9b::7f00:1", 443, 0, 0)),
        ]
        with patch.object(skill_http.socket, "getaddrinfo", return_value=records):
            with self.assertRaisesRegex(ValueError, "non-public"):
                await skill_http._public_addresses("api.vendor.test")

    async def test_cancelled_dns_keeps_capacity_until_blocking_lookup_exits(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        local_slots = threading.BoundedSemaphore(1)

        def blocking_lookup(*_args, **_kwargs):
            started.set()
            try:
                release.wait(2)
                return [(2, 1, 6, "", ("8.8.8.8", 443))]
            finally:
                finished.set()

        with (
            patch.object(skill_http, "_DNS_SLOTS", local_slots),
            patch.object(skill_http.socket, "getaddrinfo", blocking_lookup),
        ):
            lookup = asyncio.create_task(
                skill_http._public_addresses("api.vendor.test")
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            lookup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await lookup
            with self.assertRaisesRegex(OSError, "DNS capacity"):
                await skill_http._public_addresses("other.vendor.test")

            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))
            capacity_released = False
            for _ in range(50):
                if local_slots.acquire(blocking=False):
                    capacity_released = True
                    local_slots.release()
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(capacity_released)

    async def test_host_root_user_and_global_concurrency_fail_closed(self) -> None:
        def context(run: str, root: str, user: str) -> ToolContext:
            return ToolContext(
                user_id=user,
                session_id="s",
                run_id=run,
                root_run_id=root,
            )

        dimensions = (
            (
                "MAX_CONCURRENT_PER_HOST",
                context("r1", "root1", "u1"),
                "api.vendor.test",
                context("r2", "root2", "u2"),
                "api.vendor.test",
                "skill_http_host_concurrency_limit",
            ),
            (
                "MAX_CONCURRENT_PER_ROOT_RUN",
                context("r1", "root1", "u1"),
                "a.vendor.test",
                context("r2", "root1", "u1"),
                "b.vendor.test",
                "skill_http_root_concurrency_limit",
            ),
            (
                "MAX_CONCURRENT_PER_USER",
                context("r1", "root1", "u1"),
                "a.vendor.test",
                context("r2", "root2", "u1"),
                "b.vendor.test",
                "skill_http_user_concurrency_limit",
            ),
            (
                "MAX_CONCURRENT_REQUESTS",
                context("r1", "root1", "u1"),
                "a.vendor.test",
                context("r2", "root2", "u2"),
                "b.vendor.test",
                "skill_http_global_concurrency_limit",
            ),
        )
        for constant, first_context, first_host, second_context, second_host, code in dimensions:
            with self.subTest(constant=constant), patch.object(
                skill_http,
                constant,
                1,
            ):
                first, first_error = skill_http._acquire_request_slot(
                    first_context,
                    first_host,
                    now=1.0,
                )
                self.assertIsNotNone(first)
                self.assertIsNone(first_error)
                second, second_error = skill_http._acquire_request_slot(
                    second_context,
                    second_host,
                    now=1.0,
                )
                self.assertIsNone(second)
                self.assertEqual(code, second_error["code"])
                skill_http._release_request_slot(first)
                # Isolate cumulative request quotas/windows between dimensions.
                self.setUp()

    async def test_user_window_and_root_run_quota_are_bounded(self) -> None:
        with patch.object(skill_http, "MAX_REQUESTS_PER_USER_WINDOW", 1):
            first, first_error = skill_http._acquire_request_slot(
                self.context,
                "a.vendor.test",
                now=1.0,
            )
            self.assertIsNone(first_error)
            skill_http._release_request_slot(first)
            second_context = ToolContext(
                user_id="u",
                session_id="other",
                run_id="run-2",
                root_run_id="root-2",
            )
            second, second_error = skill_http._acquire_request_slot(
                second_context,
                "b.vendor.test",
                now=2.0,
            )
            self.assertIsNone(second)
            self.assertEqual(
                "skill_http_user_rate_limit",
                second_error["code"],
            )

        self.setUp()
        _run_identity, root_identity, _user_identity = (
            skill_http._quota_identities(self.context)
        )
        skill_http._root_request_counts[root_identity] = (
            skill_http.MAX_REQUESTS_PER_ROOT_RUN
        )
        lease, error = skill_http._acquire_request_slot(
            self.context,
            "api.vendor.test",
            now=1.0,
        )
        self.assertIsNone(lease)
        self.assertEqual("skill_http_root_request_limit", error["code"])


if __name__ == "__main__":
    unittest.main()
