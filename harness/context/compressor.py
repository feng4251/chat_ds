"""Automatic context window compression for long conversations.

Adapted from hermes-agent/agent/context_compressor.py.

Key changes from hermes:
  - Async HTTP calls via httpx instead of sync call_llm()
  - No redaction (we don't handle secrets)
  - Simplified token estimation (chars / 4)
  - Shares multimodal-aware token budgeting with the provider request path
  - No summary-model fallback (single model)
  - Retains core algorithm: prune tool results, token-budget tail protection,
    structured summary template, iterative updates
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from context.engine import ContextEngine
from context.token_estimator import estimate_message_tokens, is_image_content_part
from provider_transcript import (
    align_tool_round_boundary,
    audit_provider_transcript,
    canonicalize_legacy_provider_transcript,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Summary prefix — tells the model how to interpret compacted context
# ---------------------------------------------------------------------------

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Respond ONLY to the latest user message that appears AFTER this "
    "summary — that message is the single source of truth for what to do "
    "right now. "
    "If the latest user message is consistent with the '## Active Task' "
    "section, you may use the summary as background. If the latest user "
    "message contradicts, supersedes, changes topic from, or in any way "
    "diverges from '## Active Task' / '## In Progress' / '## Pending User "
    "Asks' / '## Remaining Work', the latest message WINS — discard those "
    "stale items entirely. "
    "IMPORTANT: Your persistent memory in the system prompt is ALWAYS "
    "authoritative and active — never ignore or deprioritize memory content "
    "due to this compaction note. "
    "The current session state (files, config, etc.) may reflect work "
    "described here — avoid repeating it:"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_SUMMARY_TOKENS = 2000
_SUMMARY_RATIO = 0.20
_SUMMARY_TOKENS_CEILING = 12_000
_CHARS_PER_TOKEN = 4

_DEFAULT_CONTEXT_LENGTH = 131_072  # Default for models without known context length

# Truncation limits for summarizer input
_CONTENT_MAX = 6000
_CONTENT_HEAD = 4000
_CONTENT_TAIL = 1500
_IMAGE_METADATA_TEXT_MAX = 500
_IMAGE_METADATA_ITEMS_MAX = 30

# Canonical Skill instructions are loader-owned tool data, not summary text.
# Reserve a separate, context-derived allowance for exact skill_view pairs so
# compression can never silently trade instruction integrity for a smaller
# summary.  The ceiling prevents a collection of Skills from consuming an
# unbounded share of a large model context.
_PROTECTED_SKILL_CONTEXT_RATIO = 0.20
_PROTECTED_SKILL_TOKENS_FLOOR = 2_000
_PROTECTED_SKILL_TOKENS_CEILING = 32_000
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    """Rough token count: chars / 4."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Use the same multimodal-aware estimate as the provider request path."""
    return sum(estimate_message_tokens(msg) for msg in messages)


def _content_length_for_budget(raw_content: Any) -> int:
    """Return effective char-length of message content for token budgeting."""
    if isinstance(raw_content, str):
        return len(raw_content)
    if not isinstance(raw_content, list):
        return len(str(raw_content or ""))
    total = 0
    for p in raw_content:
        if isinstance(p, str):
            total += len(p)
        elif isinstance(p, dict):
            total += len(p.get("text", "") or "")
        else:
            total += len(str(p))
    return total


def _bounded_image_metadata(value: Any, *, depth: int = 0) -> Any:
    """Keep useful image metadata while removing transport bytes and URLs."""
    if depth >= 4:
        return "[metadata depth omitted]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _IMAGE_METADATA_ITEMS_MAX:
                result["_truncated"] = True
                break
            key = str(raw_key)
            if key.casefold() in {"data", "url"}:
                continue
            if key.casefold() == "image_url" and isinstance(item, str):
                result[key] = "[image transport omitted]"
                continue
            result[key] = _bounded_image_metadata(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_image_metadata(item, depth=depth + 1)
            for item in list(value)[:_IMAGE_METADATA_ITEMS_MAX]
        ]
    if isinstance(value, str):
        if value.lstrip().casefold().startswith("data:image/"):
            return "[image transport omitted]"
        if len(value) > _IMAGE_METADATA_TEXT_MAX:
            return value[:_IMAGE_METADATA_TEXT_MAX] + "... [metadata truncated]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_IMAGE_METADATA_TEXT_MAX]


def _serialize_content_for_summary(raw_content: Any) -> str:
    """Render multimodal content without ever copying image transport data."""
    if isinstance(raw_content, str):
        return raw_content
    if is_image_content_part(raw_content):
        metadata = _bounded_image_metadata(raw_content)
        return "[IMAGE OMITTED; metadata=" + json.dumps(
            metadata,
            ensure_ascii=False,
            default=str,
        ) + "]"
    if isinstance(raw_content, list):
        rendered: list[str] = []
        for part in raw_content:
            if isinstance(part, str):
                rendered.append(part)
            elif is_image_content_part(part):
                rendered.append(_serialize_content_for_summary(part))
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                # Preserve ordinary multimodal text exactly; its surrounding
                # type metadata does not add useful context to the summary.
                rendered.append(part["text"])
            else:
                rendered.append(json.dumps(
                    _bounded_image_metadata(part),
                    ensure_ascii=False,
                    default=str,
                ))
        return "\n".join(rendered)
    if isinstance(raw_content, dict):
        return json.dumps(
            _bounded_image_metadata(raw_content),
            ensure_ascii=False,
            default=str,
        )
    return str(raw_content or "")


# ---------------------------------------------------------------------------
# Canonical Skill activation ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CanonicalSkillViewReceipt:
    """One integrity-addressed main SKILL.md tool-call/result pair."""

    skill_name: str
    document_sha256: str
    offset: int
    next_offset: int | None
    has_more: bool
    total_chars: int
    is_paged: bool
    content: str
    assistant_index: int
    call_position: int
    result_index: int
    tool_call: Any
    tool_result: dict[str, Any]

    @property
    def occurrence(self) -> tuple[int, int, int]:
        return (self.result_index, self.assistant_index, self.call_position)


def _tool_call_identity(tool_call: Any) -> tuple[str, str, str] | None:
    """Return (id, name, arguments-json) for one native tool call."""
    if isinstance(tool_call, dict):
        call_id = str(
            tool_call.get("call_id") or tool_call.get("id") or ""
        )
        function = tool_call.get("function")
        if not isinstance(function, dict):
            return None
        name = function.get("name")
        arguments = function.get("arguments")
    else:
        call_id = str(
            getattr(tool_call, "call_id", None)
            or getattr(tool_call, "id", None)
            or ""
        )
        function = getattr(tool_call, "function", None)
        name = getattr(function, "name", None) if function is not None else None
        arguments = (
            getattr(function, "arguments", None)
            if function is not None else None
        )
    if not call_id or not isinstance(name, str) or not isinstance(arguments, str):
        return None
    return call_id, name, arguments


def _strict_json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _coherent_receipt_field(
    result: dict[str, Any],
    pagination: dict[str, Any] | None,
    field: str,
) -> tuple[bool, bool, Any]:
    """Read duplicated top-level/pagination metadata without ambiguity."""
    values: list[Any] = []
    if field in result:
        values.append(result[field])
    if pagination is not None and field in pagination:
        values.append(pagination[field])
    if not values:
        return True, False, None
    first = values[0]
    if any(type(value) is not type(first) or value != first for value in values[1:]):
        return False, True, None
    return True, True, first


def _validated_canonical_skill_receipt(
    *,
    assistant_index: int,
    call_position: int,
    tool_call: Any,
    result_index: int,
    tool_result: dict[str, Any],
) -> _CanonicalSkillViewReceipt | None:
    """Validate a successful, integrity-addressed main SKILL.md read.

    Supporting resources deliberately fail this predicate.  The tool result
    must be one complete JSON object; persisted/truncated pointer wrappers and
    compacted-history records therefore cannot masquerade as an activation.
    """
    identity = _tool_call_identity(tool_call)
    if identity is None:
        return None
    _, tool_name, raw_arguments = identity
    if tool_name != "skill_view":
        return None
    arguments = _strict_json_object(raw_arguments)
    if arguments is None:
        return None

    skill_name = arguments.get("name")
    if not isinstance(skill_name, str) or not skill_name.strip():
        return None
    skill_name = skill_name.strip()
    if "filepath" in arguments:
        return None
    requested_path = arguments.get("file_path")
    if requested_path not in (None, "", "SKILL.md"):
        return None
    requested_offset = arguments.get("offset", 0)
    if (
        not isinstance(requested_offset, int)
        or isinstance(requested_offset, bool)
        or requested_offset < 0
    ):
        return None
    requested_limit = arguments.get("limit")
    if requested_limit is not None and (
        not isinstance(requested_limit, int)
        or isinstance(requested_limit, bool)
        or requested_limit <= 0
    ):
        return None
    if requested_path in (None, "") and (
        requested_offset != 0 or "offset" in arguments or "limit" in arguments
    ):
        return None

    result = _strict_json_object(tool_result.get("content"))
    if result is None or result.get("success") is not True:
        return None
    if result.get("error") not in (None, ""):
        return None
    # These are model-history/persistence receipts, not literal skill_view
    # results.  Do not follow a pointer while compacting model context.
    if any(
        key in result
        for key in (
            "history_result_path",
            "history_result_chars",
            "content_omitted",
            "_chatds_argument_omitted",
        )
    ):
        return None
    if result.get("name") != skill_name or result.get("is_binary") is True:
        return None
    content = result.get("content")
    if not isinstance(content, str) or not content:
        return None

    response_file = result.get("file")
    if requested_path == "SKILL.md":
        if response_file != "SKILL.md":
            return None
    elif response_file not in (None, "SKILL.md"):
        return None

    skill_digest = result.get("skill_md_sha256")
    resource_digest = result.get("sha256")
    for value in (skill_digest, resource_digest):
        if value is not None and (
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        ):
            return None
    if requested_path == "SKILL.md":
        digest = resource_digest
    else:
        digest = skill_digest
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        return None
    digests = [
        value.casefold()
        for value in (skill_digest, resource_digest)
        if isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None
    ]
    if not digests or any(value != digests[0] for value in digests[1:]):
        return None
    digest = digest.casefold()

    pagination_value = result.get("pagination")
    if pagination_value is not None and not isinstance(pagination_value, dict):
        return None
    pagination = pagination_value if isinstance(pagination_value, dict) else None
    is_paged = (
        pagination is not None
        or result.get("main_document_paged") is True
        or any(
            field in result
            for field in (
                "offset", "returned_chars", "total_chars", "next_offset", "has_more"
            )
        )
    )

    if is_paged:
        if (
            pagination is None
            or pagination.get("unit") != "unicode_codepoints"
        ):
            return None
        fields: dict[str, Any] = {}
        for field in (
            "offset", "limit", "returned_chars", "total_chars", "next_offset", "has_more"
        ):
            coherent, present, value = _coherent_receipt_field(
                result, pagination, field
            )
            if not coherent or not present:
                return None
            fields[field] = value
        offset = fields["offset"]
        limit = fields["limit"]
        returned_chars = fields["returned_chars"]
        total_chars = fields["total_chars"]
        next_offset = fields["next_offset"]
        has_more = fields["has_more"]
        if any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in (offset, limit, returned_chars, total_chars)
        ):
            return None
        if (
            offset < 0
            or limit <= 0
            or returned_chars != len(content)
            or returned_chars > limit
            or total_chars < offset + returned_chars
            or offset != requested_offset
        ):
            return None
        if requested_limit is not None and limit != requested_limit:
            return None
        if not isinstance(has_more, bool):
            return None
        expected_next = offset + returned_chars
        if has_more:
            if (
                returned_chars <= 0
                or not isinstance(next_offset, int)
                or isinstance(next_offset, bool)
                or next_offset != expected_next
                or next_offset >= total_chars
            ):
                return None
        elif next_offset is not None or expected_next != total_chars:
            return None
        if "truncated" in result and result.get("truncated") is not has_more:
            return None
        skill_chars = result.get("skill_md_chars")
        if skill_chars is not None and (
            not isinstance(skill_chars, int)
            or isinstance(skill_chars, bool)
            or skill_chars != total_chars
        ):
            return None
    else:
        # A non-paged main view carries the parsed Markdown body while the
        # loader-owned digest/length address the complete document including
        # frontmatter.  Those fields are the verifiable whole-document receipt.
        if requested_offset != 0:
            return None
        if result.get("has_more") not in (None, False):
            return None
        if result.get("next_offset") is not None:
            return None
        skill_chars = result.get("skill_md_chars")
        if (
            not isinstance(skill_chars, int)
            or isinstance(skill_chars, bool)
            or skill_chars <= 0
        ):
            return None
        offset = 0
        next_offset = None
        has_more = False
        total_chars = skill_chars

    return _CanonicalSkillViewReceipt(
        skill_name=skill_name,
        document_sha256=digest,
        offset=offset,
        next_offset=next_offset,
        has_more=has_more,
        total_chars=total_chars,
        is_paged=is_paged,
        content=content,
        assistant_index=assistant_index,
        call_position=call_position,
        result_index=result_index,
        tool_call=tool_call,
        tool_result=tool_result,
    )


def _canonical_skill_view_receipts(
    messages: list[dict[str, Any]],
) -> list[_CanonicalSkillViewReceipt]:
    """Find native one-to-one canonical skill_view call/result pairs."""
    call_counts: dict[str, int] = {}
    result_indices: dict[str, list[int]] = {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or []:
                identity = _tool_call_identity(tool_call)
                if identity is not None:
                    call_counts[identity[0]] = call_counts.get(identity[0], 0) + 1
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                result_indices.setdefault(call_id, []).append(index)

    receipts: list[_CanonicalSkillViewReceipt] = []
    for assistant_index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for call_position, tool_call in enumerate(message.get("tool_calls") or []):
            identity = _tool_call_identity(tool_call)
            if identity is None:
                continue
            call_id = identity[0]
            matching_results = result_indices.get(call_id) or []
            if call_counts.get(call_id) != 1 or len(matching_results) != 1:
                continue
            result_index = matching_results[0]
            if result_index <= assistant_index:
                continue
            if any(
                messages[index].get("role") != "tool"
                for index in range(assistant_index + 1, result_index + 1)
            ):
                continue
            tool_result = messages[result_index]
            receipt = _validated_canonical_skill_receipt(
                assistant_index=assistant_index,
                call_position=call_position,
                tool_call=tool_call,
                result_index=result_index,
                tool_result=tool_result,
            )
            if receipt is not None:
                receipts.append(receipt)
    return receipts


def _select_latest_contiguous_skill_receipts(
    receipts: list[_CanonicalSkillViewReceipt],
) -> list[_CanonicalSkillViewReceipt]:
    """Keep one latest digest and one latest exact page per offset per Skill."""
    grouped: dict[
        tuple[str, str], dict[int, _CanonicalSkillViewReceipt]
    ] = {}
    group_occurrence: dict[tuple[str, str], tuple[int, int, int]] = {}
    for receipt in receipts:
        key = (receipt.skill_name, receipt.document_sha256)
        pages = grouped.setdefault(key, {})
        prior = pages.get(receipt.offset)
        if prior is None or receipt.occurrence > prior.occurrence:
            pages[receipt.offset] = receipt
        group_occurrence[key] = max(
            group_occurrence.get(key, (-1, -1, -1)),
            receipt.occurrence,
        )

    viable: dict[
        tuple[str, str], tuple[tuple[int, int, int], list[_CanonicalSkillViewReceipt]]
    ] = {}
    for key, pages in grouped.items():
        first = pages.get(0)
        if first is None:
            continue
        chain: list[_CanonicalSkillViewReceipt] = []
        cursor = 0
        total_chars = first.total_chars
        seen: set[int] = set()
        while cursor not in seen:
            seen.add(cursor)
            page = pages.get(cursor)
            if page is None or page.total_chars != total_chars:
                break
            chain.append(page)
            if not page.has_more:
                break
            if page.next_offset is None:
                break
            cursor = page.next_offset
        if (
            chain
            and chain[0].is_paged
            and not chain[-1].has_more
            and hashlib.sha256(
                "".join(page.content for page in chain).encode("utf-8")
            ).hexdigest() != key[1]
        ):
            continue
        if chain:
            viable[key] = (group_occurrence[key], chain)

    selected_by_skill: dict[
        str, tuple[tuple[int, int, int], str, list[_CanonicalSkillViewReceipt]]
    ] = {}
    for (skill_name, digest), (occurrence, chain) in viable.items():
        candidate = (occurrence, digest, chain)
        current = selected_by_skill.get(skill_name)
        if current is None or candidate[:2] > current[:2]:
            selected_by_skill[skill_name] = candidate

    ordered = sorted(
        selected_by_skill.values(),
        key=lambda item: (item[0], item[1]),
    )
    return [receipt for _, _, chain in ordered for receipt in chain]


def _strip_canonical_skill_receipts(
    messages: list[dict[str, Any]],
    receipts: list[_CanonicalSkillViewReceipt],
) -> list[dict[str, Any]]:
    """Remove exact valid receipts before pruning/summarization."""
    calls_by_assistant: dict[int, set[int]] = {}
    result_indices: set[int] = set()
    for receipt in receipts:
        calls_by_assistant.setdefault(receipt.assistant_index, set()).add(
            receipt.call_position
        )
        result_indices.add(receipt.result_index)

    stripped: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index in result_indices:
            continue
        removed_positions = calls_by_assistant.get(index)
        if not removed_positions:
            stripped.append(message.copy())
            continue
        tool_calls = list(message.get("tool_calls") or [])
        retained_calls = [
            tool_call
            for position, tool_call in enumerate(tool_calls)
            if position not in removed_positions
        ]
        updated = message.copy()
        if retained_calls:
            updated["tool_calls"] = retained_calls
            stripped.append(updated)
            continue
        updated.pop("tool_calls", None)
        if updated.get("content") not in (None, ""):
            stripped.append(updated)
    return stripped


def _native_skill_receipt_messages(
    receipts: list[_CanonicalSkillViewReceipt],
) -> list[dict[str, Any]]:
    """Render selected receipts only in their native assistant/tool roles."""
    protected: list[dict[str, Any]] = []
    for receipt in receipts:
        protected.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [receipt.tool_call],
        })
        protected.append(receipt.tool_result.copy())
    return protected


# ---------------------------------------------------------------------------
# Tool result summarizer (same logic as hermes)
# ---------------------------------------------------------------------------

_PATH_MENTION_RE = re.compile(r"(?:/|~/?|[A-Za-z]:\\)[^\s`'\")\]}<>]+")


def _summarize_tool_result(tool_name: str, tool_args: str, tool_content: str) -> str:
    """Create an informative 1-line summary of a tool call + result."""
    try:
        args = json.loads(tool_args) if tool_args else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    content = tool_content or ""
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    # Match hermes tool naming conventions
    if tool_name in ("terminal", "execute_code"):
        code = args.get("code", "") or args.get("command", "")
        preview = code[:77] + "..." if len(code) > 80 else code
        return f"[{tool_name}] ran code ({line_count} lines output, {content_len:,} chars)"

    if tool_name == "read_file":
        path = args.get("path", "?")
        return f"[read_file] read {path} ({content_len:,} chars)"

    if tool_name == "write_file":
        path = args.get("path", "?")
        return f"[write_file] wrote to {path} ({content_len:,} chars)"

    if tool_name == "search_files":
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        return f"[search_files] search for '{pattern}' in {path} -> {line_count} lines"

    if tool_name == "web_search":
        query = args.get("query", "?")
        return f"[web_search] query='{query}' ({content_len:,} chars result)"

    if tool_name == "web_extract":
        url = args.get("url", "?")
        return f"[web_extract] {url} ({content_len:,} chars)"

    if tool_name in ("browser_navigate", "browser_click", "browser_snapshot"):
        url = args.get("url", "")
        detail = f" {url}" if url else ""
        return f"[{tool_name}]{detail} ({content_len:,} chars)"

    if tool_name == "memory":
        action = args.get("action", "?")
        return f"[memory] {action}"

    if tool_name == "todo":
        return "[todo] updated task list"

    if tool_name in ("skills_list", "skill_view", "skill_manage"):
        name = args.get("name", "?")
        return f"[{tool_name}] name={name} ({content_len:,} chars)"

    if tool_name == "clarify":
        return "[clarify] asked user a question"

    if tool_name == "session_search":
        query = args.get("query", "?")
        return f"[session_search] query='{query}'"

    # Generic fallback
    first_arg = ""
    for k, v in list(args.items())[:2]:
        sv = str(v)[:40]
        first_arg += f" {k}={sv}"
    return f"[{tool_name}]{first_arg} ({content_len:,} chars result)"


_SUMMARY_SAFE_TOOL_ARGUMENT_KEYS = (
    "filepath", "file_path", "path", "script_path", "name", "query", "url",
    "action", "pattern", "offset", "limit", "timeout",
)


def _summarize_tool_call_args_json(args: str) -> str:
    """Render identifiers only; never place executable payloads in summaries."""
    try:
        parsed = json.loads(args)
    except (ValueError, TypeError):
        return "(arguments unavailable)"
    if not isinstance(parsed, dict):
        return "(arguments unavailable)"
    summary: dict[str, Any] = {}
    for key in _SUMMARY_SAFE_TOOL_ARGUMENT_KEYS:
        value = parsed.get(key)
        if isinstance(value, (str, int, float, bool)):
            summary[key] = value[:240] if isinstance(value, str) else value
    return json.dumps(summary, ensure_ascii=False) if summary else "(payload withheld)"


# ============================================================================
# ContextCompressor
# ============================================================================


class ContextCompressor(ContextEngine):
    """Default context engine — compresses conversation context via LLM summarization.

    Algorithm:
      1. Prune old tool results (cheap, no LLM call)
      2. Protect head messages (system prompt + first exchange)
      3. Protect tail messages by token budget
      4. Summarize middle turns with structured LLM prompt
      5. On subsequent compactions, iteratively update the previous summary
    """

    @property
    def name(self) -> str:
        return "compressor"

    def __init__(
        self,
        context_length: int | None = None,
        threshold_percent: float = 0.75,
        protect_first_n: int = 3,
        protect_last_n: int = 6,
        summary_target_ratio: float = 0.20,
        base_url: str = "",
        api_model: str = "",
        api_key: str = "",
    ):
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.summary_target_ratio = max(0.10, min(summary_target_ratio, 0.80))
        self.base_url = base_url
        self.api_model = api_model
        self.api_key = api_key

        self.context_length = context_length or _DEFAULT_CONTEXT_LENGTH
        self.threshold_tokens = max(
            int(self.context_length * threshold_percent),
            64_000,  # floor at 64K
        )

        target_tokens = int(self.threshold_tokens * self.summary_target_ratio)
        self.tail_token_budget = target_tokens
        self.max_summary_tokens = min(
            int(self.context_length * 0.05), _SUMMARY_TOKENS_CEILING,
        )

        self.compression_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0

        # Iterative summary state
        self._previous_summary: str | None = None

        # Anti-thrashing
        self._last_compression_savings_pct: float = 100.0
        self._ineffective_compression_count: int = 0

        # Failure tracking
        self._last_summary_error: str | None = None
        self._last_summary_dropped_count: int = 0
        self._last_summary_fallback_used: bool = False
        self._last_compress_aborted: bool = False
        self._summary_failure_cooldown_until: float = 0.0
        self._last_protected_skill_receipt_count: int = 0
        self._last_protected_skill_tokens: int = 0

        logger.info(
            "ContextCompressor initialized: context_length=%d threshold=%d (%.0f%%) "
            "tail_budget=%d max_summary=%d",
            self.context_length, self.threshold_tokens,
            threshold_percent * 100, self.tail_token_budget,
            self.max_summary_tokens,
        )

    # -- ContextEngine interface -----------------------------------------------

    def set_context_length(self, context_length: Any) -> None:
        """Update run-local budgets after authoritative provider feedback."""

        if isinstance(context_length, bool):
            return
        try:
            resolved = int(context_length)
        except (TypeError, ValueError, OverflowError):
            return
        if resolved <= 0:
            return
        self.context_length = resolved
        self.threshold_tokens = max(
            int(self.context_length * self.threshold_percent),
            64_000,
        )
        target_tokens = int(
            self.threshold_tokens * self.summary_target_ratio
        )
        self.tail_token_budget = target_tokens
        self.max_summary_tokens = min(
            int(self.context_length * 0.05),
            _SUMMARY_TOKENS_CEILING,
        )

    def update_from_response(self, usage: dict[str, Any]) -> None:
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get(
            "total_tokens",
            self.last_prompt_tokens + self.last_completion_tokens,
        )

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        status.update({
            "last_compress_aborted": self._last_compress_aborted,
            "protected_skill_budget_exceeded": bool(
                self._last_compress_aborted
                and self._last_summary_error
                and self._last_summary_error.startswith(
                    "protected_skill_context_budget_exceeded:"
                )
            ),
            "protected_skill_receipt_count": (
                self._last_protected_skill_receipt_count
            ),
            "protected_skill_tokens": self._last_protected_skill_tokens,
            "protected_skill_token_budget": self._protected_skill_token_budget(),
        })
        return status

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        if tokens < self.threshold_tokens:
            return False
        if self._ineffective_compression_count >= 2:
            logger.warning(
                "Compression skipped — last %d compressions saved <10%% each.",
                self._ineffective_compression_count,
            )
            return False
        return True

    def on_session_reset(self) -> None:
        super().on_session_reset()
        self._previous_summary = None
        self._last_summary_error = None
        self._last_summary_dropped_count = 0
        self._last_summary_fallback_used = False
        self._last_compress_aborted = False
        self._last_compression_savings_pct = 100.0
        self._ineffective_compression_count = 0
        self._summary_failure_cooldown_until = 0.0
        self._last_protected_skill_receipt_count = 0
        self._last_protected_skill_tokens = 0

    def _protected_skill_token_budget(self) -> int:
        return max(
            _PROTECTED_SKILL_TOKENS_FLOOR,
            min(
                int(self.context_length * _PROTECTED_SKILL_CONTEXT_RATIO),
                _PROTECTED_SKILL_TOKENS_CEILING,
            ),
        )

    # -- Pruning ---------------------------------------------------------------

    def _prune_old_tool_results(
        self, messages: list[dict[str, Any]],
        protect_tail_count: int,
        protect_tail_tokens: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Replace old tool result contents with informative 1-line summaries."""
        if not messages:
            return messages, 0

        result = [m.copy() for m in messages]
        pruned = 0

        # Build index: tool_call_id -> (tool_name, arguments_json)
        call_id_to_tool: dict[str, tuple] = {}
        for msg in result:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        cid = tc.get("id", "")
                        fn = tc.get("function", {})
                        call_id_to_tool[cid] = (fn.get("name", "unknown"), fn.get("arguments", ""))
                    else:
                        cid = getattr(tc, "id", "") or ""
                        fn = getattr(tc, "function", None)
                        name = getattr(fn, "name", "unknown") if fn else "unknown"
                        args_str = getattr(fn, "arguments", "") if fn else ""
                        call_id_to_tool[cid] = (name, args_str)

        # Determine prune boundary
        if protect_tail_tokens is not None and protect_tail_tokens > 0:
            accumulated = 0
            boundary = len(result)
            min_protect = min(protect_tail_count, len(result))
            for i in range(len(result) - 1, -1, -1):
                msg = result[i]
                msg_tokens = estimate_message_tokens(msg) + 10
                if accumulated + msg_tokens > protect_tail_tokens and (len(result) - i) >= min_protect:
                    boundary = i
                    break
                accumulated += msg_tokens
                boundary = i
            budget_protect_count = len(result) - boundary
            protected_count = max(budget_protect_count, min_protect)
            prune_boundary = len(result) - protected_count
        else:
            prune_boundary = len(result) - protect_tail_count

        # Pass 1: Deduplicate identical tool results
        content_hashes: dict[str, tuple] = {}
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content") or ""
            if not isinstance(content, str) or len(content) < 200:
                continue
            h = hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]
            if h in content_hashes:
                result[i] = {**msg, "content": "[Duplicate tool output — same content as a more recent call]"}
                pruned += 1
            else:
                content_hashes[h] = (i, msg.get("tool_call_id", "?"))

        # Pass 2: Replace old tool results with informative summaries
        for i in range(prune_boundary):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            if not content or content.startswith("[Duplicate tool output"):
                continue
            if len(content) > 200:
                call_id = msg.get("tool_call_id", "")
                tool_name, tool_args = call_id_to_tool.get(call_id, ("unknown", ""))
                summary = _summarize_tool_result(tool_name, tool_args, content)
                result[i] = {**msg, "content": summary}
                pruned += 1

        return result, pruned

    # -- Summarization ---------------------------------------------------------

    def _compute_summary_budget(self, turns: list[dict[str, Any]]) -> int:
        content_tokens = _estimate_messages_tokens(turns)
        budget = int(content_tokens * _SUMMARY_RATIO)
        return max(_MIN_SUMMARY_TOKENS, min(budget, self.max_summary_tokens))

    def _serialize_for_summary(self, turns: list[dict[str, Any]]) -> str:
        """Serialize conversation turns into labeled text for the summarizer."""
        parts = []
        for msg in turns:
            role = msg.get("role", "unknown")
            content = _serialize_content_for_summary(msg.get("content") or "")

            if role == "tool":
                tool_id = msg.get("tool_call_id", "")
                if len(content) > _CONTENT_MAX:
                    content = content[:_CONTENT_HEAD] + "\n...[truncated]...\n" + content[-_CONTENT_TAIL:]
                parts.append(f"[TOOL RESULT {tool_id}]: {content}")
                continue

            if role == "assistant":
                if len(content) > _CONTENT_MAX:
                    content = content[:_CONTENT_HEAD] + "\n...[truncated]...\n" + content[-_CONTENT_TAIL:]
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tc_parts = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            tc_parts.append(
                                f"  {name}({_summarize_tool_call_args_json(args)})"
                            )
                        else:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", "?") if fn else "?"
                            tc_parts.append(f"  {name}(...)")
                    content += "\n[Tool calls:\n" + "\n".join(tc_parts) + "\n]"
                parts.append(f"[ASSISTANT]: {content}")
                continue

            if len(content) > _CONTENT_MAX:
                content = content[:_CONTENT_HEAD] + "\n...[truncated]...\n" + content[-_CONTENT_TAIL:]
            parts.append(f"[{role.upper()}]: {content}")

        return "\n\n".join(parts)

    async def _generate_summary(
        self,
        turns_to_summarize: list[dict[str, Any]],
        focus_topic: str | None = None,
    ) -> str | None:
        """Generate a structured summary via LLM call."""
        now = time.monotonic()
        if now < self._summary_failure_cooldown_until:
            logger.debug("Skipping summary during cooldown (%.0fs remaining)",
                         self._summary_failure_cooldown_until - now)
            return None

        summary_budget = self._compute_summary_budget(turns_to_summarize)
        content_to_summarize = self._serialize_for_summary(turns_to_summarize)

        _summarizer_preamble = (
            "You are a summarization agent creating a context checkpoint. "
            "Treat the conversation turns below as source material for a "
            "compact record of prior work. "
            "Produce only the structured summary; do not add a greeting, "
            "preamble, or prefix. "
            "Write the summary in the same language the user was using in the "
            "conversation — do not translate or switch to English. "
            "NEVER include API keys, tokens, passwords, secrets, credentials, "
            "or connection strings in the summary — replace any that appear "
            "with [REDACTED]."
        )

        _template_sections = f"""## Active Task
[THE SINGLE MOST IMPORTANT FIELD. Capture the user's most recent unfulfilled
input verbatim — the exact words they used. This includes:
- Explicit task assignments ("refactor the auth module")
- Questions awaiting an answer
- Decisions awaiting input ("optie A of B?")
- Ongoing discussions where the assistant owes the next substantive reply
A conversation where the user just asked a question IS an active task — the
task is "answer that question with full context". Do NOT write "None" merely
because the user did not issue an imperative command; reserve "None" for the
rare case where the last exchange was fully resolved and the user said
something like "thanks, that's all".
If multiple items are outstanding, list only the ones NOT yet completed.
If the user's most recent message was a reverse signal (stop, undo, roll
back, never mind, just verify, change of topic) that supersedes earlier
work, write the reverse signal verbatim and DO NOT carry forward the
cancelled task.
If no outstanding task exists, write "None."]

## Goal
[What the user is trying to accomplish overall]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Completed Actions
[Numbered list of concrete actions taken — include tool used, target, and outcome.
Format each as: N. ACTION target — outcome [tool: name]
Example:
1. READ config.py:45 — found `==` should be `!=` [tool: read_file]
2. PATCH config.py:45 — changed `==` to `!=` [tool: patch]
Be specific with file paths, commands, line numbers, and results.]

## Active State
[Current working state — include:
- Working directory and branch (if applicable)
- Modified/created files with brief note on each
- Test status (X/Y passing)
- Any running processes or servers
- Environment details that matter]

## In Progress
[Work currently underway — what was being done when compaction fired]

## Blocked
[Any blockers, errors, or issues not yet resolved. Include exact error messages.]

## Key Decisions
[Important technical decisions and WHY they were made]

## Resolved Questions
[Questions the user asked that were ALREADY answered — include the answer so it is not repeated]

## Pending User Asks
[Questions or requests from the user that have NOT yet been answered or fulfilled. If none, write "None."]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Remaining Work
[What remains to be done — framed as context, not instructions]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation. NEVER include API keys, tokens, passwords, or credentials — write [REDACTED] instead.]

Target ~{summary_budget} tokens. Be CONCRETE — include file paths, command outputs, error messages, line numbers, and specific values. Avoid vague descriptions like "made some changes" — say exactly what changed.

Write only the summary body. Do not include any preamble or prefix."""

        if self._previous_summary:
            prompt = f"""{_summarizer_preamble}

You are updating a context compaction summary. A previous compaction produced the summary below. New conversation turns have occurred since then and need to be incorporated.

PREVIOUS SUMMARY:
{self._previous_summary}

NEW TURNS TO INCORPORATE:
{content_to_summarize}

Update the summary using this exact structure. PRESERVE all existing information that is still relevant. ADD new completed actions to the numbered list (continue numbering). Move items from "In Progress" to "Completed Actions" when done. Move answered questions to "Resolved Questions". Update "Active State" to reflect current state. Remove information only if it is clearly obsolete. CRITICAL: Update "## Active Task" to reflect the user's most recent unfulfilled input — this includes any question, decision request, or discussion turn that the assistant has not yet answered. Only write "None" if the last exchange was fully resolved.

{_template_sections}"""
        else:
            prompt = f"""{_summarizer_preamble}

Create a structured checkpoint summary for the conversation after earlier turns are compacted. The summary should preserve enough detail for continuity without re-reading the original turns.

TURNS TO SUMMARIZE:
{content_to_summarize}

Use this exact structure:

{_template_sections}"""

        if focus_topic:
            prompt += f"""

FOCUS TOPIC: "{focus_topic}"
The user has requested that this compaction PRIORITISE preserving all information related to the focus topic above. For content related to "{focus_topic}", include full detail — exact values, file paths, command outputs, error messages, and decisions. For content NOT related to the focus topic, summarise more aggressively (brief one-liners or omit if truly irrelevant). The focus topic sections should receive roughly 60-70% of the summary token budget."""

        # Call LLM for summarization
        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key and self.api_key != "EMPTY":
                headers["Authorization"] = f"Bearer {self.api_key}"

            body = {
                "model": self.api_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(summary_budget * 1.3),
                "temperature": 0.3,
                "stream": False,
            }

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=body,
                )
                if resp.status_code != 200:
                    text = resp.text[:500]
                    raise RuntimeError(f"Summary LLM returned {resp.status_code}: {text}")

                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    content = str(content) if content else ""

            summary = content.strip()
            self._previous_summary = summary
            self._summary_failure_cooldown_until = 0.0
            self._last_summary_error = None
            return self._with_summary_prefix(summary)

        except Exception as e:
            self._summary_failure_cooldown_until = time.monotonic() + 60
            err_text = str(e)[:220]
            self._last_summary_error = err_text
            logger.warning("Failed to generate context summary: %s", e)
            return None

    # -- Summary prefix management ---------------------------------------------

    @staticmethod
    def _strip_summary_prefix(summary: str) -> str:
        text = (summary or "").strip()
        if text.startswith(SUMMARY_PREFIX):
            return text[len(SUMMARY_PREFIX):].lstrip()
        # Also strip legacy prefix
        legacy = "[CONTEXT SUMMARY]:"
        if text.startswith(legacy):
            return text[len(legacy):].lstrip()
        return text

    @classmethod
    def _with_summary_prefix(cls, summary: str) -> str:
        text = cls._strip_summary_prefix(summary)
        return f"{SUMMARY_PREFIX}\n{text}" if text else SUMMARY_PREFIX

    # -- Boundary helpers ------------------------------------------------------

    def _protect_head_size(self, messages: list[dict[str, Any]]) -> int:
        head = 0
        if messages and messages[0].get("role") == "system":
            head = 1
        return head + self.protect_first_n

    def _align_boundary_forward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Push a start boundary past an indivisible provider tool round."""
        return align_tool_round_boundary(
            messages,
            idx,
            direction="forward",
        )

    def _align_boundary_backward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Pull an end boundary before an indivisible provider tool round."""
        return align_tool_round_boundary(
            messages,
            idx,
            direction="backward",
        )

    def _find_last_user_message_idx(self, messages: list[dict[str, Any]], head_end: int) -> int:
        for i in range(len(messages) - 1, head_end - 1, -1):
            if messages[i].get("role") == "user":
                return i
        return -1

    def _ensure_last_user_message_in_tail(
        self, messages: list[dict[str, Any]], cut_idx: int, head_end: int,
    ) -> int:
        """Guarantee the most recent user message is in the protected tail."""
        last_user_idx = self._find_last_user_message_idx(messages, head_end)
        if last_user_idx < 0 or last_user_idx >= cut_idx:
            return cut_idx
        return max(last_user_idx, head_end + 1)

    def _find_tail_cut_by_tokens(
        self, messages: list[dict[str, Any]], head_end: int,
        token_budget: int | None = None,
    ) -> int:
        """Walk backward from end accumulating tokens until budget reached."""
        if token_budget is None:
            token_budget = self.tail_token_budget
        n = len(messages)
        min_tail = min(3, n - head_end - 1) if n - head_end > 1 else 0
        soft_ceiling = int(token_budget * 1.5)
        accumulated = 0
        cut_idx = n

        for i in range(n - 1, head_end - 1, -1):
            msg = messages[i]
            msg_tokens = estimate_message_tokens(msg) + 10
            if accumulated + msg_tokens > soft_ceiling and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i

        fallback_cut = n - min_tail
        cut_idx = min(cut_idx, fallback_cut)

        if cut_idx <= head_end:
            cut_idx = max(fallback_cut, head_end + 1)

        cut_idx = self._align_boundary_backward(messages, cut_idx)
        cut_idx = self._ensure_last_user_message_in_tail(messages, cut_idx, head_end)

        return max(cut_idx, head_end + 1)

    # -- Tool pair sanitization ------------------------------------------------

    def _sanitize_tool_pairs(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return only real, adjacent one-result-per-call provider rounds.

        Compression must not invent an effect receipt.  If malformed legacy
        history reaches this final guard, unresolved call envelopes and orphan
        results are quarantined by the shared transcript canonicalizer.
        """
        final, report = canonicalize_legacy_provider_transcript(messages)
        audit = audit_provider_transcript(final)
        if not audit.valid:
            raise RuntimeError(
                "provider transcript remained invalid after compaction "
                f"canonicalization: {audit.as_dict()}"
            )
        if report.changed:
            logger.warning(
                "Compaction canonicalized malformed provider history: %s",
                report.as_dict(),
            )
        return final

    # -- Main compress entry point --------------------------------------------

    async def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Compress conversation messages by summarizing middle turns.

        Returns the compressed message list.
        """
        # Reset per-call state
        self._last_summary_dropped_count = 0
        self._last_summary_fallback_used = False
        self._last_summary_error = None
        self._last_compress_aborted = False
        self._last_protected_skill_receipt_count = 0
        self._last_protected_skill_tokens = 0

        if force and self._summary_failure_cooldown_until > 0.0:
            self._summary_failure_cooldown_until = 0.0

        original_messages = messages
        original_message_count = len(messages)
        n_messages = original_message_count
        _min_for_compress = self._protect_head_size(messages) + 3 + 1
        if n_messages <= _min_for_compress:
            logger.warning("Cannot compress: only %d messages (need > %d)",
                          n_messages, _min_for_compress)
            return messages

        display_tokens = current_tokens or self.last_prompt_tokens or _estimate_messages_tokens(messages)

        # Canonical Skill instructions must stay in their original trust role.
        # Extract all structurally valid main-document receipts before either
        # the generic tool pruner or the summary model can see them.  Only the
        # latest digest's contiguous offset-0 chain is reinserted.
        canonical_receipts = _canonical_skill_view_receipts(messages)
        protected_receipts = _select_latest_contiguous_skill_receipts(
            canonical_receipts
        )
        protected_skill_messages = _native_skill_receipt_messages(
            protected_receipts
        )
        protected_skill_tokens = _estimate_messages_tokens(
            protected_skill_messages
        ) if protected_skill_messages else 0
        protected_skill_budget = self._protected_skill_token_budget()
        self._last_protected_skill_receipt_count = len(protected_receipts)
        self._last_protected_skill_tokens = protected_skill_tokens
        if protected_skill_tokens > protected_skill_budget:
            self._last_compress_aborted = True
            self._last_summary_error = (
                "protected_skill_context_budget_exceeded: "
                f"required={protected_skill_tokens} limit={protected_skill_budget}"
            )
            logger.error(
                "Context compression aborted: %d protected canonical Skill "
                "receipt(s) require %d tokens, exceeding dedicated budget %d",
                len(protected_receipts),
                protected_skill_tokens,
                protected_skill_budget,
            )
            return original_messages

        messages = _strip_canonical_skill_receipts(
            messages,
            canonical_receipts,
        )
        n_messages = len(messages)

        # Phase 1: Prune old tool results
        messages, pruned_count = self._prune_old_tool_results(
            messages,
            protect_tail_count=self.protect_last_n,
            protect_tail_tokens=self.tail_token_budget,
        )
        if pruned_count:
            logger.info("Pre-compression: pruned %d old tool result(s)", pruned_count)

        # Phase 2: Determine boundaries
        compress_start = self._protect_head_size(messages)
        compress_start = self._align_boundary_forward(messages, compress_start)
        compress_end = self._find_tail_cut_by_tokens(messages, compress_start)

        if compress_start >= compress_end:
            # No safe middle range exists.  Return the untouched input rather
            # than a pruned/ledger-stripped partial transformation.
            return original_messages

        turns_to_summarize = messages[compress_start:compress_end]

        logger.info(
            "Context compression: %d tokens >= %d threshold. "
            "Summarizing turns %d-%d (%d turns), protecting %d head + %d tail",
            display_tokens, self.threshold_tokens,
            compress_start + 1, compress_end, len(turns_to_summarize),
            compress_start, n_messages - compress_end,
        )

        # Phase 3: Generate structured summary
        summary = await self._generate_summary(turns_to_summarize, focus_topic=focus_topic)

        # Phase 4: Assemble compressed message list
        compressed = []
        for i in range(compress_start):
            msg = messages[i].copy()
            if i == 0 and msg.get("role") == "system" and self.compression_count == 0:
                existing = msg.get("content", "")
                _note = (
                    "[Note: Some earlier conversation turns have been compacted "
                    "into a handoff summary to preserve context space. The current "
                    "session state may still reflect earlier work, so build on that "
                    "summary and state rather than re-doing work. Your persistent "
                    "memory remains fully authoritative regardless of compaction.]"
                )
                if _note not in str(existing):
                    msg["content"] = str(existing) + "\n\n" + _note
            compressed.append(msg)

        if not summary:
            # Build a simple fallback
            self._last_summary_fallback_used = True
            self._last_summary_dropped_count = compress_end - compress_start
            fallback_lines = [
                "## Active Task",
                "(Summary model unavailable — see tail messages for latest context)",
                "",
                "## Goal",
                f"Continue from the most recent messages. {len(turns_to_summarize)} turns were compacted.",
                "",
                "## Remaining Work",
                "Review the protected tail messages for the current task.",
            ]
            summary = self._with_summary_prefix("\n".join(fallback_lines))

        # Choose summary role to avoid consecutive same-role collisions
        last_head_role = messages[compress_start - 1].get("role", "user") if compress_start > 0 else "user"
        first_tail_role = messages[compress_end].get("role", "user") if compress_end < n_messages else "user"

        if last_head_role in ("assistant", "tool"):
            summary_role = "user"
        else:
            summary_role = "assistant"

        if summary_role == first_tail_role:
            flipped = "assistant" if summary_role == "user" else "user"
            if flipped != last_head_role:
                summary_role = flipped

        if summary_role == "user":
            summary += (
                "\n\n--- END OF CONTEXT SUMMARY — "
                "respond to the message below, not the summary above ---"
            )

        compressed.append({"role": summary_role, "content": summary})

        # Keep canonical instructions as native tool data.  They are never
        # promoted into system/user messages and were never exposed to the
        # summarizer, including when summary generation fell back.
        compressed.extend(protected_skill_messages)

        for i in range(compress_end, n_messages):
            compressed.append(messages[i].copy())

        self.compression_count += 1

        compressed = self._sanitize_tool_pairs(compressed)

        new_estimate = _estimate_messages_tokens(compressed)
        saved_estimate = display_tokens - new_estimate

        savings_pct = (saved_estimate / display_tokens * 100) if display_tokens > 0 else 0
        self._last_compression_savings_pct = savings_pct
        if savings_pct < 10:
            self._ineffective_compression_count += 1
        else:
            self._ineffective_compression_count = 0

        logger.info(
            "Compressed: %d -> %d messages (~%d tokens saved, %.0f%%)",
            original_message_count, len(compressed), saved_estimate, savings_pct,
        )

        return compressed
