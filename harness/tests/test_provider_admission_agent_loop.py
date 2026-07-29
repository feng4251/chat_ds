import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_loop
from provider_admission import ProviderAdmissionLimits, provider_admission


def _provider(endpoint: str = "http://provider.invalid/v1") -> dict:
    return {
        "id": "admission-test",
        "base_url": endpoint,
        "api_model": "admission-model",
        "api_key": "EMPTY",
        "protocol": "openai",
        "provider": "test",
        "context_length": 64_000,
        "is_multimodal": False,
        "supports_thinking_toggle": True,
        "thinking_enabled_by_default": False,
    }


def _sse_lines(content: str = "ok") -> list[str]:
    return [
        "data: " + json.dumps({
            "choices": [{
                "delta": {"content": content},
                "finish_reason": None,
            }],
        }),
        "",
        "data: " + json.dumps({
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
            },
        }),
        "",
        "data: [DONE]",
        "",
    ]


class _ControlledResponse:
    status_code = 200

    def __init__(self, entered: asyncio.Event, release: asyncio.Event):
        self.entered = entered
        self.release = release

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_lines(self):
        await self.release.wait()
        for line in _sse_lines():
            yield line


class _ImmediateResponse:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def aiter_lines(self):
        for line in _sse_lines():
            yield line


class ProviderAdmissionAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def _common_patches(
        self,
        temp_dir: str,
        async_client,
        *,
        token_estimator,
        wait_timeout: float = 0.0,
        debug: bool = True,
    ):
        return (
            patch("workspace_context.WORKSPACE_ROOT", Path(temp_dir)),
            patch("agent_loop.httpx.AsyncClient", async_client),
            patch("agent_loop._estimate_payload_tokens", side_effect=token_estimator),
            patch("agent_loop.build_system_prompt", return_value="system"),
            patch("agent_loop.load_workspace_context", return_value=""),
            patch("agent_loop._fetch_goal", AsyncMock(return_value=None)),
            patch("skills.scanner.find_all_skills", return_value=[]),
            patch.object(agent_loop.settings, "agent_debug_trace", debug),
            patch.object(
                agent_loop.settings,
                "provider_admission_max_inflight_requests",
                3,
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_max_inflight_estimated_tokens",
                100,
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_estimate_safety_factor",
                1.0,
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_wait_timeout_seconds",
                wait_timeout,
            ),
        )

    async def _collect_run(
        self,
        text: str,
        *,
        provider: dict,
        user_id: str,
        session_id: str,
        sink: list[dict] | None = None,
        **run_stream_kwargs,
    ) -> list[dict]:
        events: list[dict] = []

        async def event_sink(event: dict) -> None:
            if sink is not None:
                sink.append(event)

        async for event in agent_loop.run_stream(
            provider["id"],
            [{"role": "user", "content": text}],
            [],
            user_id=user_id,
            session_id=session_id,
            max_iterations=1,
            max_tokens=1,
            provider_override=provider,
            enabled_user_skills=[],
            enforce_session_skill_workflow=False,
            include_session_context=False,
            allow_session_mcp=False,
            event_sink=event_sink,
            **run_stream_kwargs,
        ):
            events.append(event)
        return events

    async def test_two_sessions_are_weighted_before_provider_http(self):
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        responses: list[_ControlledResponse] = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                index = len(responses)
                response = _ControlledResponse(entered[index], release[index])
                responses.append(response)
                return response

        def estimate(messages, _schemas):
            serialized = repr(messages)
            return 70 if "first-heavy" in serialized else 40

        provider = _provider()
        first_debug: list[dict] = []
        second_debug: list[dict] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=estimate,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11]:
                first = asyncio.create_task(self._collect_run(
                    "first-heavy",
                    provider=provider,
                    user_id="u-1",
                    session_id="s-1",
                    sink=first_debug,
                ))
                await asyncio.wait_for(entered[0].wait(), timeout=1)
                second = asyncio.create_task(self._collect_run(
                    "second-light",
                    provider=provider,
                    user_id="u-2",
                    session_id="s-2",
                    sink=second_debug,
                ))
                await asyncio.sleep(0.03)
                self.assertEqual(len(responses), 1)

                release[0].set()
                await asyncio.wait_for(entered[1].wait(), timeout=1)
                release[1].set()
                first_events, second_events = await asyncio.gather(first, second)

        self.assertTrue(any(item.get("type") == "done" for item in first_events))
        self.assertTrue(any(item.get("type") == "done" for item in second_events))
        admission_events = [
            event for event in first_debug + second_debug
            if str(event.get("event_type") or "").startswith(
                "debug.provider.admission."
            )
        ]
        self.assertTrue(any(
            event["event_type"] == "debug.provider.admission.queued"
            for event in admission_events
        ))
        self.assertTrue(any(
            int((event.get("payload") or {}).get("wait_ms") or 0) > 0
            for event in admission_events
            if event["event_type"] == "debug.provider.admission.acquired"
        ))
        self.assertTrue(all(
            "provider_key_sha256" in (event.get("payload") or {})
            and "provider.invalid" not in repr(event.get("payload") or {})
            for event in admission_events
        ))

    async def test_admission_wait_is_outside_stream_timeout_wrapper(self):
        timeout_wrapper_entered = asyncio.Event()

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                return _ImmediateResponse()

        original_timeout = agent_loop._aiter_with_timeout

        async def observed_timeout(
            iterator,
            *,
            timeout_seconds,
            material_progress_lease=None,
            execution_context=None,
        ):
            timeout_wrapper_entered.set()
            async for item in original_timeout(
                iterator,
                timeout_seconds=timeout_seconds,
                material_progress_lease=material_progress_lease,
                execution_context=execution_context,
            ):
                yield item

        provider = _provider()
        limits = ProviderAdmissionLimits(
            max_inflight_requests=3,
            max_inflight_estimated_tokens=100,
        )
        blocker = await provider_admission.acquire(
            endpoint=provider["base_url"],
            api_model=provider["api_model"],
            estimated_input_tokens=100,
            max_output_tokens=0,
            limits=limits,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=lambda _messages, _schemas: 70,
                debug=False,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11], patch(
                        "agent_loop._aiter_with_timeout",
                        observed_timeout,
                    ):
                run = asyncio.create_task(self._collect_run(
                    "wait outside timeout",
                    provider=provider,
                    user_id="u-time",
                    session_id="s-time",
                ))
                await asyncio.sleep(0.03)
                self.assertFalse(timeout_wrapper_entered.is_set())
                await blocker.release()
                await asyncio.wait_for(timeout_wrapper_entered.wait(), timeout=1)
                events = await asyncio.wait_for(run, timeout=1)

        self.assertTrue(any(item.get("type") == "done" for item in events))

    async def test_caller_and_configured_stream_deadlines_use_stricter_bound(self):
        observed_timeouts: list[float] = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                return _ImmediateResponse()

        original_timeout = agent_loop._aiter_with_timeout

        async def observed_timeout(
            iterator,
            *,
            timeout_seconds,
            material_progress_lease=None,
            execution_context=None,
        ):
            observed_timeouts.append(timeout_seconds)
            async for item in original_timeout(
                iterator,
                timeout_seconds=timeout_seconds,
                material_progress_lease=material_progress_lease,
                execution_context=execution_context,
            ):
                yield item

        provider = _provider()
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=lambda _messages, _schemas: 10,
                debug=False,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11], patch(
                        "agent_loop._aiter_with_timeout",
                        observed_timeout,
                    ), patch.object(
                        agent_loop.settings,
                        "llm_stream_total_timeout_seconds",
                        45.0,
                    ):
                caller_bounded = await self._collect_run(
                    "caller deadline",
                    provider=provider,
                    user_id="u-caller-deadline",
                    session_id="s-caller-deadline",
                    timeout=7.5,
                )
                config_bounded = await self._collect_run(
                    "configured deadline",
                    provider=provider,
                    user_id="u-config-deadline",
                    session_id="s-config-deadline",
                    timeout=90.0,
                )

        self.assertEqual(observed_timeouts, [7.5, 45.0])
        self.assertTrue(any(item.get("type") == "done" for item in caller_bounded))
        self.assertTrue(any(item.get("type") == "done" for item in config_bounded))

    async def test_explicit_bounded_synthesis_policy_is_provider_capability_aware(self):
        request_bodies: list[dict] = []
        debug_events: list[dict] = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                request_bodies.append(json.loads(json.dumps(kwargs["json"])))
                return _ImmediateResponse()

        provider = _provider()
        provider["thinking_enabled_by_default"] = True
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=lambda _messages, _schemas: 10,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11]:
                events = await self._collect_run(
                    "bounded synthesis",
                    provider=provider,
                    user_id="u-bounded-policy",
                    session_id="s-bounded-policy",
                    sink=debug_events,
                    thinking_policy="off_if_supported",
                    temperature_override=0,
                )
                unsupported_provider = dict(provider)
                unsupported_provider["supports_thinking_toggle"] = False
                unsupported_events = await self._collect_run(
                    "bounded synthesis without a provider toggle",
                    provider=unsupported_provider,
                    user_id="u-bounded-policy-unsupported",
                    session_id="s-bounded-policy-unsupported",
                    thinking_policy="off_if_supported",
                    temperature_override=0,
                )

        self.assertTrue(any(item.get("type") == "done" for item in events))
        self.assertTrue(any(
            item.get("type") == "done" for item in unsupported_events
        ))
        self.assertEqual(request_bodies[0]["temperature"], 0)
        self.assertEqual(
            request_bodies[0]["chat_template_kwargs"],
            {"enable_thinking": False},
        )
        self.assertEqual(request_bodies[1]["temperature"], 0)
        self.assertNotIn("chat_template_kwargs", request_bodies[1])
        request_debug = next(
            event for event in debug_events
            if event.get("event_type") == "debug.llm.request"
        )
        self.assertEqual(
            request_debug["payload"]["thinking_policy"],
            "off_if_supported",
        )
        self.assertTrue(request_debug["payload"]["thinking_disabled"])
        self.assertEqual(request_debug["payload"]["effective_temperature"], 0)

    async def test_default_policy_preserves_ordinary_request_parameters(self):
        request_bodies: list[dict] = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                request_bodies.append(json.loads(json.dumps(kwargs["json"])))
                return _ImmediateResponse()

        provider = _provider()
        provider["thinking_enabled_by_default"] = True
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=lambda _messages, _schemas: 10,
                debug=False,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11]:
                events = await self._collect_run(
                    "ordinary chat",
                    provider=provider,
                    user_id="u-default-policy",
                    session_id="s-default-policy",
                )

        self.assertTrue(any(item.get("type") == "done" for item in events))
        self.assertEqual(request_bodies[0]["temperature"], 0.7)
        self.assertNotIn("chat_template_kwargs", request_bodies[0])

    async def test_run_stream_policy_arguments_are_strictly_validated(self):
        provider = _provider()

        async def collect(**kwargs):
            return [
                event
                async for event in agent_loop.run_stream(
                    provider["id"],
                    [{"role": "user", "content": "validation"}],
                    [],
                    provider_override=provider,
                    **kwargs,
                )
            ]

        invalid_cases = [
            ({"thinking_policy": "always_off"}, ValueError),
            ({"thinking_policy": False}, TypeError),
            ({"temperature_override": True}, TypeError),
            ({"temperature_override": float("inf")}, ValueError),
            ({"temperature_override": -0.01}, ValueError),
            ({"temperature_override": 2.01}, ValueError),
            ({"timeout": False}, TypeError),
            ({"timeout": 0}, ValueError),
            ({"timeout": float("nan")}, ValueError),
        ]
        for kwargs, error_type in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(error_type):
                    await collect(**kwargs)

    async def test_absolute_iterator_deadline_has_no_one_second_floor(self):
        entered = asyncio.Event()

        async def blocked_iterator():
            entered.set()
            await asyncio.Event().wait()
            yield {"unreachable": True}

        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(asyncio.TimeoutError):
            async for _event in agent_loop._aiter_with_timeout(
                blocked_iterator(),
                timeout_seconds=0.02,
            ):
                pass
        elapsed = loop.time() - started

        self.assertTrue(entered.is_set())
        self.assertGreaterEqual(elapsed, 0.01)
        self.assertLess(elapsed, 0.5)

    async def test_admission_timeout_is_explicit_and_never_opens_http(self):
        stream_opened = False

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                nonlocal stream_opened
                stream_opened = True
                return _ImmediateResponse()

        provider = _provider()
        blocker = await provider_admission.acquire(
            endpoint=provider["base_url"],
            api_model=provider["api_model"],
            estimated_input_tokens=100,
            max_output_tokens=0,
            limits=ProviderAdmissionLimits(
                max_inflight_requests=3,
                max_inflight_estimated_tokens=100,
            ),
        )
        debug: list[dict] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=lambda _messages, _schemas: 70,
                wait_timeout=0.01,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11], patch("agent_loop.MAX_RETRIES", 1):
                events = await self._collect_run(
                    "capacity timeout",
                    provider=provider,
                    user_id="u-cap",
                    session_id="s-cap",
                    sink=debug,
                )
        await blocker.release()

        self.assertFalse(stream_opened)
        self.assertTrue(any(
            event.get("event_type")
            == "debug.provider.admission.timed_out"
            for event in debug
        ))
        classified = next(
            event for event in debug
            if event.get("event_type")
            == "debug.provider.admission.timeout_classified"
        )
        self.assertFalse(classified["payload"]["provider_request_opened"])
        failed = next(
            event for event in debug
            if event.get("event_type") == "run.failed"
        )
        self.assertEqual(
            failed["payload"]["finish_reason"],
            "provider_admission_timeout",
        )
        self.assertTrue(failed["payload"]["retryable"])
        self.assertTrue(any(item.get("type") == "error" for item in events))

    async def test_nonstream_fallback_uses_provider_budget_and_repair_timeout(self):
        post_entered = asyncio.Event()
        client_timeouts = []

        class JSONResponse:
            status_code = 200

            def json(self):
                return {
                    "choices": [{
                        "message": {
                            "tool_calls": [{
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"filepath":"a.md"}',
                                },
                            }],
                        },
                    }],
                }

        class Client:
            def __init__(self, *args, **kwargs):
                client_timeouts.append(kwargs.get("timeout"))

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def post(self, url, **kwargs):
                post_entered.set()
                return JSONResponse()

        provider = _provider()
        limits = ProviderAdmissionLimits(
            max_inflight_requests=3,
            max_inflight_estimated_tokens=100,
        )
        blocker = await provider_admission.acquire(
            endpoint=provider["base_url"],
            api_model=provider["api_model"],
            estimated_input_tokens=100,
            max_output_tokens=0,
            limits=limits,
        )
        with (
            patch("agent_loop.httpx.AsyncClient", Client),
            patch.object(
                agent_loop.settings,
                "provider_admission_max_inflight_requests",
                3,
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_max_inflight_estimated_tokens",
                100,
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_estimate_safety_factor",
                1.0,
            ),
            patch.object(
                agent_loop.settings,
                "llm_stream_total_timeout_seconds",
                1500.0,
            ),
            patch.object(
                agent_loop.settings,
                "llm_nonstream_repair_timeout_seconds",
                73.0,
            ),
        ):
            fallback = asyncio.create_task(
                agent_loop._request_openai_nonstream_tool_call_fallback(
                    request_url=provider["base_url"] + "/chat/completions",
                    provider_endpoint=provider["base_url"],
                    api_model=provider["api_model"],
                    headers={},
                    streamed_body={"model": provider["api_model"], "max_tokens": 1},
                    exposed_tool_names={"read_file"},
                    estimated_input_tokens=70,
                )
            )
            await asyncio.sleep(0.03)
            self.assertFalse(post_entered.is_set())
            await blocker.release()
            result = await asyncio.wait_for(fallback, timeout=1)

        self.assertTrue(post_entered.is_set())
        self.assertEqual(len(client_timeouts), 1)
        self.assertEqual(client_timeouts[0].read, 73.0)
        self.assertIsNotNone(result[0])
        self.assertTrue(result[0].ok)

    async def test_goal_judge_and_semantic_selector_direct_posts_are_admitted(self):
        post_entered = asyncio.Event()

        class JSONResponse:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def post(self, url, **kwargs):
                post_entered.set()
                body = kwargs.get("json") or {}
                if body.get("tools"):
                    return JSONResponse({
                        "choices": [{
                            "message": {
                                "tool_calls": [{
                                    "id": "selector",
                                    "type": "function",
                                    "function": {
                                        "name": "select_session_skill",
                                        "arguments": json.dumps({
                                            "decision": "candidate",
                                            "candidate_names": ["alpha"],
                                            "reason": "exact metadata match",
                                        }),
                                    },
                                }],
                            },
                            "finish_reason": "tool_calls",
                        }],
                    })
                return JSONResponse({
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "status": "complete",
                                "reason": "evidence is complete",
                            }),
                        },
                    }],
                })

        provider = _provider()
        limits = ProviderAdmissionLimits(
            max_inflight_requests=3,
            max_inflight_estimated_tokens=100,
        )

        async def blocker():
            return await provider_admission.acquire(
                endpoint=provider["base_url"],
                api_model=provider["api_model"],
                estimated_input_tokens=100,
                max_output_tokens=0,
                limits=limits,
            )

        with (
            patch("agent_loop.httpx.AsyncClient", Client),
            patch.dict(
                agent_loop.PROVIDERS,
                {agent_loop.DEFAULT_AGENT_MODEL_ID: provider},
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_max_inflight_requests",
                3,
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_max_inflight_estimated_tokens",
                100,
            ),
            patch.object(
                agent_loop.settings,
                "provider_admission_estimate_safety_factor",
                1.0,
            ),
        ):
            first_blocker = await blocker()
            judge = asyncio.create_task(agent_loop._judge_goal(
                "finish the work",
                "the work is complete",
            ))
            await asyncio.sleep(0.03)
            self.assertFalse(post_entered.is_set())
            await first_blocker.release()
            verdict = await asyncio.wait_for(judge, timeout=1)
            self.assertEqual(verdict[0], "complete")

            post_entered.clear()
            second_blocker = await blocker()
            selector = asyncio.create_task(
                agent_loop._request_semantic_skill_selector_page(
                    request_text="use alpha",
                    page=[{"name": "alpha", "description": "alpha tasks"}],
                    page_index=0,
                    total_pages=1,
                    provider=provider,
                )
            )
            await asyncio.sleep(0.03)
            self.assertFalse(post_entered.is_set())
            await second_blocker.release()
            decision, failure, _usage = await asyncio.wait_for(
                selector,
                timeout=1,
            )

        self.assertEqual(failure, "")
        self.assertEqual(decision["candidate_names"], ("alpha",))

    async def test_cancelling_queued_run_removes_it_before_http(self):
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        responses: list[_ControlledResponse] = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                index = len(responses)
                response = _ControlledResponse(entered[index], release[index])
                responses.append(response)
                return response

        provider = _provider()
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=lambda _messages, _schemas: 70,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11]:
                first = asyncio.create_task(self._collect_run(
                    "active",
                    provider=provider,
                    user_id="u-active",
                    session_id="s-active",
                ))
                await asyncio.wait_for(entered[0].wait(), timeout=1)
                queued = asyncio.create_task(self._collect_run(
                    "cancel me",
                    provider=provider,
                    user_id="u-cancel",
                    session_id="s-cancel",
                ))
                await asyncio.sleep(0.03)
                self.assertEqual(len(responses), 1)
                queued.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await queued

                release[0].set()
                await asyncio.wait_for(first, timeout=1)
                third = asyncio.create_task(self._collect_run(
                    "after cancel",
                    provider=provider,
                    user_id="u-third",
                    session_id="s-third",
                ))
                await asyncio.wait_for(entered[1].wait(), timeout=1)
                self.assertEqual(len(responses), 2)
                release[1].set()
                third_events = await asyncio.wait_for(third, timeout=1)

        self.assertTrue(any(item.get("type") == "done" for item in third_events))

    async def test_provider_exception_releases_lease_for_next_run(self):
        call_count = 0

        class FailingResponse(_ImmediateResponse):
            async def aiter_lines(self):
                raise RuntimeError("synthetic provider failure")
                yield "unreachable"

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                nonlocal call_count
                call_count += 1
                return FailingResponse() if call_count == 1 else _ImmediateResponse()

        provider = _provider()
        with tempfile.TemporaryDirectory() as temp_dir:
            patches = self._common_patches(
                temp_dir,
                Client,
                token_estimator=lambda _messages, _schemas: 70,
                wait_timeout=0.05,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8], patches[9], \
                    patches[10], patches[11], patch("agent_loop.MAX_RETRIES", 1):
                failed_events = await self._collect_run(
                    "first fails",
                    provider=provider,
                    user_id="u-fail",
                    session_id="s-fail",
                )
                next_events = await asyncio.wait_for(self._collect_run(
                    "second succeeds",
                    provider=provider,
                    user_id="u-next",
                    session_id="s-next",
                ), timeout=1)

        self.assertTrue(any(item.get("type") == "error" for item in failed_events))
        self.assertTrue(any(item.get("type") == "done" for item in next_events))
        self.assertEqual(call_count, 2)
