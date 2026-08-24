"""Bounded replay helpers for the durable DeepSeek event ledger."""

from __future__ import annotations

from pathlib import Path


MAX_EVENT_STREAM_BATCH_BYTES = 1024 * 1024
MAX_EVENT_STREAM_BATCH_LINES = 4096
MAX_EVENT_STREAM_LINE_BYTES = 16 * 1024 * 1024


def read_event_tail(path: Path, position: int) -> tuple[int, tuple[bytes, ...]]:
    """Read complete JSONL records from one bounded byte/line batch."""

    lines: list[bytes] = []
    observed = 0
    try:
        with path.open("rb") as stream:
            stream.seek(position)
            while (
                len(lines) < MAX_EVENT_STREAM_BATCH_LINES
                and (observed < MAX_EVENT_STREAM_BATCH_BYTES or not lines)
            ):
                line_start = stream.tell()
                line = stream.readline(MAX_EVENT_STREAM_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT_STREAM_LINE_BYTES:
                    raise RuntimeError("deepseek_event_stream_line_too_large")
                if not line.endswith(b"\n"):
                    stream.seek(line_start)
                    break
                position = stream.tell()
                observed += len(line)
                lines.append(line[:-1].removesuffix(b"\r"))
    except OSError:
        return position, ()
    return position, tuple(lines)
