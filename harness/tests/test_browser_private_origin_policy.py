from __future__ import annotations

import asyncio
import ipaddress
import threading
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import tools.browser as browser_tools

from tools.approval import (
    canonical_http_origin,
    check_url_safety,
    compile_user_private_origin_grants,
)
from tools.browser import (
    _bind_browser_policy,
    _browser_session_key,
    _build_snapshot,
    _click_element,
    _context_private_origins,
    _get_session,
    _guard_browser_request,
    _guard_browser_websocket,
    _type_text,
)
from tools.context import ToolContext


PRIVATE_ORIGIN = "http://172.30.100.145:5173"


class BrowserPrivateOriginApprovalTests(unittest.TestCase):
    def test_opt_in_synthetic_dns_allows_only_named_public_targets(self):
        synthetic_answers = (
            ipaddress.ip_address("198.18.0.71"),
            ipaddress.ip_address("fc00::3a"),
        )
        with patch(
            "tools.approval._resolved_ip_addresses",
            return_value=synthetic_answers,
        ):
            self.assertIsNotNone(check_url_safety("https://public.example"))
            self.assertIsNone(check_url_safety(
                "https://public.example/path",
                synthetic_public_ranges="198.18.0.0/15,fc00::/18",
            ))
            # Arbitrary private ranges cannot be smuggled into the synthetic
            # carrier configuration.
            self.assertIsNotNone(check_url_safety(
                "https://public.example",
                synthetic_public_ranges="10.0.0.0/8",
            ))

        # Literal carrier addresses remain non-routable even when configured;
        # the exemption belongs only to DNS names handled by the proxy.
        self.assertIsNotNone(check_url_safety(
            "http://198.18.0.71/",
            synthetic_public_ranges="198.18.0.0/15",
        ))

    def test_grant_is_exact_config_and_explicit_user_url_intersection(self):
        user_text = (
            "请使用 skill 访问 "
            f"{PRIVATE_ORIGIN}/chat/2624d90b8eb447568ca4910dd3d40c99，"
            "说明页面内容"
        )
        self.assertEqual(
            (PRIVATE_ORIGIN,),
            compile_user_private_origin_grants(
                user_text,
                f"{PRIVATE_ORIGIN},http://172.30.100.146:5173",
            ),
        )
        self.assertEqual(
            (),
            compile_user_private_origin_grants(
                user_text,
                "http://172.30.100.145:5174",
            ),
        )
        self.assertEqual(
            (),
            compile_user_private_origin_grants(
                "请访问 172.30.100.145:5173",
                PRIVATE_ORIGIN,
            ),
        )

    def test_grant_is_origin_exact_and_config_cannot_include_a_path(self):
        self.assertEqual(
            (),
            compile_user_private_origin_grants(
                f"访问 {PRIVATE_ORIGIN}/chat/abc",
                f"{PRIVATE_ORIGIN}/admin",
            ),
        )
        self.assertEqual(
            (),
            compile_user_private_origin_grants(
                "访问 http://172.30.100.145:5174/chat/abc",
                PRIVATE_ORIGIN,
            ),
        )
        self.assertEqual(
            "http://172.30.100.145",
            canonical_http_origin("HTTP://172.30.100.145:80/path?q=1"),
        )

    def test_exact_private_origin_is_allowed_but_other_private_origin_is_not(self):
        self.assertIsNone(check_url_safety(
            f"{PRIVATE_ORIGIN}/chat/abc",
            allowed_private_origins=(PRIVATE_ORIGIN,),
        ))
        self.assertIn(
            "private/internal",
            check_url_safety(
                "http://172.30.100.145:5174/chat/abc",
                allowed_private_origins=(PRIVATE_ORIGIN,),
            ) or "",
        )

    def test_never_grants_credentials_metadata_loopback_or_link_local(self):
        rejected = (
            "http://user:secret@172.30.100.145:5173",
            "http://169.254.169.254",
            "http://127.0.0.1:5173",
            "http://169.254.3.4:5173",
        )
        for origin in rejected:
            with self.subTest(origin=origin):
                self.assertEqual(
                    (),
                    compile_user_private_origin_grants(
                        f"访问 {origin}/path",
                        origin,
                    ),
                )
                self.assertIsNotNone(check_url_safety(
                    origin,
                    allowed_private_origins=(origin,),
                ))

    def test_special_purpose_and_site_local_addresses_fail_closed(self):
        rejected = (
            "http://0.1.2.3/",       # IPv4 this-network space
            "http://192.0.2.1/",     # TEST-NET-1 documentation space
            "http://[2001:db8::1]/", # IPv6 documentation space
            "http://[fec0::1]/",     # deprecated IPv6 site-local space
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertIsNotNone(check_url_safety(url))

    def test_every_address_in_a_mixed_dns_answer_must_be_global(self):
        with patch(
            "tools.approval._resolved_ip_addresses",
            return_value=(
                ipaddress.ip_address("93.184.216.34"),
                ipaddress.ip_address("192.0.2.1"),
            ),
        ):
            self.assertIsNotNone(check_url_safety("https://public.example"))

    def test_non_http_schemes_are_always_blocked(self):
        for url in (
            "file:///etc/passwd",
            "ftp://172.30.100.145/resource",
            "javascript:alert(1)",
            "data:text/plain,hello",
        ):
            with self.subTest(url=url):
                self.assertIn(
                    "http(s)",
                    check_url_safety(
                        url,
                        allowed_private_origins=(PRIVATE_ORIGIN,),
                    ) or "",
                )

    def test_delegate_context_cannot_carry_private_origin_grant(self):
        self.assertEqual(
            (PRIVATE_ORIGIN,),
            _context_private_origins(ToolContext(
                agent_kind="primary",
                source="chat",
                allowed_browser_private_origins=(PRIVATE_ORIGIN,),
            )),
        )
        self.assertEqual(
            (),
            _context_private_origins(ToolContext(
                agent_kind="delegate",
                source="delegate",
                allowed_browser_private_origins=(PRIVATE_ORIGIN,),
            )),
        )

    def test_reused_session_replaces_policy_and_run_id_instead_of_merging(self):
        context = ToolContext(
            user_id="user-a",
            session_id="session-a",
            run_id="new-run",
            agent_kind="primary",
            source="chat",
            allowed_browser_private_origins=(PRIVATE_ORIGIN,),
        )
        state_key = _browser_session_key("model-controlled", context)
        session = {
            "state_key": state_key,
            "allowed_private_origins": ("http://172.30.100.146:5173",),
            "policy_run_id": "old-run",
            "last_blocked_request": {"error": "old"},
        }
        allowed = _bind_browser_policy(session, context)
        self.assertEqual((PRIVATE_ORIGIN,), allowed)
        self.assertEqual((PRIVATE_ORIGIN,), session["allowed_private_origins"])
        self.assertEqual("new-run", session["policy_run_id"])
        self.assertIsNone(session["last_blocked_request"])

        with self.assertRaisesRegex(RuntimeError, "does not own"):
            _bind_browser_policy(session, ToolContext(
                user_id="user-a",
                session_id="session-a",
                run_id="delegate-run",
                agent_kind="delegate",
                source="delegate",
                allowed_browser_private_origins=(PRIVATE_ORIGIN,),
            ))

    def test_runtime_key_ignores_argument_and_isolates_user_session_and_run(self):
        base = ToolContext(
            user_id="user-a",
            session_id="runtime-session",
            run_id="run-a",
        )
        self.assertEqual(
            ("user-a", "runtime-session", "run-a"),
            _browser_session_key("model-controlled-session", base),
        )
        self.assertEqual(
            _browser_session_key("ignored-one", base),
            _browser_session_key(
                "ignored-two",
                ToolContext(
                    user_id="user-a",
                    session_id="runtime-session",
                    run_id="run-a",
                ),
            ),
        )
        self.assertNotEqual(
            _browser_session_key("ignored", base),
            _browser_session_key("ignored", ToolContext(
                user_id="user-a",
                session_id="runtime-session",
                run_id="run-b",
            )),
        )
        self.assertNotEqual(
            _browser_session_key("ignored", base),
            _browser_session_key("ignored", ToolContext(
                user_id="user-b",
                session_id="runtime-session",
                run_id="run-a",
            )),
        )

    def test_implicit_runtime_scope_survives_context_replacement(self):
        first = ToolContext(user_id="u", session_id="s")
        narrowed = replace(first, enabled_tools=("browser_navigate",))
        independent = ToolContext(user_id="u", session_id="s")
        self.assertEqual(
            _browser_session_key("ignored", first),
            _browser_session_key("ignored", narrowed),
        )
        self.assertNotEqual(
            _browser_session_key("ignored", first),
            _browser_session_key("ignored", independent),
        )

    def test_contextless_fallback_is_separate_and_has_no_private_grant(self):
        self.assertEqual(
            (
                "__direct_contextless_browser__",
                "legacy-session",
                "__direct_contextless_run__",
            ),
            _browser_session_key("legacy-session", None),
        )
        self.assertEqual((), _context_private_origins(None))


class _FakeRoute:
    def __init__(self):
        self.continued = False
        self.aborted_with = None

    async def continue_(self):
        self.continued = True

    async def abort(self, reason):
        self.aborted_with = reason


class _FakeRequest:
    def __init__(self, url: str, *, navigation: bool = True):
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class _FakeWebSocketRoute:
    def __init__(self):
        self.close_args = None

    async def close(self, **kwargs):
        self.close_args = kwargs


class _FakePage:
    url = "about:blank"


class _FakeBrowserContext:
    def __init__(self):
        self.actions = []
        self.init_script = None
        self.request_handler = None
        self.websocket_handler = None
        self.closed = False

    async def add_init_script(self, *, script):
        self.actions.append(("add_init_script", None))
        self.init_script = script

    async def route(self, pattern, handler):
        self.actions.append(("route", pattern))
        self.request_handler = handler

    async def route_web_socket(self, pattern, handler):
        self.actions.append(("route_web_socket", pattern))
        self.websocket_handler = handler

    async def new_page(self):
        self.actions.append(("new_page", None))
        return _FakePage()

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.new_context_kwargs = None

    async def new_context(self, **kwargs):
        self.new_context_kwargs = kwargs
        return self.context


class _FakeBrowserFactory:
    def __init__(self):
        self.contexts = []

    async def new_context(self, **kwargs):
        context = _FakeBrowserContext()
        context.new_context_kwargs = kwargs
        self.contexts.append(context)
        return context


class BrowserRequestGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_routes_cover_future_popup_before_page_creation(self):
        fake_context = _FakeBrowserContext()
        fake_browser = _FakeBrowser(fake_context)
        runtime_context = ToolContext(
            user_id="popup-user",
            session_id="context-popup-policy-test",
            run_id="popup-run",
            allowed_browser_private_origins=(PRIVATE_ORIGIN,),
        )
        session_key = _browser_session_key("model-session", runtime_context)
        browser_tools._sessions.pop(session_key, None)
        browser_tools._last_activity.pop(session_key, None)
        self.addAsyncCleanup(browser_tools.cleanup_session, session_key)

        with patch(
            "tools.browser._get_browser",
            AsyncMock(return_value=fake_browser),
        ):
            session = await _get_session(session_key)

        self.assertEqual(
            [
                ("add_init_script", None),
                ("route", "**/*"),
                ("route_web_socket", "**/*"),
                ("new_page", None),
            ],
            fake_context.actions,
        )
        self.assertFalse(fake_browser.new_context_kwargs["accept_downloads"])
        self.assertEqual(
            "block", fake_browser.new_context_kwargs["service_workers"]
        )
        self.assertIsNotNone(fake_context.request_handler)
        self.assertIsNotNone(fake_context.websocket_handler)
        self.assertIn("RTCPeerConnection", fake_context.init_script)
        self.assertIn("RTCDataChannel", fake_context.init_script)
        self.assertIn("WebTransport", fake_context.init_script)
        self.assertIn("configurable: false", fake_context.init_script)

        _bind_browser_policy(session, runtime_context)
        popup_route = _FakeRoute()
        await fake_context.request_handler(
            popup_route,
            _FakeRequest("http://172.30.100.146:5173/popup"),
        )
        self.assertEqual("blockedbyclient", popup_route.aborted_with)

        popup_socket = _FakeWebSocketRoute()
        await fake_context.websocket_handler(popup_socket)
        self.assertEqual(1008, popup_socket.close_args["code"])

    async def test_concurrent_runs_get_distinct_contexts_and_immutable_owners(self):
        origin_b = "http://172.30.100.146:5173"
        context_a = ToolContext(
            user_id="same-user",
            session_id="same-session",
            run_id="run-a",
            allowed_browser_private_origins=(PRIVATE_ORIGIN,),
        )
        context_b = ToolContext(
            user_id="same-user",
            session_id="same-session",
            run_id="run-b",
            allowed_browser_private_origins=(origin_b,),
        )
        key_a = _browser_session_key("forged", context_a)
        key_b = _browser_session_key("forged", context_b)
        factory = _FakeBrowserFactory()
        for key in (key_a, key_b):
            browser_tools._sessions.pop(key, None)
            browser_tools._last_activity.pop(key, None)
            self.addAsyncCleanup(browser_tools.cleanup_session, key)

        with patch(
            "tools.browser._get_browser",
            AsyncMock(return_value=factory),
        ):
            session_a, session_b, session_a_again = await asyncio.gather(
                _get_session(key_a),
                _get_session(key_b),
                _get_session(key_a),
            )

        self.assertIs(session_a, session_a_again)
        self.assertIsNot(session_a, session_b)
        self.assertEqual(2, len(factory.contexts))
        self.assertEqual(key_a, session_a["state_key"])
        self.assertEqual(key_b, session_b["state_key"])

        _bind_browser_policy(session_a, context_a)
        _bind_browser_policy(session_b, context_b)
        self.assertEqual(
            (PRIVATE_ORIGIN,), session_a["allowed_private_origins"]
        )
        self.assertEqual((origin_b,), session_b["allowed_private_origins"])

        route_a = _FakeRoute()
        route_b = _FakeRoute()
        await session_a["context"].request_handler(
            route_a,
            _FakeRequest(f"{PRIVATE_ORIGIN}/allowed"),
        )
        await session_b["context"].request_handler(
            route_b,
            _FakeRequest(f"{PRIVATE_ORIGIN}/must-not-cross-run"),
        )
        self.assertTrue(route_a.continued)
        self.assertEqual("blockedbyclient", route_b.aborted_with)

    async def test_initial_allowed_origin_continues(self):
        session = {"allowed_private_origins": (PRIVATE_ORIGIN,)}
        route = _FakeRoute()
        await _guard_browser_request(
            session,
            route,
            _FakeRequest(f"{PRIVATE_ORIGIN}/chat/abc"),
        )
        self.assertTrue(route.continued)
        self.assertIsNone(route.aborted_with)

    async def test_dns_backed_request_guard_runs_off_event_loop_thread(self):
        event_loop_thread = threading.get_ident()
        guard_threads = []

        def check(*_args, **_kwargs):
            guard_threads.append(threading.get_ident())
            return None

        route = _FakeRoute()
        with patch("tools.browser.check_url_safety", side_effect=check):
            await _guard_browser_request(
                {"allowed_private_origins": ()},
                route,
                _FakeRequest("https://example.com"),
            )

        self.assertTrue(route.continued)
        self.assertEqual(1, len(guard_threads))
        self.assertNotEqual(event_loop_thread, guard_threads[0])

    async def test_redirect_to_other_private_origin_aborts_before_dispatch(self):
        session = {"allowed_private_origins": (PRIVATE_ORIGIN,)}
        route = _FakeRoute()
        await _guard_browser_request(
            session,
            route,
            _FakeRequest("http://172.30.100.146:5173/private"),
        )
        self.assertFalse(route.continued)
        self.assertEqual("blockedbyclient", route.aborted_with)
        self.assertTrue(session["last_blocked_request"]["navigation"])
        self.assertIn(
            "private/internal",
            session["last_blocked_request"]["error"],
        )

    async def test_metadata_subresource_is_blocked_even_if_misconfigured(self):
        session = {
            "allowed_private_origins": (
                PRIVATE_ORIGIN,
                "http://169.254.169.254",
            )
        }
        route = _FakeRoute()
        await _guard_browser_request(
            session,
            route,
            _FakeRequest(
                "http://169.254.169.254/latest/meta-data/",
                navigation=False,
            ),
        )
        self.assertEqual("blockedbyclient", route.aborted_with)
        self.assertFalse(session["last_blocked_request"]["navigation"])

    async def test_websocket_is_fail_closed_before_server_connection(self):
        session = {"allowed_private_origins": (PRIVATE_ORIGIN,)}
        route = _FakeWebSocketRoute()
        await _guard_browser_websocket(session, route)
        self.assertEqual(1008, route.close_args["code"])
        self.assertEqual(
            "websocket",
            session["last_blocked_request"]["transport"],
        )
        self.assertIn(
            "HTTP(S) origin policy",
            session["last_blocked_request"]["error"],
        )


class _CloseResource:
    def __init__(self):
        self.close_count = 0

    async def close(self):
        self.close_count += 1


class _StopResource:
    def __init__(self):
        self.stop_count = 0

    async def stop(self):
        self.stop_count += 1


class _BlockingContext(_CloseResource):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def close(self):
        self.started.set()
        await self.release.wait()
        await super().close()


class BrowserRunCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved_sessions = dict(browser_tools._sessions)
        self.saved_activity = dict(browser_tools._last_activity)
        self.saved_transport = (
            browser_tools._browser_instance,
            browser_tools._playwright_instance,
            browser_tools._browser_relay,
        )
        browser_tools._sessions.clear()
        browser_tools._last_activity.clear()
        browser_tools._browser_instance = None
        browser_tools._playwright_instance = None
        browser_tools._browser_relay = None

    async def asyncTearDown(self):
        await browser_tools.close_all_browser_sessions()
        browser_tools._sessions.update(self.saved_sessions)
        browser_tools._last_activity.update(self.saved_activity)
        (
            browser_tools._browser_instance,
            browser_tools._playwright_instance,
            browser_tools._browser_relay,
        ) = self.saved_transport

    async def test_close_browser_run_removes_only_exact_concurrent_run(self):
        context_a = _CloseResource()
        context_b = _CloseResource()
        browser = _CloseResource()
        playwright = _StopResource()
        relay = _CloseResource()
        key_a = ("same-user", "same-session", "run-a")
        key_b = ("same-user", "same-session", "run-b")
        browser_tools._sessions.update({
            key_a: {"context": context_a},
            key_b: {"context": context_b},
        })
        browser_tools._last_activity.update({key_a: 1.0, key_b: 2.0})
        browser_tools._browser_instance = browser
        browser_tools._playwright_instance = playwright
        browser_tools._browser_relay = relay

        await browser_tools.close_browser_run(*key_a)

        self.assertNotIn(key_a, browser_tools._sessions)
        self.assertIn(key_b, browser_tools._sessions)
        self.assertEqual(1, context_a.close_count)
        self.assertEqual(0, context_b.close_count)
        self.assertEqual(0, browser.close_count)
        self.assertEqual(0, playwright.stop_count)
        self.assertEqual(0, relay.close_count)

        await browser_tools.close_browser_run(*key_b)

        self.assertEqual({}, browser_tools._sessions)
        self.assertEqual({}, browser_tools._last_activity)
        self.assertEqual(1, context_b.close_count)
        self.assertEqual(1, browser.close_count)
        self.assertEqual(1, playwright.stop_count)
        self.assertEqual(1, relay.close_count)

    async def test_close_browser_run_finishes_exact_cleanup_before_cancellation(self):
        context = _BlockingContext()
        key = ("cancel-user", "cancel-session", "cancel-run")
        browser_tools._sessions[key] = {"context": context}
        browser_tools._last_activity[key] = 1.0

        close_task = asyncio.create_task(browser_tools.close_browser_run(*key))
        await context.started.wait()
        close_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())
        # Repeated disconnect/shutdown cancellation must not punch through the
        # helper's shield before ownership cleanup completes.
        close_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())

        context.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await close_task

        self.assertEqual(1, context.close_count)
        self.assertNotIn(key, browser_tools._sessions)
        self.assertNotIn(key, browser_tools._last_activity)


class _SnapshotPage:
    def __init__(self):
        self.script = ""
        self.arguments = None

    async def evaluate(self, script, arguments):
        self.script = script
        self.arguments = arguments
        return {
            "url": f"{PRIVATE_ORIGIN}/chat/abc",
            "title": "Example",
            "visibleText": "Rendered page content",
            "controls": [
                {
                    "ref": "e1",
                    "role": "button",
                    "name": "Continue",
                    "value": "",
                    "href": "",
                    "disabled": False,
                    "checked": None,
                },
                {
                    "ref": "e2",
                    "role": "textbox",
                    "name": "Query",
                    "value": "",
                    "href": "",
                    "disabled": False,
                    "checked": None,
                },
            ],
        }


class _FakeLocator:
    def __init__(self, count: int):
        self._count = count
        self.clicked = False
        self.filled = None

    async def count(self):
        return self._count

    async def click(self):
        self.clicked = True

    async def fill(self, value):
        self.filled = value


class _LocatorPage:
    def __init__(self, locator: _FakeLocator):
        self._locator = locator
        self.selector = None

    def locator(self, selector):
        self.selector = selector
        return self._locator


class BrowserDomSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_uses_dom_and_assigns_unique_refs(self):
        page = _SnapshotPage()
        snapshot = await _build_snapshot(page)
        self.assertEqual(1, snapshot.count("[@e1]"))
        self.assertEqual(1, snapshot.count("[@e2]"))
        self.assertIn("Rendered page content", snapshot)
        self.assertIn("querySelectorAll", page.script)
        self.assertNotIn("accessibility.snapshot", page.script)

    async def test_click_and_type_use_exact_snapshot_marker(self):
        click_locator = _FakeLocator(1)
        click_page = _LocatorPage(click_locator)
        self.assertIsNone(await _click_element(click_page, "@e7"))
        self.assertEqual(
            '[data-chatds-agent-ref="e7"]',
            click_page.selector,
        )
        self.assertTrue(click_locator.clicked)

        type_locator = _FakeLocator(1)
        type_page = _LocatorPage(type_locator)
        self.assertIsNone(await _type_text(type_page, "e2", "hello"))
        self.assertEqual("hello", type_locator.filled)

    async def test_stale_or_ambiguous_ref_fails_closed(self):
        page = _LocatorPage(_FakeLocator(2))
        error = await _click_element(page, "@e1")
        self.assertIn("stale or ambiguous", error or "")
        self.assertIn("fresh browser_snapshot", error or "")


if __name__ == "__main__":
    unittest.main()
