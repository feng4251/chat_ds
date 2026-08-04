"""OpenAI Chat Completions transport — adapted for vLLM endpoints.

Handles message sanitization, kwargs building, and response normalization
for the three vLLM provider endpoints used by chat_ds harness.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from provider_transcript import (
    audit_provider_transcript,
    project_unique_tool_call_ids,
)
from transports.base import (
    NormalizedResponse,
    ProviderTransport,
    ToolCall,
    Usage,
)


class ChatCompletionsTransport(ProviderTransport):
    """Transport for OpenAI-compatible chat completions (vLLM endpoints)."""

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(
        self, messages: List[Dict[str, Any]], **kwargs
    ) -> List[Dict[str, Any]]:
        """Strip internal fields that vLLM endpoints reject.

        Removes ``_``-prefixed keys, ``tool_name`` on tool-result messages,
        and Codex-specific fields that don't belong in OpenAI format.
        """
        needs_sanitize = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if "tool_name" in msg:
                needs_sanitize = True
                break
            if any(isinstance(k, str) and k.startswith("_") for k in msg):
                needs_sanitize = True
                break
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and (
                        "call_id" in tc or "response_item_id" in tc
                    ):
                        needs_sanitize = True
                        break
                if needs_sanitize:
                    break

        if not needs_sanitize:
            return messages

        sanitized = copy.deepcopy(messages)
        for msg in sanitized:
            if not isinstance(msg, dict):
                continue
            msg.pop("tool_name", None)
            for key in [
                k for k in msg if isinstance(k, str) and k.startswith("_")
            ]:
                msg.pop(key, None)
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        tc.pop("call_id", None)
                        tc.pop("response_item_id", None)
        return sanitized

    def convert_tools(
        self, tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Tools are already in OpenAI format — identity."""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """Build chat.completions.create() kwargs for vLLM endpoints.

        Extra params:
            max_tokens: int
            temperature: float
            timeout: float
            extra_body: dict | None (e.g. chat_template_kwargs for Qwen)
        """
        sanitized = self.convert_messages(messages)
        sanitized, _id_projection = project_unique_tool_call_ids(sanitized)
        transcript_audit = audit_provider_transcript(sanitized)
        if not transcript_audit.valid:
            raise ValueError(
                "refusing invalid provider transcript: "
                f"{transcript_audit.as_dict()}"
            )

        api_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        if tools:
            api_kwargs["tools"] = self.convert_tools(tools)

        max_tokens = params.get("max_tokens")
        if max_tokens is not None:
            api_kwargs["max_tokens"] = max_tokens

        temperature = params.get("temperature")
        if temperature is not None:
            api_kwargs["temperature"] = temperature

        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        extra_body = params.get("extra_body")
        if extra_body:
            api_kwargs["extra_body"] = extra_body

        return api_kwargs

    def normalize_response(
        self, response: Any, **kwargs
    ) -> NormalizedResponse:
        """Normalize an OpenAI ChatCompletion response to NormalizedResponse."""
        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    )
                )

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        reasoning = getattr(msg, "reasoning", None)
        reasoning_content = getattr(msg, "reasoning_content", None)

        provider_data: Dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content

        return NormalizedResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        if not response.choices:
            return False
        return True
