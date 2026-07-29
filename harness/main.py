"""Harness service — OpenAI-compatible API that routes to configured vLLM backends.

GET  /v1/models              → list configured providers
POST /v1/chat/completions    → SSE stream (multi-turn agent with tool calling)
"""

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import (
    DEFAULT_AGENT_MODEL_ID,
    PROVIDERS,
    canonical_provider_id,
    settings,
)
from agent_loop import (
    _safe_exception_stack_projection,
    _safe_unhandled_run_failure_event,
    run_stream,
    set_harness_service_shutdown_started,
)
import tools  # noqa: F401 — triggers tool registration

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Reclaim stale executor leases, then own all session runtime cleanup."""

    set_harness_service_shutdown_started(False)
    # Process capabilities intentionally remain in Harness memory. A crash can
    # therefore leave an executor-side lease alive. Before serving any request,
    # the replacement Harness authenticates to every configured executor and
    # proves the fixed worker slot empty.
    from tools.isolated_skill_executor import reap_isolated_executor_leases
    from tools.skill_runtime_profile import (
        BASE_RUNTIME_PROFILE,
        BROWSER_RUNTIME_PROFILE,
        runtime_profile_socket_binding,
    )

    startup_profiles = [BASE_RUNTIME_PROFILE]
    if os.environ.get("SKILL_BROWSER_EXECUTOR_SOCKET", "").strip():
        startup_profiles.append(BROWSER_RUNTIME_PROFILE)
    seen_sockets: set[str] = set()
    for runtime_profile in startup_profiles:
        binding = runtime_profile_socket_binding(runtime_profile)
        if binding.socket_path in seen_sockets:
            raise RuntimeError(
                "Distinct executor profiles must not share one process socket."
            )
        seen_sockets.add(binding.socket_path)
        receipt = await reap_isolated_executor_leases(
            socket_path=binding.socket_path,
        )
        if receipt.get("runtime_profile") != runtime_profile:
            raise RuntimeError(
                "Executor startup reap returned the wrong runtime profile."
            )
        logger.info(
            "Executor startup reap complete profile=%s leases=%s",
            runtime_profile,
            receipt.get("reaped_leases"),
        )

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    set_harness_service_shutdown_started(True)
    try:
        from tools.mcp_client import disconnect_all_for_user, get_active_mcp_sessions
        for user_id, session_id in get_active_mcp_sessions():
            try:
                await disconnect_all_for_user(user_id, session_id)
                logger.info(
                    "Shutdown MCP disconnect: user=%s session=%s",
                    user_id, session_id,
                )
            except Exception:
                logger.exception(
                    "Shutdown MCP disconnect failed for user=%s session=%s",
                    user_id, session_id,
                )
    except Exception:
        logger.exception("Shutdown MCP disconnect failed")

    try:
        from tools.browser import close_all_browser_sessions
        await close_all_browser_sessions()
    except Exception:
        logger.exception("Shutdown browser cleanup failed")

    try:
        from tools.skill_process import close_all_skill_processes
        await close_all_skill_processes()
    except Exception:
        logger.exception("Shutdown persistent Skill process cleanup failed")


app = FastAPI(title="Chat ACITS Harness", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def require_internal_api_token(request: Request, call_next):
    """Authenticate every stateful Harness API boundary.

    The Harness shares a container network with extensible subprocess-backed
    capabilities.  Network placement alone is therefore not authentication:
    callers of the agent loop and its internal control plane must prove that
    they are the trusted backend.  Health and the read-only model catalog stay
    available to container health checks and service discovery.
    """

    path = request.url.path
    if path == "/v1/chat/completions" or path.startswith("/internal/"):
        supplied = request.headers.get("X-Internal-Token", "")
        expected = str(settings.internal_api_token or "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized Harness request"},
            )
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok", "title": "Chat ACITS Harness"}


# ── Internal endpoints (not exposed externally) ────────────────────────────


@app.post("/internal/mcp/connect/{user_id}/{server_name}")
async def internal_mcp_connect(
    user_id: str, server_name: str, session_id: str = "default",
):
    """Internal endpoint for backend to trigger MCP connection after config write."""
    from tools.mcp_client import connect_server
    state = await connect_server(user_id, server_name, session_id)
    return {
        "connected": state.connected if state else False,
        "transport": state.transport if state else None,
        "tool_count": len(state.tools) if state else 0,
    }


@app.post("/internal/mcp/server/add")
async def internal_mcp_server_add(req: Request):
    """Control-plane mutation for an explicitly scoped MCP server."""
    from tools.mcp_client import mcp_server_add
    body = await req.json()
    result = await mcp_server_add(**body)
    return json.loads(result)


@app.post("/internal/mcp/server/remove")
async def internal_mcp_server_remove(req: Request):
    from tools.mcp_client import mcp_server_remove
    body = await req.json()
    result = await mcp_server_remove(**body)
    return json.loads(result)


@app.get("/internal/mcp/servers")
async def internal_mcp_servers(
    user_id: str,
    session_id: str = "default",
):
    from tools.mcp_client import mcp_server_list
    return json.loads(await mcp_server_list(user_id=user_id, session_id=session_id))


@app.get("/internal/mcp/tools")
async def internal_mcp_tools(
    user_id: str,
    session_id: str = "default",
):
    """Inspect the model-visible MCP catalog for one runtime scope."""
    from tools.mcp_client import get_session_tool_definitions
    definitions = get_session_tool_definitions(user_id, session_id)
    return {
        "user_id": user_id,
        "session_id": session_id,
        "tools": definitions,
        "count": len(definitions),
    }


@app.post("/internal/mcp/auto-register")
async def internal_mcp_auto_register(
    skill_dir: str = "",
    user_id: str = "default",
    session_id: str = "default",
):
    """Internal endpoint for backend to trigger MCP auto-registration from a skill."""
    from tools.mcp_auto import auto_register_skill_mcp
    result = await auto_register_skill_mcp(skill_dir, user_id, session_id)
    return result


@app.post("/internal/mcp/remove-skill")
async def internal_mcp_remove_skill(
    skill_dir: str = "",
    user_id: str = "default",
    session_id: str = "default",
):
    """Remove live/configured MCP servers owned by one skill directory."""
    from tools.mcp_auto import remove_skill_mcp
    return await remove_skill_mcp(skill_dir, user_id, session_id)


@app.post("/internal/session/cleanup")
async def internal_session_cleanup(
    user_id: str,
    session_id: str,
):
    """Tear down every session-owned runtime without masking either result."""
    from tools.mcp_client import cleanup_session_runtime
    from tools.skill_process import cleanup_skill_process_session

    try:
        process_result = await cleanup_skill_process_session(
            user_id,
            session_id,
        )
    except Exception as exc:
        logger.exception(
            "Session persistent Skill process cleanup failed user=%s session=%s",
            user_id,
            session_id,
        )
        process_result = {
            "success": False,
            "error_code": type(exc).__name__,
        }
    try:
        mcp_result = await cleanup_session_runtime(user_id, session_id)
    except Exception as exc:
        logger.exception(
            "Session MCP cleanup failed user=%s session=%s",
            user_id,
            session_id,
        )
        mcp_result = {
            "success": False,
            "error_code": type(exc).__name__,
        }
    return {
        **mcp_result,
        "success": bool(
            mcp_result.get("success") is True
            and process_result.get("success") is True
        ),
        "skill_processes": process_result,
    }


@app.post("/internal/runtime/python/ensure")
async def internal_runtime_python_ensure(
    user_id: str,
    session_id: str,
):
    from runtime.python_env import ensure_session_runtime
    return await ensure_session_runtime(user_id, session_id)


@app.get("/internal/runtime/python/status")
async def internal_runtime_python_status(
    user_id: str,
    session_id: str,
):
    from runtime.python_env import get_session_runtime_status
    return get_session_runtime_status(user_id, session_id)


# ── Public endpoints ───────────────────────────────────────────────────────


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": mid,
                "object": "model",
                "owned_by": "harness",
                "display_name": cfg.get("display_name", mid),
                "provider": cfg.get("provider", ""),
                "is_multimodal": cfg.get("is_multimodal", False),
                "is_default": cfg.get("is_default", False),
                "capabilities": cfg.get("capabilities", ["text"]),
                "context_length": cfg.get("context_length"),
                "discover_runtime_metadata": bool(
                    cfg.get("discover_runtime_metadata", False)
                ),
            }
            for mid, cfg in PROVIDERS.items()
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    model_id = canonical_provider_id(body.get("model") or DEFAULT_AGENT_MODEL_ID)
    messages: list[dict] = body.get("messages", [])
    stream: bool = body.get("stream", False)
    tools: Optional[list[str]] = body.get("tools")
    provider_config: Optional[dict] = body.get("provider_config")
    fallback_configs: list[dict] = body.get("fallback_configs") or []
    source: str = body.get("source", "chat")
    max_tokens: int | None = body.get("max_tokens")
    run_metadata: dict = body.get("run_metadata") or {}
    event_schema: str = body.get("event_schema") or "flat"

    # Per-user / per-session isolation
    user_id: str = body.get("user", "default")
    session_id: str = body.get("session_id", str(uuid.uuid4()))
    enabled_user_skills: list[str] = body.get("enabled_user_skills") or []
    session_skill_registry = body.get("session_skill_registry")

    if stream:
        return _streaming_response(
            model_id, messages, tools, user_id, session_id,
            provider_config, fallback_configs, source,
            enabled_user_skills,
            session_skill_registry,
            max_tokens,
            run_metadata,
            event_schema,
        )

    # ── Non-streaming: collect all events, assemble full response ──────
    full_content = ""
    full_reasoning = ""
    finish_reason = "stop"
    tool_progress: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    resolved_model = model_id

    async for evt in run_stream(
        model_id, messages, tools,
        user_id=user_id, session_id=session_id,
        provider_override=provider_config,
        fallback_overrides=fallback_configs,
        source=source,
        enabled_user_skills=enabled_user_skills,
        session_skill_registry=session_skill_registry,
        max_tokens=max_tokens,
        run_id=run_metadata.get("run_id"),
        root_run_id=run_metadata.get("root_run_id"),
        parent_run_id=run_metadata.get("parent_run_id"),
        agent_kind=run_metadata.get("agent_kind") or "primary",
        agent_name=run_metadata.get("agent_name"),
        depth=int(run_metadata.get("depth") or 0),
        workspace_scope=run_metadata.get("workspace_scope") or "shared_session",
        event_schema=event_schema,
    ):
        tp = evt["type"]
        if tp == "delta":
            full_content += evt.get("content", "")
        elif tp == "reasoning_delta":
            full_reasoning += evt.get("content", "")
        elif tp == "tool_progress":
            tool_progress.append(evt.get("msg", ""))
        elif tp == "done":
            finish_reason = evt.get("finish_reason", "stop")
        elif tp == "usage":
            usage = {
                "input_tokens": evt.get("input_tokens", 0),
                "output_tokens": evt.get("output_tokens", 0),
                "total_tokens": evt.get("total_tokens", 0),
            }
            resolved_model = evt.get("model") or resolved_model
        elif tp == "model_switch":
            resolved_model = evt.get("to_model") or resolved_model
        elif tp == "error":
            return {
                "error": {"message": evt.get("msg", "Unknown error"), "type": "agent_error"}
            }

    message = {"role": "assistant", "content": full_content or None}
    if full_reasoning:
        message["reasoning"] = full_reasoning
    if tool_progress:
        message["tool_progress"] = tool_progress

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": resolved_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            **usage,
        },
    }


def _streaming_response(
    model_id: str,
    messages: list[dict],
    tools: Optional[list[str]],
    user_id: str,
    session_id: str,
    provider_config: dict | None,
    fallback_configs: list[dict],
    source: str,
    enabled_user_skills: list[str] | None = None,
    session_skill_registry: list[dict] | None = None,
    max_tokens: int | None = None,
    run_metadata: dict | None = None,
    event_schema: str = "flat",
) -> StreamingResponse:
    """Build an SSE streaming response from the agent loop."""
    run_metadata = run_metadata or {}

    async def _stream():
        if event_schema != "chatds.agent.v2":
            async for evt in run_stream(
                model_id,
                messages,
                tools,
                user_id=user_id,
                session_id=session_id,
                provider_override=provider_config,
                fallback_overrides=fallback_configs,
                source=source,
                enabled_user_skills=enabled_user_skills,
                session_skill_registry=session_skill_registry,
                max_tokens=max_tokens,
                run_id=run_metadata.get("run_id"),
                root_run_id=run_metadata.get("root_run_id"),
                parent_run_id=run_metadata.get("parent_run_id"),
                agent_kind=run_metadata.get("agent_kind") or "primary",
                agent_name=run_metadata.get("agent_name"),
                depth=int(run_metadata.get("depth") or 0),
                workspace_scope=run_metadata.get("workspace_scope") or "shared_session",
                event_schema=event_schema,
            ):
                if evt.get("type") == "agent_event":
                    continue
                async for encoded in _encode_stream_event(evt):
                    yield encoded
            return

        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        seen_agent_events: set[tuple[str, str, int]] = set()
        producer_terminal_seen = False
        producer_maximum_event_seq = 0
        stream_run_id = str(
            run_metadata.get("run_id") or uuid.uuid4().hex
        )
        stream_root_run_id = str(
            run_metadata.get("root_run_id") or stream_run_id
        )
        stream_agent_kind = str(
            run_metadata.get("agent_kind") or "primary"
        )
        stream_agent_name = str(
            run_metadata.get("agent_name") or stream_agent_kind
        )
        stream_depth = int(run_metadata.get("depth") or 0)
        stream_workspace_scope = str(
            run_metadata.get("workspace_scope") or "shared_session"
        )

        def observe_producer_event(event: dict) -> None:
            nonlocal producer_terminal_seen
            nonlocal producer_maximum_event_seq
            if (
                not isinstance(event, dict)
                or event.get("type") != "agent_event"
                or str(event.get("run_id") or "") != stream_run_id
            ):
                return
            try:
                producer_maximum_event_seq = max(
                    producer_maximum_event_seq,
                    int(event.get("seq") or 0),
                )
            except (TypeError, ValueError):
                pass
            if str(event.get("event_type") or "") in {
                "run.completed",
                "run.failed",
                "run.cancelled",
            }:
                producer_terminal_seen = True

        async def forward_agent_event(event: dict) -> None:
            observe_producer_event(event)
            await queue.put(event)

        async def produce() -> None:
            try:
                async for evt in run_stream(
                    model_id,
                    messages,
                    tools,
                    user_id=user_id,
                    session_id=session_id,
                    provider_override=provider_config,
                    fallback_overrides=fallback_configs,
                    source=source,
                    enabled_user_skills=enabled_user_skills,
                    session_skill_registry=session_skill_registry,
                    max_tokens=max_tokens,
                    run_id=stream_run_id,
                    root_run_id=stream_root_run_id,
                    parent_run_id=run_metadata.get("parent_run_id"),
                    agent_kind=stream_agent_kind,
                    agent_name=stream_agent_name,
                    depth=stream_depth,
                    workspace_scope=stream_workspace_scope,
                    event_schema=event_schema,
                    event_sink=forward_agent_event,
                ):
                    observe_producer_event(evt)
                    await queue.put(evt)
                if not producer_terminal_seen:
                    failure_event = _safe_unhandled_run_failure_event(
                        run_id=stream_run_id,
                        root_run_id=stream_root_run_id,
                        parent_run_id=run_metadata.get("parent_run_id"),
                        agent_kind=stream_agent_kind,
                        agent_name=stream_agent_name,
                        depth=stream_depth,
                        workspace_scope=stream_workspace_scope,
                        seq=producer_maximum_event_seq + 1,
                        source=source,
                        terminal_reason="missing_terminal_event",
                        failure_class="harness_lifecycle_error",
                        error_message=(
                            "Harness execution ended without a terminal run "
                            "event."
                        ),
                    )
                    observe_producer_event(failure_event)
                    await queue.put(failure_event)
                    await queue.put({
                        "type": "error",
                        "msg": failure_event["payload"]["error"],
                    })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The agent-loop lifecycle wrapper is the primary authority.
                # Keep this independent producer boundary as a final guard for
                # patched/custom generators or failures in that wrapper:
                # clients must receive a terminal event, never a naked EOF or
                # an unobserved background-task exception.
                logger.error(
                    "Unhandled SSE producer exception run=%s class=%s "
                    "stack=%s",
                    stream_run_id,
                    type(exc).__name__,
                    json.dumps(
                        _safe_exception_stack_projection(exc),
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                )
                if not producer_terminal_seen:
                    failure_event = _safe_unhandled_run_failure_event(
                        run_id=stream_run_id,
                        root_run_id=stream_root_run_id,
                        parent_run_id=run_metadata.get("parent_run_id"),
                        agent_kind=stream_agent_kind,
                        agent_name=stream_agent_name,
                        depth=stream_depth,
                        workspace_scope=stream_workspace_scope,
                        seq=producer_maximum_event_seq + 1,
                        source=source,
                        exception=exc,
                    )
                    observe_producer_event(failure_event)
                    await queue.put(failure_event)
                    await queue.put({
                        "type": "error",
                        "msg": failure_event["payload"]["error"],
                    })
            finally:
                await queue.put(None)

        producer = asyncio.create_task(produce())
        try:
            while True:
                evt = await queue.get()
                if evt is None:
                    break
                if evt.get("type") == "agent_event":
                    key = (
                        str(evt.get("run_id") or ""),
                        str(evt.get("event_type") or ""),
                        int(evt.get("seq") or 0),
                    )
                    if key in seen_agent_events:
                        continue
                    seen_agent_events.add(key)
                async for encoded in _encode_stream_event(evt):
                    yield encoded
        finally:
            if not producer.done():
                producer.cancel()
                try:
                    await producer
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _encode_stream_event(evt: dict):
    tp = evt["type"]
    if tp == "agent_event":
        payload = {
            "choices": [{"delta": {"agent_event": evt}, "index": 0}],
            "object": "chat.completion.chunk",
        }
        yield f"data: {json.dumps(payload)}\n\n"
    elif tp == "tool_progress":
        payload = {
            "choices": [{"delta": {"tool_progress": evt["msg"]}, "index": 0}]
        }
        yield f"data: {json.dumps(payload)}\n\n"
    elif tp == "delta":
        payload = {
            "choices": [
                {"delta": {"content": evt["content"]}, "index": 0}
            ],
            "object": "chat.completion.chunk",
        }
        yield f"data: {json.dumps(payload)}\n\n"
    elif tp == "reasoning_delta":
        payload = {
            "choices": [
                {"delta": {"reasoning": evt["content"]}, "index": 0}
            ],
            "object": "chat.completion.chunk",
        }
        yield f"data: {json.dumps(payload)}\n\n"
    elif tp == "usage":
        payload = {
            "choices": [{"delta": {"usage": {
                "input_tokens": evt.get("input_tokens", 0),
                "output_tokens": evt.get("output_tokens", 0),
                "total_tokens": evt.get("total_tokens", 0),
            }}, "index": 0}],
            "model": evt.get("model"),
            "object": "chat.completion.chunk",
        }
        yield f"data: {json.dumps(payload)}\n\n"
    elif tp == "model_switch":
        payload = {
            "choices": [{"delta": {"model_switch": {
                "from_model": evt.get("from_model"),
                "to_model": evt.get("to_model"),
                "reason": evt.get("reason"),
            }}, "index": 0}],
            "object": "chat.completion.chunk",
        }
        yield f"data: {json.dumps(payload)}\n\n"
    elif tp == "done":
        payload = {
            "choices": [
                {
                    "delta": {},
                    "finish_reason": evt.get("finish_reason", "stop"),
                    "index": 0,
                }
            ],
            "object": "chat.completion.chunk",
        }
        yield f"data: {json.dumps(payload)}\n\n"
        yield "data: [DONE]\n\n"
    elif tp == "error":
        payload = {
            "choices": [{"delta": {"error": evt["msg"]}, "index": 0}],
            "object": "chat.completion.chunk",
        }
        yield f"data: {json.dumps(payload)}\n\n"
        yield "data: [DONE]\n\n"
