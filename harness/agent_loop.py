"""Agent loop — multi-turn tool-calling with iteration budget, error retry,
and streaming think-block scrubbing.

Ported from hermes-agent/conversation_loop.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

from config import PROVIDERS, settings
from context import ContextCompressor
from error_classifier import FailoverReason, classify_api_error
from iteration_budget import IterationBudget
from prompt.builder import build_system_prompt
from retry_utils import jittered_backoff
from think_scrubber import StreamingThinkScrubber
from tools.context import ToolContext
from tools.registry import dispatch, get_schemas
from tools.tool_result_storage import wrap_result
from transports.base import build_tool_call
from workspace_context import load_workspace_context, SubdirectoryHintTracker

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
DEFAULT_MAX_TOKENS = 8192

# For a complex deliverable driven by a session skill that declares multiple
# worker/pipeline resources, encourage reading a breadth of them (not just one)
# before drafting. The target scales down for skills that declare fewer workers.
_WORKER_BREADTH_TARGET = 4


@dataclass
class HarnessRunState:
    tool_call_count: int = 0
    tool_error_count: int = 0
    parse_failure_count: int = 0
    schema_failure_count: int = 0
    successful_write_sizes: list[int] = field(default_factory=list)
    viewed_skill_names: set[str] = field(default_factory=set)
    viewed_skill_files: dict[str, set[str]] = field(default_factory=dict)
    viewed_skill_categories: dict[str, set[str]] = field(default_factory=dict)
    skill_available_categories: dict[str, set[str]] = field(default_factory=dict)
    skill_suggested_files: dict[str, list[str]] = field(default_factory=dict)
    skill_category_files: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    session_skill_names: set[str] = field(default_factory=set)
    continuation_reasons: list[str] = field(default_factory=list)

    def record_skill_view(
        self,
        args: dict[str, Any],
        result_data: dict[str, Any] | None = None,
    ) -> None:
        name = args.get("name")
        if not name:
            return
        skill_name = str(name)
        self.viewed_skill_names.add(skill_name)
        file_path = args.get("file_path")
        if isinstance(file_path, str) and file_path:
            self.viewed_skill_files.setdefault(skill_name, set()).add(file_path)
            category = file_path.split("/", 1)[0]
            self.viewed_skill_categories.setdefault(skill_name, set()).add(category)
        if isinstance(result_data, dict):
            linked = result_data.get("linked_files")
            if isinstance(linked, dict):
                self.skill_available_categories.setdefault(skill_name, set()).update(
                    str(category) for category in linked.keys()
                )
                declared = self.skill_category_files.setdefault(skill_name, {})
                for category, files in linked.items():
                    if isinstance(files, list) and files:
                        declared[str(category)] = [str(f) for f in files if isinstance(f, str)]
            graph = result_data.get("resource_graph")
            if isinstance(graph, dict):
                categories = graph.get("categories")
                if isinstance(categories, dict):
                    self.skill_available_categories.setdefault(skill_name, set()).update(
                        str(category) for category in categories.keys()
                    )
                    declared = self.skill_category_files.setdefault(skill_name, {})
                    for category, meta in categories.items():
                        # Prefer the fuller linked_files list; fall back to the sample.
                        if str(category) in declared:
                            continue
                        sample = meta.get("sample") if isinstance(meta, dict) else None
                        if isinstance(sample, list) and sample:
                            declared[str(category)] = [str(f) for f in sample if isinstance(f, str)]
                suggested = graph.get("suggested_files")
                if isinstance(suggested, list):
                    clean_suggested = [str(path) for path in suggested if isinstance(path, str)]
                    if clean_suggested:
                        self.skill_suggested_files[skill_name] = clean_suggested

    def unviewed_session_skills(self) -> set[str]:
        return self.session_skill_names - self.viewed_skill_names

    def needs_more_skill_workflow(self) -> tuple[bool, str]:
        for skill_name in sorted(self.viewed_skill_names & self.session_skill_names):
            available_categories = self.skill_available_categories.get(skill_name, set())
            if not available_categories:
                continue
            files = self.viewed_skill_files.get(skill_name, set())
            categories = self.viewed_skill_categories.get(skill_name, set())
            if "__manifest__" not in files:
                return True, f"load the resource manifest for session skill '{skill_name}'"

            declared = self.skill_category_files.get(skill_name, {})
            declared_workers = [
                path for path in declared.get("workers", [])
                if "/workers/" in path or path.startswith("workers/")
            ]
            primary_workflow_categories = {"orchestration", "workers", "workflows"}
            if declared_workers:
                # Breadth: read several distinct workers, scaled by what exists.
                viewed_workers = {
                    path for path in files
                    if "/workers/" in path or path.startswith("workers/")
                }
                target = min(len(declared_workers), _WORKER_BREADTH_TARGET)
                if len(viewed_workers) < target:
                    return True, (
                        f"inspect additional worker resources for session skill '{skill_name}' "
                        f"(viewed {len(viewed_workers)} of {target} recommended workers)"
                    )
            elif available_categories & primary_workflow_categories:
                viewed_primary = categories & primary_workflow_categories
                viewed_worker_file = any("/workers/" in path for path in files)
                if not viewed_primary and not viewed_worker_file:
                    return True, f"inspect orchestrator or worker resources for session skill '{skill_name}'"

            # Output/format specifications are frequently required for a faithful
            # deliverable; require them explicitly when the skill declares them.
            if "formats" in available_categories and "formats" not in categories:
                return True, f"inspect output format specifications for session skill '{skill_name}'"

            supporting_categories = {
                "references", "scripts", "templates", "protocols", "evaluation", "examples",
            }
            available_supporting = available_categories & supporting_categories
            if available_supporting and not (categories & available_supporting):
                return True, f"inspect supporting resources for session skill '{skill_name}'"
        return False, ""


def _suggested_workflow_paths_for_reason(
    run_state: HarnessRunState,
    reason: str,
) -> list[str]:
    requested_primary = "orchestrator" in reason or "worker" in reason
    requested_formats = "format" in reason
    requested_supporting = "supporting" in reason
    paths: list[str] = []
    for skill_name in sorted(run_state.viewed_skill_names & run_state.session_skill_names):
        already_viewed = run_state.viewed_skill_files.get(skill_name, set())
        for path in run_state.skill_suggested_files.get(skill_name, []):
            if path in already_viewed:
                continue  # prefer resources the model has not read yet
            if requested_primary:
                if path.startswith(("orchestration/", "workers/", "workflows/")) or "/workers/" in path:
                    paths.append(path)
            elif requested_formats:
                if path.startswith("formats/") or "/formats/" in path:
                    paths.append(path)
            elif requested_supporting:
                if path.startswith((
                    "references/", "scripts/", "templates/", "protocols/",
                    "evaluation/", "examples/",
                )):
                    paths.append(path)
            else:
                paths.append(path)
    return paths


async def run_stream(
    model_id: str,
    messages: list[dict],
    enabled_tools: list[str] | None = None,
    user_id: str = "default",
    session_id: str = "default",
    timeout: float = 600.0,
    max_iterations: int = 20,
    max_tokens: int | None = None,
    provider_override: dict[str, Any] | None = None,
    fallback_overrides: list[dict[str, Any]] | None = None,
    source: str = "chat",
    enabled_user_skills: list[str] | None = None,
) -> AsyncIterator[dict]:
    """Async generator yielding SSE-style dicts for a full agent conversation turn.

    Yields:
      {"type": "tool_progress", "msg": str}
      {"type": "delta", "content": str}
      {"type": "reasoning_delta", "content": str}
      {"type": "done", "finish_reason": str}
      {"type": "error", "msg": str}
    """
    provider = dict(provider_override or PROVIDERS.get(model_id) or {})
    if not provider:
        yield {"type": "error", "msg": f"Unknown model: {model_id}"}
        return
    provider.setdefault("id", model_id)
    provider.setdefault("protocol", "openai")
    provider.setdefault("context_length", 262144)
    provider_chain = [provider]
    for fallback in fallback_overrides or []:
        if fallback and fallback.get("base_url") and fallback.get("api_model"):
            normalized = dict(fallback)
            normalized.setdefault("id", normalized.get("api_model"))
            normalized.setdefault("protocol", "openai")
            normalized.setdefault("context_length", 262144)
            provider_chain.append(normalized)
    provider_cursor = 0

    def apply_provider(selected: dict) -> tuple[str, str, str, str, dict[str, str]]:
        base = str(selected["base_url"]).rstrip("/")
        api_name = str(selected["api_model"])
        key = str(selected.get("api_key") or "")
        protocol_name = str(selected.get("protocol") or "openai").lower()
        request_headers = {
            "Content-Type": "application/json",
            **(selected.get("extra_headers") or {}),
        }
        if protocol_name == "anthropic":
            if key and key != "EMPTY":
                request_headers["x-api-key"] = key
            request_headers.setdefault("anthropic-version", "2023-06-01")
        elif key and key != "EMPTY":
            request_headers["Authorization"] = f"Bearer {key}"
        return base, api_name, key, protocol_name, request_headers

    base_url, api_model, api_key, protocol, headers = apply_provider(provider)

    tools = list(enabled_tools or [])
    tool_context = ToolContext(
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        provider_config=provider,
        fallback_configs=tuple(provider_chain[1:]),
        enabled_tools=tuple(tools),
        source=source,
        enabled_user_skills=tuple(enabled_user_skills or []),
    )
    hint_tracker = SubdirectoryHintTracker(user_id, session_id)
    run_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    goal_continuations = 0
    goal_parse_failures = 0
    run_state = HarnessRunState()
    artifact_enforcement_continuations = 0
    skill_enforcement_continuations = 0
    skill_workflow_continuations = 0
    max_skill_workflow_continuations = 8

    def queue_skill_workflow_continuation(reason: str) -> None:
        run_state.continuation_reasons.append(reason)
        requires_manifest = "manifest" in reason
        suggested_paths = [] if requires_manifest else _suggested_workflow_paths_for_reason(run_state, reason)
        suggested_line = (
            "\nSuggested skill resource paths to inspect now: " + ", ".join(suggested_paths[:12])
            if suggested_paths else ""
        )
        next_action = (
            "Your next tool call MUST be skill_view(name, file_path='__manifest__') for the relevant session skill. "
            if requires_manifest else
            "Your next tool call MUST choose at least one path from the suggested skill resource paths below "
            "and call skill_view(name, file_path=that_path). "
        )
        conversation.append({
            "role": "user",
            "content": (
                "The task uses a session-level skill for a complex deliverable, but the skill workflow "
                f"is not sufficiently inspected yet: {reason}. "
                "Before doing web search, code execution, workspace file reads, or final writing, call "
                "skill_view for the missing skill resources. "
                f"{next_action}"
                "Use skill_view for skill resources; read_file/search_files are only for workspace files."
                f"{suggested_line}\n"
                "After reading those skill resources, continue the task."
            ),
        })
    if "skill_view" in tools:
        try:
            from skills.scanner import find_all_skills
            run_state.session_skill_names = {
                str(skill.get("name"))
                for skill in find_all_skills(
                    user_id,
                    session_id,
                    enabled_user_skills=enabled_user_skills,
                )
                if skill.get("scope") == "session" and skill.get("name")
            }
        except Exception:
            logger.debug(
                "Failed to inspect session skills for user=%s session=%s",
                user_id, session_id, exc_info=True,
            )

    # ── Auto-connect MCP servers for exactly this user+session ──────────
    mcp_tool_names: list[str] = []
    try:
        from tools.mcp_client import (
            connect_all_for_user,
            get_session_tool_names,
        )
        mcp_results = await connect_all_for_user(
            user_id, session_id,
            enabled_user_skills=enabled_user_skills,
        )
        connected = [n for n, ok in mcp_results.items() if ok]
        if connected:
            logger.info(
                "Auto-connected MCP servers for user=%s session=%s: %s",
                user_id, session_id, connected,
            )
        mcp_tool_names = get_session_tool_names(user_id, session_id)
    except Exception:
        logger.exception(
            "Failed to initialize session MCP catalog for user=%s session=%s",
            user_id, session_id,
        )

    prompt_tools = list(dict.fromkeys(tools + mcp_tool_names))

    budget = IterationBudget(max_iterations)
    scrubber = StreamingThinkScrubber()

    # ── Initialize context compressor ───────────────────────────────────
    # Uses planner model (qwen3_5) as the auxiliary summarization model.
    compressor_cfg = PROVIDERS.get(settings.compressor_model, PROVIDERS.get("qwen3_5", provider))
    compressor = ContextCompressor(
        base_url=compressor_cfg["base_url"],
        api_model=compressor_cfg["api_model"],
        api_key=compressor_cfg["api_key"],
    )

    # Work on a copy so we don't mutate the caller's list.
    conversation: list[dict] = list(messages)
    original_user_text = _latest_user_text(conversation)

    # ── Image routing: pre-analyze images for text-only models ─────────
    # When the active model is text-only (deepseek_v4_pro),
    # pre-analyze any images in the last user message with qwen3_5 and
    # replace the image content with text descriptions.  The agent still
    # has vision_analyze as a tool for deeper inspection.
    # This mirrors hermes-agent's _enrich_message_with_vision().
    is_multimodal = provider.get("is_multimodal", False)
    if not is_multimodal and conversation:
        last_msg = conversation[-1]
        if last_msg.get("role") == "user":
            content = last_msg.get("content")
            # Check for image_url parts in the content
            if isinstance(content, list):
                image_urls: list[str] = []
                text_parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        img_data = part.get("image_url", {})
                        url = img_data.get("url", "") if isinstance(img_data, dict) else str(img_data or "")
                        if url:
                            image_urls.append(url)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        t = str(part.get("text", "")).strip()
                        if t:
                            text_parts.append(t)
                    elif isinstance(part, str) and part.strip():
                        text_parts.append(part.strip())

                if image_urls:
                    from agent.image_routing import enrich_message_with_vision
                    user_text = "\n".join(text_parts)
                    enriched = await enrich_message_with_vision(
                        user_text, image_urls,
                        session_id=session_id, user_id=user_id,
                    )
                    # Replace multimodal content with plain text
                    conversation[-1] = {**last_msg, "content": enriched}
                    logger.info(
                        "Image routing: enriched %d image(s) with vision descriptions "
                        "(model %s is text-only)", len(image_urls), model_id,
                    )

    # ── Build and inject system prompt ────────────────────────────────
    # Extract caller-supplied system_message if present, then replace with
    # our full three-tier system prompt (stable / context / volatile).
    caller_system_message = None
    if conversation and conversation[0].get("role") == "system":
        caller_system_message = conversation[0].get("content", "")
        # Remove the caller's bare system message — we replace it below
        conversation.pop(0)

    goal = await _fetch_goal(user_id, session_id)
    system_prompt = build_system_prompt(
        user_id=user_id,
        session_id=session_id,
        system_message=caller_system_message,
        enabled_tools=prompt_tools,
        model_id=model_id,
        provider=provider.get("provider", ""),
        workspace_context=load_workspace_context(user_id, session_id),
        goal=goal,
        enabled_user_skills=enabled_user_skills,
    )
    conversation.insert(0, {"role": "system", "content": system_prompt})

    while budget.remaining > 0:
        if not budget.consume():
            yield {"type": "error", "msg": "Agent iteration budget exhausted."}
            return

        # ── Sanitize messages before sending ──────────────────────────
        sanitized = _sanitize_messages(
            conversation,
            strip_images=not bool(provider.get("is_multimodal", False)),
        )

        # ── Build request body ────────────────────────────────────────
        tool_schemas = get_schemas(tools) if tools else []
        deferred_catalog = None
        try:
            from tools.mcp_client import get_session_tool_definitions
            mcp_definitions = get_session_tool_definitions(user_id, session_id)
            from tools.tool_search import (
                DeferredCatalog, bridge_schemas, should_defer,
            )
            if should_defer(
                mcp_definitions,
                int(provider.get("context_length") or 262144),
            ):
                deferred_catalog = DeferredCatalog.from_definitions(mcp_definitions)
                tool_schemas.extend(bridge_schemas(len(deferred_catalog.definitions)))
            else:
                tool_schemas.extend(mcp_definitions)
        except Exception:
            logger.exception(
                "Failed to build session MCP schemas for user=%s session=%s",
                user_id, session_id,
            )

        body: dict = {
            "model": api_model,
            "messages": sanitized,
            "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
            "temperature": 0.7,
            "stream": True,
        }
        if tool_schemas:
            body["tools"] = tool_schemas
        if protocol != "anthropic":
            body["stream_options"] = {"include_usage": True}

        # Qwen-specific: disable thinking by default (it's mainly a tool model)
        if (provider.get("id") or model_id) == "qwen3_5":
            body["chat_template_kwargs"] = {"enable_thinking": False}

        # ── Call LLM with retry ───────────────────────────────────────
        full_content = ""
        full_reasoning = ""
        finish_reason: Optional[str] = None
        tool_call_fragments: dict[int, dict] = {}  # index -> {id, name, arguments}
        api_usage: dict[str, int] = {}  # captured from streaming chunks

        fallback_requested = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                finish_reason = None
                tool_call_fragments.clear()
                scrubber.reset()

                request_url, request_body = _build_provider_request(
                    base_url=base_url,
                    protocol=protocol,
                    body=body,
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "POST",
                        request_url,
                        headers=headers,
                        json=request_body,
                    ) as resp:
                        if resp.status_code != 200:
                            text = await resp.aread()
                            err_text = text.decode(errors="replace")[:1000]
                            raise _http_error(resp.status_code, err_text)

                        async for normalized in _iter_provider_stream(resp, protocol):
                            reasoning = normalized.get("reasoning", "")
                            if reasoning:
                                scrubbed_reasoning = scrubber.feed(reasoning)
                                if scrubbed_reasoning:
                                    full_reasoning += scrubbed_reasoning
                                    yield {
                                        "type": "reasoning_delta",
                                        "content": scrubbed_reasoning,
                                    }

                            # Regular content — scrub and emit
                            content = normalized.get("content", "")
                            if content:
                                scrubbed_content = scrubber.feed(content)
                                if scrubbed_content:
                                    full_content += scrubbed_content
                                    yield {
                                        "type": "delta",
                                        "content": scrubbed_content,
                                    }

                            # Accumulate tool call fragments
                            for tc_delta in normalized.get("tool_calls", []):
                                idx = tc_delta.get("index", 0)
                                if idx not in tool_call_fragments:
                                    tool_call_fragments[idx] = {
                                        "id": None,
                                        "name": "",
                                        "arguments": "",
                                    }
                                frag = tool_call_fragments[idx]
                                if tc_delta.get("id"):
                                    frag["id"] = tc_delta["id"]
                                fn = tc_delta.get("function", {})
                                if fn.get("name"):
                                    frag["name"] = fn["name"]
                                if fn.get("arguments"):
                                    frag["arguments"] += fn["arguments"]

                            # Check finish reason
                            fr = normalized.get("finish_reason")
                            if fr:
                                finish_reason = fr

                            # Capture token usage from streaming chunks
                            usage = normalized.get("usage")
                            if usage:
                                api_usage["prompt_tokens"] = max(
                                    api_usage.get("prompt_tokens", 0),
                                    int(usage.get("prompt_tokens", 0) or 0),
                                )
                                api_usage["completion_tokens"] = max(
                                    api_usage.get("completion_tokens", 0),
                                    int(usage.get("completion_tokens", 0) or 0),
                                )
                                api_usage["total_tokens"] = max(
                                    api_usage.get("total_tokens", 0),
                                    int(usage.get("total_tokens", 0) or 0),
                                    api_usage["prompt_tokens"] + api_usage["completion_tokens"],
                                )

                # Flush scrubber tail
                tail = scrubber.flush()
                if tail:
                    full_content += tail
                    yield {"type": "delta", "content": tail}

                # Success — break out of retry loop
                break

            except _HTTPError as e:
                # Some OpenAI-compatible servers reject stream_options even
                # though they otherwise support streaming chat completions.
                if (
                    e.status_code == 400
                    and "stream_options" in e.body.lower()
                    and "stream_options" in body
                ):
                    body.pop("stream_options", None)
                    logger.info(
                        "Provider %s rejected stream_options; retrying without it",
                        provider.get("id") or api_model,
                    )
                    continue

                # ── Image-rejection recovery ──────────────────────────────
                # If the text-only model rejects messages containing image
                # content (shouldn't happen since we pre-enrich, but safety
                # net for edge cases), strip images and retry.
                # Ported from hermes-agent/agent/conversation_loop.py.
                from agent.image_routing import is_image_rejection_error, strip_images_from_messages
                if is_image_rejection_error(e.body, e.status_code):
                    stripped = strip_images_from_messages(conversation)
                    if stripped:
                        sanitized = _sanitize_messages(
                            conversation, strip_images=True
                        )
                        body["messages"] = sanitized
                        logger.warning(
                            "Model %s rejected image content — stripped images, retrying",
                            model_id,
                        )
                        continue

                classified = classify_api_error(e, provider=model_id, model=api_model)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s — %s",
                    attempt, MAX_RETRIES, classified.reason.value, classified.message[:200],
                )
                if (
                    (not classified.retryable or attempt >= MAX_RETRIES)
                    and not full_content
                    and provider_cursor + 1 < len(provider_chain)
                ):
                    previous = provider_chain[provider_cursor]
                    provider_cursor += 1
                    provider = provider_chain[provider_cursor]
                    base_url, api_model, api_key, protocol, headers = apply_provider(provider)
                    tool_context = ToolContext(
                        user_id=user_id,
                        session_id=session_id,
                        model_id=str(provider.get("id") or api_model),
                        provider_config=provider,
                        fallback_configs=tuple(provider_chain[provider_cursor + 1:]),
                        enabled_tools=tuple(tools),
                        source=source,
                        enabled_user_skills=tuple(enabled_user_skills or []),
                    )
                    yield {
                        "type": "model_switch",
                        "from_model": previous.get("id") or model_id,
                        "to_model": provider.get("id") or api_model,
                        "reason": classified.reason.value,
                    }
                    fallback_requested = True
                    break
                if not classified.retryable or attempt >= MAX_RETRIES:
                    yield {
                        "type": "error",
                        "msg": f"LLM error: {classified.reason.value} — {classified.message[:300]}",
                    }
                    return
                delay = jittered_backoff(attempt)
                await asyncio.sleep(delay)

            except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                logger.warning(
                    "LLM transport error (attempt %d/%d): %s", attempt, MAX_RETRIES, e,
                )
                if (
                    attempt >= MAX_RETRIES
                    and not full_content
                    and provider_cursor + 1 < len(provider_chain)
                ):
                    previous = provider_chain[provider_cursor]
                    provider_cursor += 1
                    provider = provider_chain[provider_cursor]
                    base_url, api_model, api_key, protocol, headers = apply_provider(provider)
                    tool_context = ToolContext(
                        user_id=user_id,
                        session_id=session_id,
                        model_id=str(provider.get("id") or api_model),
                        provider_config=provider,
                        fallback_configs=tuple(provider_chain[provider_cursor + 1:]),
                        enabled_tools=tuple(tools),
                        source=source,
                        enabled_user_skills=tuple(enabled_user_skills or []),
                    )
                    yield {
                        "type": "model_switch",
                        "from_model": previous.get("id") or model_id,
                        "to_model": provider.get("id") or api_model,
                        "reason": "transport_error",
                    }
                    fallback_requested = True
                    break
                if attempt >= MAX_RETRIES:
                    yield {
                        "type": "error",
                        "msg": f"LLM transport error after {MAX_RETRIES} attempts: {type(e).__name__}: {e}",
                    }
                    return
                delay = jittered_backoff(attempt)
                await asyncio.sleep(delay)

            except Exception as e:
                logger.exception("Unexpected error streaming LLM (attempt %d): %s", attempt, e)
                if attempt >= MAX_RETRIES:
                    yield {"type": "error", "msg": f"LLM error: {type(e).__name__}: {e}"}
                    return
                delay = jittered_backoff(attempt)
                await asyncio.sleep(delay)

        if fallback_requested:
            continue

        # ── Determine next action based on finish_reason ──────────────
        if finish_reason is None:
            finish_reason = "stop"

        # Feed token usage to context compressor
        if api_usage:
            compressor.update_from_response(api_usage)
            run_usage["input_tokens"] += int(api_usage.get("prompt_tokens", 0) or 0)
            run_usage["output_tokens"] += int(api_usage.get("completion_tokens", 0) or 0)
            run_usage["total_tokens"] += int(api_usage.get("total_tokens", 0) or 0)
        elif full_content or full_reasoning:
            estimated_input = max(1, len(json.dumps(sanitized, ensure_ascii=False)) // 4)
            estimated_output = max(1, (len(full_content) + len(full_reasoning)) // 4)
            run_usage["input_tokens"] += estimated_input
            run_usage["output_tokens"] += estimated_output
            run_usage["total_tokens"] += estimated_input + estimated_output

        if finish_reason == "stop":
            unviewed_session_skills = run_state.unviewed_session_skills()
            if (
                unviewed_session_skills
                and skill_enforcement_continuations < 1
                and (
                    _looks_like_complex_artifact_request(original_user_text)
                    or run_state.tool_call_count > 0
                    or len(full_content.strip()) < 200
                )
            ):
                skill_enforcement_continuations += 1
                skill_list = ", ".join(sorted(unviewed_session_skills))
                conversation.append({
                    "role": "assistant",
                    "content": full_content or "(No visible response.)",
                })
                conversation.append({
                    "role": "user",
                    "content": (
                        "A session-level skill is available for this conversation but has not been loaded: "
                        f"{skill_list}. If any listed skill is relevant to the user's task, call "
                        "skill_view(name) before continuing, then follow the skill workflow. "
                        "If none is relevant, state the concrete reason and continue."
                    ),
                })
                yield {
                    "type": "tool_progress",
                    "msg": f"↻ Loading relevant session skill before continuing — {skill_list}",
                }
                yield {"type": "delta", "content": "\n\n"}
                continue

            needs_artifact_gate = bool(
                run_state.viewed_skill_names
                and _looks_like_complex_artifact_request(original_user_text)
            )
            needs_workflow_gate, workflow_reason = run_state.needs_more_skill_workflow()
            if (
                needs_artifact_gate
                and needs_workflow_gate
                and skill_workflow_continuations < max_skill_workflow_continuations
            ):
                skill_workflow_continuations += 1
                conversation.append({
                    "role": "assistant",
                    "content": full_content or "(No visible response.)",
                })
                queue_skill_workflow_continuation(workflow_reason)
                yield {
                    "type": "tool_progress",
                    "msg": f"↻ Inspecting session skill workflow before finishing — {workflow_reason}",
                }
                yield {"type": "delta", "content": "\n\n"}
                continue
            if needs_artifact_gate and artifact_enforcement_continuations < 1:
                largest_write = max(run_state.successful_write_sizes or [0])
                if run_state.tool_error_count >= 2 or (largest_write and largest_write < 20_000) or (
                    not largest_write and len(full_content.strip()) < 8_000
                ):
                    artifact_enforcement_continuations += 1
                    conversation.append({
                        "role": "assistant",
                        "content": full_content or "(No visible response.)",
                    })
                    conversation.append({
                        "role": "user",
                        "content": (
                            "The task uses a session-level skill and asks for a complex deliverable. "
                            "Continue once to complete the artifact using the loaded skill workflow and any "
                            "relevant linked files. If tool calls failed validation, retry with valid schema "
                            "arguments. Produce one coherent final artifact with the required major sections; "
                            "do not stop at a short summary, placeholder, or incomplete file."
                        ),
                    })
                    yield {
                        "type": "tool_progress",
                        "msg": "↻ Completing session-skill artifact before finishing",
                    }
                    yield {"type": "delta", "content": "\n\n"}
                    continue

            current_goal = await _fetch_goal(user_id, session_id)
            if current_goal and current_goal.get("status") == "active":
                token_budget = int(current_goal.get("token_budget") or 0)
                tokens_used = int(current_goal.get("tokens_used") or 0)
                if token_budget and tokens_used + run_usage["total_tokens"] >= token_budget:
                    await _set_goal_status(
                        user_id,
                        session_id,
                        "budget_limited",
                        "Goal token budget reached.",
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": "⏸ Goal paused — token budget reached.",
                    }
                    yield {
                        "type": "usage",
                        **run_usage,
                        "model": provider.get("id") or api_model,
                    }
                    yield {"type": "done", "finish_reason": "stop"}
                    return
                verdict, reason, parse_failed = await _judge_goal(
                    str(current_goal.get("objective") or ""),
                    full_content,
                )
                if parse_failed:
                    goal_parse_failures += 1
                else:
                    goal_parse_failures = 0
                if verdict == "complete":
                    await _set_goal_status(
                        user_id, session_id, "complete", reason,
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": f"🎯 Goal complete — {reason}",
                    }
                elif verdict == "blocked":
                    await _set_goal_status(
                        user_id, session_id, "blocked", reason,
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": f"⛔ Goal blocked — {reason}",
                    }
                elif goal_parse_failures >= settings.goal_max_parse_failures:
                    await _set_goal_status(
                        user_id,
                        session_id,
                        "pause",
                        "Goal judge repeatedly returned invalid output.",
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": "⏸ Goal paused — judge output could not be parsed.",
                    }
                elif goal_continuations < max(0, settings.goal_max_continuations):
                    goal_continuations += 1
                    conversation.append({
                        "role": "assistant",
                        "content": full_content or "(No visible response.)",
                    })
                    conversation.append({
                        "role": "user",
                        "content": (
                            "[Continuing toward your standing goal]\n"
                            f"Goal: {current_goal['objective']}\n\n"
                            f"Judge feedback: {reason}\n\n"
                            "Continue working toward this goal. Take the next "
                            "concrete step, use tools when needed, and verify the "
                            "result. If complete, state concrete evidence. If "
                            "blocked and user input is required, say so clearly."
                        ),
                    })
                    yield {
                        "type": "tool_progress",
                        "msg": (
                            f"↻ Continuing toward goal "
                            f"({goal_continuations}/{settings.goal_max_continuations})"
                            f" — {reason}"
                        ),
                    }
                    yield {"type": "delta", "content": "\n\n"}
                    continue
                else:
                    await _set_goal_status(
                        user_id,
                        session_id,
                        "pause",
                        "Automatic continuation budget reached.",
                    )
                    yield {
                        "type": "tool_progress",
                        "msg": (
                            "⏸ Goal paused — automatic continuation budget reached."
                        ),
                    }
            yield {
                "type": "usage",
                **run_usage,
                "model": provider.get("id") or api_model,
            }
            yield {"type": "done", "finish_reason": "stop"}
            return

        if finish_reason == "tool_calls" and tool_call_fragments:
            # Assemble tool calls from accumulated fragments
            sorted_frags = sorted(tool_call_fragments.items())
            assembled_calls = []
            for _, frag in sorted_frags:
                assembled_calls.append(
                    build_tool_call(
                        id=frag["id"],
                        name=frag["name"],
                        arguments=_safe_parse_args(frag["arguments"]),
                    )
                )

            # Build the assistant message with tool_calls
            assistant_msg = {
                "role": "assistant",
                "content": full_content or None,
                "tool_calls": [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for i, tc in enumerate(assembled_calls)
                ],
            }
            conversation.append(assistant_msg)

            # Execute each tool and append results
            for tc in assembled_calls:
                run_state.tool_call_count += 1
                args_summary = tc.arguments[:80] if tc.arguments else "{}"
                display_tool_name = tc.name
                yield {
                    "type": "tool_progress",
                    "msg": f"🔧 {tc.name}({args_summary})",
                }

                args = _safe_parse_args(tc.arguments or "")
                if isinstance(args, dict) and "__tool_arg_parse_error" in args:
                    run_state.parse_failure_count += 1
                executed_args = args

                if tc.name == "tool_search" and deferred_catalog is not None:
                    result = json.dumps(
                        deferred_catalog.search(
                            str(args.get("query", "")),
                            int(args.get("limit", 5)),
                        ),
                        ensure_ascii=False,
                    )
                elif tc.name == "tool_describe" and deferred_catalog is not None:
                    result = json.dumps(
                        deferred_catalog.describe(str(args.get("name", ""))),
                        ensure_ascii=False,
                    )
                elif tc.name == "tool_call" and deferred_catalog is not None:
                    display_tool_name = str(args.get("name", ""))
                    actual_args = args.get("arguments")
                    if not isinstance(actual_args, dict):
                        actual_args = {}
                    executed_args = actual_args
                    if display_tool_name not in deferred_catalog.definitions:
                        result = json.dumps({
                            "error": f"Deferred tool not found: {display_tool_name}"
                        })
                    elif display_tool_name in ("mcp_server_list", "mcp_server_status"):
                        result = await dispatch(
                            display_tool_name,
                            actual_args,
                            context=tool_context,
                        )
                    elif display_tool_name.startswith("mcp_"):
                        from tools.mcp_client import dispatch_mcp_tool
                        result = await dispatch_mcp_tool(
                            display_tool_name,
                            actual_args,
                            user_id=user_id,
                            session_id=session_id,
                            enabled_user_skills=list(tool_context.enabled_user_skills),
                        )
                    else:
                        result = await dispatch(
                            display_tool_name,
                            actual_args,
                            context=tool_context,
                        )
                elif tc.name in ("mcp_server_list", "mcp_server_status"):
                    result = await dispatch(
                        tc.name,
                        args,
                        context=tool_context,
                    )
                elif tc.name.startswith("mcp_"):
                    from tools.mcp_client import dispatch_mcp_tool
                    result = await dispatch_mcp_tool(
                        tc.name,
                        args,
                        user_id=user_id,
                        session_id=session_id,
                        enabled_user_skills=list(tool_context.enabled_user_skills),
                    )
                else:
                    result = await dispatch(
                        tc.name,
                        args,
                        context=tool_context,
                    )
                hint = hint_tracker.check(args)
                if hint:
                    result = str(result) + "\n\n" + hint
                outcome, outcome_detail = _tool_outcome_summary(str(result))
                if outcome != "success":
                    run_state.tool_error_count += 1
                    if isinstance(executed_args, dict) and "__tool_arg_parse_error" in executed_args:
                        run_state.parse_failure_count += 1
                    if "schema" in outcome_detail.lower() or "required field" in outcome_detail.lower():
                        run_state.schema_failure_count += 1
                elif display_tool_name == "write_file":
                    written_size = _tool_result_size(str(result))
                    if written_size is not None:
                        run_state.successful_write_sizes.append(written_size)
                if display_tool_name == "skill_view" and outcome == "success" and isinstance(executed_args, dict):
                    run_state.record_skill_view(executed_args, _json_object(str(result)))
                logger.info(
                    "Tool completed user=%s session=%s tool=%s outcome=%s detail=%s",
                    user_id, session_id, display_tool_name, outcome, outcome_detail[:300],
                )
                marker = "✅" if outcome == "success" else "⚠️"
                progress = f"{marker} {display_tool_name}: {outcome}"
                if outcome_detail:
                    progress += f" — {outcome_detail[:240]}"
                yield {"type": "tool_progress", "msg": progress}
                wrapped = wrap_result(str(result), display_tool_name, user_id=user_id, session_id=session_id)
                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc.id or f"call_unknown",
                    "content": wrapped,
                })

            needs_artifact_gate_after_tools = bool(
                run_state.viewed_skill_names
                and _looks_like_complex_artifact_request(original_user_text)
            )
            needs_workflow_gate_after_tools, workflow_reason_after_tools = run_state.needs_more_skill_workflow()
            if (
                needs_artifact_gate_after_tools
                and needs_workflow_gate_after_tools
                and skill_workflow_continuations < max_skill_workflow_continuations
            ):
                skill_workflow_continuations += 1
                queue_skill_workflow_continuation(workflow_reason_after_tools)
                yield {
                    "type": "tool_progress",
                    "msg": f"↻ Inspecting session skill workflow before continuing — {workflow_reason_after_tools}",
                }

            # ── Context compression check ────────────────────────────
            if compressor.should_compress():
                logger.info("Context compression triggered (prompt_tokens=%d threshold=%d)",
                           compressor.last_prompt_tokens, compressor.threshold_tokens)
                conversation = await compressor.compress(conversation)

            # Continue the loop — LLM will process tool results
            continue

        if finish_reason == "length":
            # Inject continuation prompt and loop
            conversation.append({
                "role": "user",
                "content": "Please continue from where you left off. Complete your response.",
            })

            # ── Context compression check ────────────────────────────
            if compressor.should_compress():
                logger.info("Context compression triggered (prompt_tokens=%d threshold=%d)",
                           compressor.last_prompt_tokens, compressor.threshold_tokens)
                conversation = await compressor.compress(conversation)

            continue

        # content_filter or other — treat as stop
        yield {
            "type": "usage",
            **run_usage,
            "model": provider.get("id") or api_model,
        }
        yield {"type": "done", "finish_reason": finish_reason}
        return


async def _fetch_goal(user_id: str, session_id: str) -> dict | None:
    if not user_id or not session_id or session_id == "default":
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{settings.backend_internal_url.rstrip('/')}/internal/sessions/{session_id}/goal",
                params={"user_id": user_id},
                headers={"X-Internal-Token": settings.internal_api_token},
            )
        if response.status_code == 200:
            data = response.json()
            return data if data.get("objective") else None
    except Exception:
        logger.debug("Goal lookup failed for session=%s", session_id, exc_info=True)
    return None


async def _set_goal_status(
    user_id: str,
    session_id: str,
    action: str,
    note: str,
) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{settings.backend_internal_url.rstrip('/')}/internal/sessions/{session_id}/goal",
                params={"user_id": user_id},
                headers={"X-Internal-Token": settings.internal_api_token},
                json={"action": action, "note": note[:1000]},
            )
    except Exception:
        logger.debug(
            "Goal status update failed session=%s action=%s",
            session_id, action, exc_info=True,
        )


async def _judge_goal(
    objective: str,
    last_response: str,
) -> tuple[str, str, bool]:
    """Return (complete|blocked|continue, reason, parse_failed)."""
    if not objective.strip():
        return "continue", "empty goal", False
    if not last_response.strip():
        return "continue", "the agent produced no substantive response", False
    judge_cfg = PROVIDERS.get(
        settings.compressor_model,
        PROVIDERS.get("qwen3_5"),
    )
    if not judge_cfg:
        return "continue", "goal judge model is unavailable", False
    system_prompt = (
        "You are a strict completion judge for an autonomous agent. Decide "
        "whether the latest response proves the user's durable goal is complete, "
        "is genuinely blocked on user/external input, or still needs work. "
        "Generic claims are not evidence. Reply with one JSON object only: "
        '{"status":"complete|blocked|continue","reason":"one sentence"}'
    )
    user_prompt = (
        f"Goal:\n{objective[:2000]}\n\n"
        f"Latest agent response:\n{last_response[-4000:]}\n\n"
        "Judge the current state."
    )
    headers = {"Content-Type": "application/json"}
    key = judge_cfg.get("api_key")
    if key and key != "EMPTY":
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                f"{str(judge_cfg['base_url']).rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": judge_cfg["api_model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 180,
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            )
        response.raise_for_status()
        raw = ""
        resp_data = response.json()
        choices = resp_data.get("choices")
        if choices:
            raw = choices[0].get("message", {}).get("content", "")
    except Exception as exc:
        logger.info("Goal judge failed: %s", exc)
        return "continue", f"goal judge unavailable: {type(exc).__name__}", False
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        return "continue", "goal judge reply was not valid JSON", True
    status = str(data.get("status") or "").strip().lower()
    if not status and "done" in data:
        status = "complete" if bool(data.get("done")) else "continue"
    if status not in {"complete", "blocked", "continue"}:
        return "continue", "goal judge returned an unknown status", True
    reason = str(data.get("reason") or "no reason provided").strip()
    return status, reason[:500], False


def _build_provider_request(
    *,
    base_url: str,
    protocol: str,
    body: dict,
) -> tuple[str, dict]:
    if protocol != "anthropic":
        return f"{base_url.rstrip('/')}/chat/completions", body

    system_parts: list[str] = []
    converted: list[dict] = []

    def append_message(role: str, content: Any) -> None:
        if converted and converted[-1]["role"] == role:
            existing = converted[-1]["content"]
            if not isinstance(existing, list):
                existing = [{"type": "text", "text": str(existing)}]
            incoming = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
            converted[-1]["content"] = existing + incoming
        else:
            converted.append({"role": role, "content": content})

    for message in body.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if content:
                system_parts.append(str(content))
            continue
        if role == "assistant":
            blocks: list[dict] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    parsed_args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tool_call.get("id") or "tool_call",
                    "name": fn.get("name") or "unknown",
                    "input": parsed_args if isinstance(parsed_args, dict) else {},
                })
            append_message("assistant", blocks or [{"type": "text", "text": ""}])
            continue
        if role == "tool":
            append_message("user", [{
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or "tool_call",
                "content": str(content or ""),
            }])
            continue
        if isinstance(content, list):
            blocks = []
            for part in content:
                if not isinstance(part, dict):
                    blocks.append({"type": "text", "text": str(part)})
                elif part.get("type") == "text":
                    blocks.append({"type": "text", "text": str(part.get("text", ""))})
                elif part.get("type") == "image_url":
                    image = part.get("image_url") or {}
                    url = image.get("url", "") if isinstance(image, dict) else str(image)
                    if url.startswith("data:") and ";base64," in url:
                        header, data = url.split(",", 1)
                        media_type = header[5:].split(";", 1)[0] or "image/png"
                        blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        })
                    elif url:
                        blocks.append({
                            "type": "image",
                            "source": {"type": "url", "url": url},
                        })
            append_message("user", blocks or [{"type": "text", "text": ""}])
        else:
            append_message("user", str(content or ""))

    anthropic_tools = []
    for tool in body.get("tools") or []:
        fn = tool.get("function") or {}
        anthropic_tools.append({
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    request_body = {
        "model": body["model"],
        "messages": converted,
        "max_tokens": body.get("max_tokens", DEFAULT_MAX_TOKENS),
        "temperature": body.get("temperature", 0.7),
        "stream": True,
    }
    if system_parts:
        request_body["system"] = "\n\n".join(system_parts)
    if anthropic_tools:
        request_body["tools"] = anthropic_tools
    return f"{base_url.rstrip('/')}/messages", request_body


async def _iter_provider_stream(
    response: httpx.Response,
    protocol: str,
) -> AsyncIterator[dict]:
    if protocol != "anthropic":
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            chunk = line[6:]
            if chunk == "[DONE]":
                break
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            choices = data.get("choices")
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta", {})
            yield {
                "reasoning": delta.get("reasoning") or delta.get("reasoning_content") or "",
                "content": delta.get("content") or "",
                "tool_calls": delta.get("tool_calls") or [],
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage"),
            }
        return

    input_tokens = 0
    output_tokens = 0
    tool_blocks: dict[int, dict] = {}
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        raw = line[6:]
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "message_start":
            input_tokens = int((event.get("message", {}).get("usage") or {}).get("input_tokens", 0) or 0)
            yield {
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": 0,
                    "total_tokens": input_tokens,
                }
            }
        elif event_type == "content_block_start":
            index = int(event.get("index", 0))
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                tool_blocks[index] = {
                    "id": block.get("id"),
                    "name": block.get("name", ""),
                }
                yield {
                    "tool_calls": [{
                        "index": index,
                        "id": block.get("id"),
                        "function": {"name": block.get("name", ""), "arguments": ""},
                    }]
                }
            elif block.get("type") == "text" and block.get("text"):
                yield {"content": block.get("text", "")}
        elif event_type == "content_block_delta":
            index = int(event.get("index", 0))
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                yield {"content": delta.get("text", "")}
            elif delta_type == "thinking_delta":
                yield {"reasoning": delta.get("thinking", "")}
            elif delta_type == "input_json_delta":
                block = tool_blocks.get(index, {})
                yield {
                    "tool_calls": [{
                        "index": index,
                        "id": block.get("id"),
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": delta.get("partial_json", ""),
                        },
                    }]
                }
        elif event_type == "message_delta":
            output_tokens = int((event.get("usage") or {}).get("output_tokens", output_tokens) or 0)
            stop_reason = (event.get("delta") or {}).get("stop_reason")
            reason_map = {
                "end_turn": "stop",
                "stop_sequence": "stop",
                "tool_use": "tool_calls",
                "max_tokens": "length",
                "refusal": "content_filter",
            }
            yield {
                "finish_reason": reason_map.get(stop_reason, stop_reason),
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }


def _tool_outcome_summary(raw: str) -> tuple[str, str]:
    """Return an auditable status line without exposing a large tool payload."""
    data = _json_object(raw)
    if data is None:
        return "success", ""
    if data.get("error"):
        return "error", str(data["error"])
    status = str(data.get("status", "")).lower()
    if status in {"error", "blocked", "timeout", "failed"}:
        return status, str(data.get("error") or "")
    if data.get("success") is False:
        return "error", str(data.get("message") or data.get("detail") or "")
    return "success", ""


def _tool_result_size(raw: str) -> int | None:
    data = _json_object(raw)
    if data is None:
        return None
    size = data.get("size")
    return size if isinstance(size, int) and size >= 0 else None


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _latest_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
    return ""


def _looks_like_complex_artifact_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "完整", "综合", "详细", "全面", "计划", "方案", "设计", "报告",
        "development plan", "clinical", "trial", "phase i", "phase ii", "phase iii",
        "regulatory", "simulation", "分析", "策略", "白皮书", "proposal",
    )
    return sum(1 for marker in markers if marker in lowered) >= 2


# ── Internal helpers ────────────────────────────────────────────────────


class _HTTPError(Exception):
    """Wrapper for non-200 HTTP responses from the LLM endpoint."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]}")


def _http_error(status_code: int, body: str) -> _HTTPError:
    return _HTTPError(status_code, body)


def _sanitize_messages(
    messages: list[dict],
    *,
    strip_images: bool = True,
) -> list[dict]:
    """Strip internal ``_``-prefixed keys, ``tool_name``, and image_url parts.

    Image stripping is a safety net for text-only models — mirrors
    hermes-agent's ``_prepare_messages_for_non_vision_model``.

    Returns a shallow copy with stripped keys so the original is not mutated.
    """
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            cleaned.append(msg)
            continue
        # Strip internal marker keys
        entry = {
            k: v
            for k, v in msg.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
        # Remove tool_name from tool messages
        entry.pop("tool_name", None)
        # Clean tool_calls entries
        tcs = entry.get("tool_calls")
        if isinstance(tcs, list):
            clean_tcs = []
            for tc in tcs:
                if isinstance(tc, dict):
                    tc = {k: v for k, v in tc.items() if k not in ("call_id", "response_item_id")}
                clean_tcs.append(tc)
            entry["tool_calls"] = clean_tcs
        # ── Safety net: strip image_url parts from content ──────────
        # Text-only models (deepseek_v4_pro) reject messages
        # containing image_url content parts. The pre-enrichment in
        # run_stream() should have already converted images to text, but
        # this catches any edge cases (e.g. tool results with images).
        content = entry.get("content")
        if strip_images and isinstance(content, list):
            text_only = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    continue
                text_only.append(part)
            if len(text_only) != len(content):
                if text_only:
                    entry["content"] = text_only
                elif entry.get("role") == "tool":
                    entry["content"] = "[image content removed — server does not support images]"
                else:
                    entry["content"] = ""
        cleaned.append(entry)
    return cleaned


def _safe_parse_args(raw: str):
    """Parse tool call arguments JSON without letting malformed args reach handlers."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        repaired = _extract_json_object(raw)
        if repaired is not None:
            return repaired
        return {
            "__tool_arg_parse_error": f"{exc.msg} at char {exc.pos}",
            "__raw_args_preview": raw[:500],
        }
    if isinstance(parsed, dict):
        return parsed
    return {
        "__tool_arg_parse_error": f"tool arguments must be a JSON object, got {type(parsed).__name__}",
        "__raw_args_preview": str(parsed)[:500],
    }


def _extract_json_object(raw: str) -> dict | None:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
