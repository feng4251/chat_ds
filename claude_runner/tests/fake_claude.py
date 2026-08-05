#!/usr/bin/env python3
"""Deterministic native-CLI fixture for container lifecycle acceptance."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def _argument(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


session_id = _argument("--session-id")
proxy = os.environ.get("HTTPS_PROXY", "")
if not proxy.startswith("http://127.0.0.1:"):
    raise SystemExit("missing exact loopback egress bridge")

probe = socket.socket()
probe.settimeout(0.2)
try:
    if probe.connect_ex(("1.1.1.1", 443)) == 0:
        raise SystemExit("ambient network unexpectedly reachable")
finally:
    probe.close()

transcript = (
    Path(os.environ["HOME"])
    / ".claude"
    / "projects"
    / "-workspace"
    / f"{session_id}.jsonl"
)
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.write_text(
    json.dumps({"session_id": session_id, "fixture": True}) + "\n",
    encoding="utf-8",
)

events = (
    {
        "type": "system",
        "subtype": "init",
        "model": "fixture-model",
        "tools": ["Bash", "Read", "Write", "Agent"],
        "skills": ["fixture"],
        "mcp_servers": [],
        "claude_code_version": "2.1.152",
    },
    {
        "type": "assistant",
        "uuid": "fixture-message",
        "message": {
            "model": "fixture-model",
            "content": [{"type": "text", "text": "fixture turn complete"}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        },
    },
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "fixture turn complete",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 4},
    },
)
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
