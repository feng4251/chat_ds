"""Bounded, provider-neutral convergence checks for delegated model streams.

The OpenAI-compatible transport is not an authority for output limits: a
provider can ignore ``max_tokens`` or serialize a tool request into ordinary
assistant text.  This module observes only structural stream properties and
keeps bounded state.  It never interprets or executes provider text.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import hashlib
import unicodedata
from typing import Any

from delegated_result_contract import (
    _mask_markdown_code_for_protocol_audit,
    audit_raw_tool_protocol,
)


_MIN_OUTPUT_CHAR_LIMIT = 128 * 1024
_MAX_OUTPUT_CHAR_LIMIT = 8 * 1024 * 1024
_CHARS_PER_REQUESTED_TOKEN = 32
_MIN_PROVIDER_FRAGMENT_LIMIT = 65_536
_MAX_PROVIDER_FRAGMENT_LIMIT = 1_000_000
_FRAGMENTS_PER_REQUESTED_TOKEN = 16

_CYCLE_WINDOW_CHARS = 256
_CYCLE_HISTORY_WINDOWS = 32_768
_CYCLE_MIN_NORMALIZED_CHARS = 12_288
_CYCLE_REPEAT_COUNT = 3
_CYCLE_REPEATED_WINDOW_THRESHOLD = 64
_RAW_PROTOCOL_AUDIT_INTERVAL_CHARS = 64


@dataclass
class StreamConvergenceAbort(RuntimeError):
    """A provider stream crossed a deterministic convergence boundary."""

    code: str
    metrics: dict[str, Any]

    def __str__(self) -> str:
        return self.code


def _bounded_output_limit(max_tokens: int) -> int:
    requested = max(1, int(max_tokens or 1))
    return min(
        _MAX_OUTPUT_CHAR_LIMIT,
        max(_MIN_OUTPUT_CHAR_LIMIT, requested * _CHARS_PER_REQUESTED_TOKEN),
    )


def _bounded_fragment_limit(max_tokens: int) -> int:
    requested = max(1, int(max_tokens or 1))
    return min(
        _MAX_PROVIDER_FRAGMENT_LIMIT,
        max(
            _MIN_PROVIDER_FRAGMENT_LIMIT,
            requested * _FRAGMENTS_PER_REQUESTED_TOKEN,
        ),
    )


def _normalize_cycle_text(value: str) -> str:
    """Return a language-neutral exact-cycle representation.

    Whitespace is intentionally discarded so harmless provider token/chunk
    boundaries cannot hide a repeated generation.  Punctuation and all
    non-whitespace Unicode characters remain significant.
    """

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if not char.isspace())


class StreamConvergenceGuard:
    """Observe one delegated provider request using bounded structural state."""

    def __init__(
        self,
        *,
        max_tokens: int,
        semantic_checks_enabled: bool = True,
    ) -> None:
        self.semantic_checks_enabled = bool(semantic_checks_enabled)
        self.content_char_limit = _bounded_output_limit(max_tokens)
        self.reasoning_char_limit = _bounded_output_limit(max_tokens)
        self.provider_fragment_limit = _bounded_fragment_limit(max_tokens)

        self.raw_content_chars = 0
        self.raw_reasoning_chars = 0
        self.provider_fragments = 0
        self.structured_tool_fragments = 0

        self._content = ""
        self._raw_protocol_candidate = False
        self._last_raw_protocol_audit_chars = 0

        self._reasoning_normalized = ""
        self._normalized_reasoning_chars_total = 0
        self._next_cycle_window_start = 0
        self._cycle_window_digests: deque[bytes] = deque()
        self._cycle_digest_counts: Counter[bytes] = Counter()
        self._repeated_cycle_windows = 0

    def observe_provider_fragment(
        self,
        *,
        content_chars: int = 0,
        reasoning_chars: int = 0,
        structured_tool_fragments: int = 0,
    ) -> None:
        """Account for one normalized provider event before retaining it."""

        self.provider_fragments += 1
        self.raw_content_chars += max(0, int(content_chars or 0))
        self.raw_reasoning_chars += max(0, int(reasoning_chars or 0))
        self.structured_tool_fragments += max(
            0, int(structured_tool_fragments or 0)
        )
        if self.raw_content_chars > self.content_char_limit:
            self._abort("content_char_limit_exceeded")
        if self.raw_reasoning_chars > self.reasoning_char_limit:
            self._abort("reasoning_char_limit_exceeded")
        if self.provider_fragments > self.provider_fragment_limit:
            self._abort("provider_fragment_limit_exceeded")

    def observe_content(self, value: str) -> None:
        """Audit scrubbed visible content without trusting raw pseudo-calls."""

        if not value:
            return
        if not self.semantic_checks_enabled:
            return
        new_value = str(value)
        self._content += new_value
        folded_tail = self._content[-32:].casefold()
        if (
            "<tool_call" in new_value.casefold()
            or "<tool_call" in folded_tail
        ):
            self._raw_protocol_candidate = True
        if not self._raw_protocol_candidate:
            return
        current_chars = len(self._content)
        should_audit = bool(
            current_chars - self._last_raw_protocol_audit_chars
            >= _RAW_PROTOCOL_AUDIT_INTERVAL_CHARS
            or "</tool_call" in folded_tail
        )
        if not should_audit:
            return
        self._last_raw_protocol_audit_chars = current_chars
        audit = audit_raw_tool_protocol(self._content)
        detected = int(audit.get("detected_count", 0) or 0)
        if detected >= 3:
            self._abort(
                "raw_pseudo_tool_protocol_cycle",
                raw_protocol_count=detected,
                raw_protocol_scan_truncated=bool(audit.get("scan_truncated")),
            )
        # Some providers emit a malformed dialect such as
        # ``<tool_call>name` ...`` without a closing tag or an ``arguments``
        # key.  One incomplete marker can be ordinary truncated prose, but a
        # third executable-reserved marker outside Markdown code is a cycle,
        # not documentation or a trustworthy native call.
        masked = _mask_markdown_code_for_protocol_audit(self._content)
        malformed_marker_count = masked.casefold().count("<tool_call")
        if malformed_marker_count >= 3:
            self._abort(
                "raw_pseudo_tool_protocol_cycle",
                raw_protocol_marker_count=malformed_marker_count,
            )

    def observe_reasoning(
        self,
        value: str,
        *,
        visible_content_seen: bool,
        structured_tool_fragment_seen: bool,
    ) -> None:
        """Detect a long exact hidden-reasoning cycle before transport timeout."""

        if (
            not value
            or not self.semantic_checks_enabled
            or visible_content_seen
            or structured_tool_fragment_seen
        ):
            return
        normalized_value = _normalize_cycle_text(value)
        self._normalized_reasoning_chars_total += len(normalized_value)
        self._reasoning_normalized += normalized_value
        text = self._reasoning_normalized
        while (
            self._next_cycle_window_start + _CYCLE_WINDOW_CHARS
            <= len(text)
        ):
            start = self._next_cycle_window_start
            window = text[start:start + _CYCLE_WINDOW_CHARS]
            digest = hashlib.blake2b(
                window.encode("utf-8"), digest_size=16
            ).digest()
            prior = self._cycle_digest_counts[digest]
            self._cycle_digest_counts[digest] = prior + 1
            if prior + 1 == _CYCLE_REPEAT_COUNT:
                self._repeated_cycle_windows += 1
            self._cycle_window_digests.append(digest)
            if len(self._cycle_window_digests) > _CYCLE_HISTORY_WINDOWS:
                expired = self._cycle_window_digests.popleft()
                expired_count = self._cycle_digest_counts[expired]
                if expired_count == _CYCLE_REPEAT_COUNT:
                    self._repeated_cycle_windows -= 1
                if expired_count <= 1:
                    del self._cycle_digest_counts[expired]
                else:
                    self._cycle_digest_counts[expired] = expired_count - 1
            self._next_cycle_window_start += 1

        retained_reasoning_chars = (
            _CYCLE_HISTORY_WINDOWS + _CYCLE_WINDOW_CHARS
        )
        if len(self._reasoning_normalized) > retained_reasoning_chars:
            discarded = len(self._reasoning_normalized) - retained_reasoning_chars
            self._reasoning_normalized = self._reasoning_normalized[discarded:]
            self._next_cycle_window_start = max(
                0, self._next_cycle_window_start - discarded
            )
            text = self._reasoning_normalized

        maximum_window_repetitions = max(
            self._cycle_digest_counts.values(), default=0
        )
        if (
            self._normalized_reasoning_chars_total
            >= _CYCLE_MIN_NORMALIZED_CHARS
            and (
                self._repeated_cycle_windows
                >= _CYCLE_REPEATED_WINDOW_THRESHOLD
                or maximum_window_repetitions >= 8
            )
        ):
            self._abort(
                "reasoning_cycle_detected",
                normalized_reasoning_chars=(
                    self._normalized_reasoning_chars_total
                ),
                repeated_cycle_windows=self._repeated_cycle_windows,
                maximum_cycle_window_repetitions=(
                    maximum_window_repetitions
                ),
                cycle_window_chars=_CYCLE_WINDOW_CHARS,
                cycle_repeat_count=_CYCLE_REPEAT_COUNT,
            )

    def _abort(self, code: str, **extra: Any) -> None:
        raise StreamConvergenceAbort(
            code=code,
            metrics={
                "raw_content_chars": self.raw_content_chars,
                "raw_reasoning_chars": self.raw_reasoning_chars,
                "provider_fragment_count": self.provider_fragments,
                "structured_tool_fragment_count": (
                    self.structured_tool_fragments
                ),
                "content_char_limit": self.content_char_limit,
                "reasoning_char_limit": self.reasoning_char_limit,
                "provider_fragment_limit": self.provider_fragment_limit,
                **extra,
            },
        )


__all__ = ["StreamConvergenceAbort", "StreamConvergenceGuard"]
