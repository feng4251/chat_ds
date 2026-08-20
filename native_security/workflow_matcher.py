"""Time-isolated regex matcher for immutable Skill workflow routes."""

from __future__ import annotations

import json
import re
import resource
import sys


MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_ROUTES = 128
MAX_PATTERNS = 128
MAX_PATTERN_BYTES = 8 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024


def _match(value: object) -> list[list[int]]:
    if not isinstance(value, dict) or set(value) != {"routes", "text"}:
        raise ValueError("workflow_match_input_invalid")
    routes = value["routes"]
    text = value["text"]
    if (
        not isinstance(routes, list)
        or len(routes) > MAX_ROUTES
        or not isinstance(text, str)
        or len(text.encode("utf-8")) > MAX_TEXT_BYTES
    ):
        raise ValueError("workflow_match_input_invalid")
    matches: list[list[int]] = []
    for patterns in routes:
        if (
            not isinstance(patterns, list)
            or not patterns
            or len(patterns) > MAX_PATTERNS
        ):
            raise ValueError("workflow_match_input_invalid")
        route_matches: list[int] = []
        for index, pattern in enumerate(patterns):
            if (
                not isinstance(pattern, str)
                or not pattern
                or len(pattern.encode("utf-8")) > MAX_PATTERN_BYTES
            ):
                raise ValueError("workflow_match_input_invalid")
            if re.search(pattern, text, flags=re.IGNORECASE) is not None:
                route_matches.append(index)
        matches.append(route_matches)
    return matches


def main() -> int:
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
        resource.setrlimit(
            resource.RLIMIT_AS,
            (256 * 1024 * 1024, 256 * 1024 * 1024),
        )
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("workflow_match_input_invalid")
        value = json.loads(raw)
        output = {"matches": _match(value)}
    except (UnicodeError, ValueError, TypeError, re.error, json.JSONDecodeError):
        return 2
    sys.stdout.write(json.dumps(
        output,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
