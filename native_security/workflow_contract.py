"""Engine-neutral binding of immutable Skill routes to one user Turn."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


MAX_ROUTES = 128
MAX_PHASES = 128
MAX_WORKERS = 128
MAX_PATTERNS = 128
MAX_PATTERN_BYTES = 8 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
EXPECTED_SKILL_PLUGIN_NAME = "chatds-session-skills"
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("skill_workflow_routes_invalid")
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise RuntimeError("skill_workflow_routes_invalid")
    return normalized


def compile_turn_workflow_contract(
    *,
    manifest: dict[str, Any],
    user_turn_text: str,
    bound_skill_name: str | None,
) -> dict[str, Any] | None:
    """Select exactly one highest-priority Skill route under a time bound.

    Route selection is shared by native engine adapters. The returned contract
    contains only immutable Skill-view identities and phase topology; engines
    remain responsible for projecting those workers onto their native runtime.
    """

    raw_routes = manifest.get("workflow_routes", [])
    selected = manifest.get("selected_primary_skill_names", [])
    if (
        not isinstance(raw_routes, list)
        or len(raw_routes) > MAX_ROUTES
        or not isinstance(selected, list)
        or any(
            not isinstance(name, str) or SAFE_NAME.fullmatch(name) is None
            for name in selected
        )
        or not isinstance(user_turn_text, str)
        or len(user_turn_text.encode("utf-8")) > MAX_TEXT_BYTES
    ):
        raise RuntimeError("skill_workflow_routes_invalid")
    if bound_skill_name is not None and (
        SAFE_NAME.fullmatch(bound_skill_name) is None
        or bound_skill_name not in selected
    ):
        raise RuntimeError("skill_workflow_routes_invalid")
    eligible_skills = (
        {bound_skill_name} if bound_skill_name is not None else set(selected)
    )

    routes: list[dict[str, Any]] = []
    for raw_route in raw_routes:
        if not isinstance(raw_route, dict):
            raise RuntimeError("skill_workflow_routes_invalid")
        skill_name = raw_route.get("skill_name")
        if skill_name not in eligible_skills:
            continue
        route_id = raw_route.get("route_id")
        priority = raw_route.get("priority")
        patterns = raw_route.get("patterns")
        phases = raw_route.get("phases")
        if (
            not isinstance(skill_name, str)
            or SAFE_NAME.fullmatch(skill_name) is None
            or not isinstance(route_id, str)
            or SAFE_NAME.fullmatch(route_id) is None
            or isinstance(priority, bool)
            or not isinstance(priority, int)
            or not -100_000 <= priority <= 100_000
            or not isinstance(patterns, list)
            or not patterns
            or len(patterns) > MAX_PATTERNS
            or any(
                not isinstance(pattern, str)
                or not pattern
                or len(pattern.encode("utf-8")) > MAX_PATTERN_BYTES
                for pattern in patterns
            )
            or not isinstance(phases, list)
            or not phases
            or len(phases) > MAX_PHASES
        ):
            raise RuntimeError("skill_workflow_routes_invalid")
        source_path = _safe_relative_path(raw_route.get("source_path"))
        observed_workers: set[str] = set()
        normalized_phases: list[dict[str, Any]] = []
        for phase in phases:
            if not isinstance(phase, dict):
                raise RuntimeError("skill_workflow_routes_invalid")
            mode = phase.get("mode")
            workers = phase.get("workers")
            if (
                mode not in {"parallel", "sequential"}
                or not isinstance(workers, list)
                or not workers
                or len(workers) > MAX_WORKERS
                or mode == "sequential" and len(workers) != 1
            ):
                raise RuntimeError("skill_workflow_routes_invalid")
            normalized_workers: list[dict[str, str]] = []
            for worker in workers:
                if not isinstance(worker, dict):
                    raise RuntimeError("skill_workflow_routes_invalid")
                worker_id = worker.get("worker_id")
                native_agent_type = worker.get("native_agent_type")
                if (
                    not isinstance(worker_id, str)
                    or SAFE_NAME.fullmatch(worker_id) is None
                    or native_agent_type
                    != f"{EXPECTED_SKILL_PLUGIN_NAME}:{skill_name}:{worker_id}"
                    or worker_id in observed_workers
                ):
                    raise RuntimeError("skill_workflow_routes_invalid")
                observed_workers.add(worker_id)
                normalized_workers.append({
                    "worker_id": worker_id,
                    "native_agent_type": native_agent_type,
                })
            normalized_phases.append({
                "mode": mode,
                "workers": normalized_workers,
            })
        routes.append({
            "skill_name": skill_name,
            "route_id": route_id,
            "source_path": source_path,
            "priority": priority,
            "patterns": list(patterns),
            "phases": normalized_phases,
        })
    if not routes:
        return None

    payload = json.dumps(
        {
            "routes": [route["patterns"] for route in routes],
            "text": user_turn_text,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    matcher = Path(__file__).with_name("workflow_matcher.py")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(matcher)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("skill_workflow_route_match_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise RuntimeError("skill_workflow_route_match_failed")
    try:
        matched_rows = json.loads(completed.stdout)["matches"]
    except (UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("skill_workflow_route_match_failed") from exc
    if (
        not isinstance(matched_rows, list)
        or len(matched_rows) != len(routes)
        or any(
            not isinstance(indices, list)
            or any(
                isinstance(index, bool) or not isinstance(index, int)
                for index in indices
            )
            for indices in matched_rows
        )
    ):
        raise RuntimeError("skill_workflow_route_match_failed")
    matches = [
        (route, indices[0])
        for route, indices in zip(routes, matched_rows, strict=True)
        if indices
    ]
    if not matches:
        return None
    highest_priority = max(route["priority"] for route, _index in matches)
    winners = [
        (route, index)
        for route, index in matches
        if route["priority"] == highest_priority
    ]
    if len(winners) != 1:
        raise RuntimeError("skill_workflow_route_ambiguous")
    route, matched_pattern_index = winners[0]
    return {
        "schema": "chatds.skill-workflow-contract.v1",
        "skill_name": route["skill_name"],
        "route_id": route["route_id"],
        "source_path": route["source_path"],
        "priority": route["priority"],
        "matched_pattern_index": matched_pattern_index,
        "route_sha256": _canonical_sha256(route),
        "phases": route["phases"],
    }
