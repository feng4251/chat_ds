"""Durable native DSH Skill activation evidence for artifact contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from native_security.artifact_contract import (
    validate_artifact_contracts,
    workspace_snapshot,
)


MAX_NATIVE_EVENTS = 1_000_000
MAX_TOOL_ARGUMENT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_CONTRACTS = 64
MAX_ARTIFACT_PROJECTION_BYTES = 32 * 1024 * 1024
SAFE_SKILL_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
SAFE_NATIVE_SESSION_ID = re.compile(r"chatds-[0-9a-f]{32}")
SAFE_WORKFLOW_RUN_NAME = re.compile(r"skill-workflow-[0-9a-f]{16}")


def _skill_argument(value: object) -> str | None:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            return None
        try:
            value = json.loads(value or "{}")
        except (UnicodeError, ValueError, TypeError):
            return None
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    if not isinstance(name, str) or SAFE_SKILL_NAME.fullmatch(name) is None:
        return None
    return name


def _root_session_event(
    envelope: object,
    *,
    native_session_id: str,
) -> dict[str, Any] | None:
    if not isinstance(envelope, dict):
        return None
    event = envelope.get("event")
    if (
        not isinstance(event, dict)
        or event.get("type") != "deepseek.session.event"
        or event.get("session_id") != native_session_id
        or event.get("delegation_depth") != 0
    ):
        return None
    session_event = event.get("session_event")
    return session_event if isinstance(session_event, dict) else None


def _call_id(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        return None
    return value


def _top_level_result(data: object) -> tuple[str, bool] | None:
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    source = message.get("source")
    content = message.get("content")
    if not isinstance(source, dict) or not isinstance(content, list):
        return None
    call_id = _call_id(source.get("callId"))
    if call_id is None:
        return None
    matching = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool-result"
        and block.get("toolCallId") == call_id
        and isinstance(block.get("isError"), bool)
    ]
    if len(matching) != 1:
        return None
    return call_id, not matching[0]["isError"]


def successful_root_skill_invocations(
    envelopes: Iterable[object],
    *,
    native_session_id: str,
) -> tuple[str, ...]:
    """Return Skills proven by a successful root native tool call/result pair."""

    if SAFE_NATIVE_SESSION_ID.fullmatch(native_session_id) is None:
        raise RuntimeError("native_skill_receipt_invalid")
    top_calls: dict[str, str] = {}
    code_calls: dict[str, str] = {}
    settled_top: dict[str, bool] = {}
    settled_code: dict[str, bool] = {}
    successful: set[str] = set()
    observed = 0

    def record_call(target: dict[str, str], call_id: str, skill_name: str) -> None:
        previous = target.get(call_id)
        if previous is not None and previous != skill_name:
            raise RuntimeError("native_skill_receipt_invalid")
        target[call_id] = skill_name

    def record_result(
        calls: dict[str, str],
        settled: dict[str, bool],
        call_id: str,
        passed: bool,
    ) -> None:
        previous = settled.get(call_id)
        if previous is not None and previous != passed:
            raise RuntimeError("native_skill_receipt_invalid")
        settled[call_id] = passed
        skill_name = calls.get(call_id)
        if passed and skill_name is not None:
            successful.add(skill_name)

    for envelope in envelopes:
        observed += 1
        if observed > MAX_NATIVE_EVENTS:
            raise RuntimeError("native_skill_receipt_event_limit")
        event = _root_session_event(
            envelope,
            native_session_id=native_session_id,
        )
        if event is None:
            continue
        event_type = event.get("type")
        data = event.get("data")
        if event_type == "tool/call" and isinstance(data, dict):
            if data.get("name") != "skill":
                continue
            call_id = _call_id(data.get("callId"))
            skill_name = _skill_argument(data.get("arguments"))
            if call_id is not None and skill_name is not None:
                record_call(top_calls, call_id, skill_name)
        elif event_type == "tool/result":
            result = _top_level_result(data)
            if result is not None:
                record_result(top_calls, settled_top, *result)
        elif event_type == "tool/code-dispatch-start" and isinstance(data, dict):
            if data.get("name") != "skill":
                continue
            call_id = _call_id(data.get("subCallId"))
            skill_name = _skill_argument(data.get("arguments"))
            if call_id is not None and skill_name is not None:
                record_call(code_calls, call_id, skill_name)
        elif event_type == "tool/code-dispatch" and isinstance(data, dict):
            if data.get("name") != "skill" or not isinstance(data.get("isError"), bool):
                continue
            call_id = _call_id(data.get("subCallId"))
            skill_name = _skill_argument(data.get("arguments"))
            if call_id is None or skill_name is None:
                continue
            record_call(code_calls, call_id, skill_name)
            record_result(code_calls, settled_code, call_id, not data["isError"])
    return tuple(sorted(successful))


def active_artifact_skill_names(
    *,
    contracts: object,
    workflow_projection: object,
    envelopes: Iterable[object],
    native_session_id: str,
    bound_skill_name: str | None = None,
) -> tuple[str, ...]:
    """Select exact artifact contracts activated by controller/native evidence."""

    if not isinstance(contracts, list) or len(contracts) > MAX_ARTIFACT_CONTRACTS:
        raise RuntimeError("artifact_contract_invalid")
    contract_names: set[str] = set()
    for row in contracts:
        if not isinstance(row, dict):
            raise RuntimeError("artifact_contract_invalid")
        skill_name = row.get("skill_name")
        if not isinstance(skill_name, str) or SAFE_SKILL_NAME.fullmatch(skill_name) is None:
            raise RuntimeError("artifact_contract_invalid")
        contract_names.add(skill_name)
    if workflow_projection is not None:
        if not isinstance(workflow_projection, dict):
            raise RuntimeError("artifact_contract_invalid")
        skill_name = workflow_projection.get("skill_name")
        if not isinstance(skill_name, str) or SAFE_SKILL_NAME.fullmatch(skill_name) is None:
            raise RuntimeError("artifact_contract_invalid")
        return (skill_name,) if skill_name in contract_names else ()
    if bound_skill_name is not None:
        if (
            not isinstance(bound_skill_name, str)
            or SAFE_SKILL_NAME.fullmatch(bound_skill_name) is None
        ):
            raise RuntimeError("artifact_contract_invalid")
        return (bound_skill_name,) if bound_skill_name in contract_names else ()
    invoked = successful_root_skill_invocations(
        envelopes,
        native_session_id=native_session_id,
    )
    return tuple(name for name in invoked if name in contract_names)


def validate_deepseek_artifact_projection(value: object) -> dict[str, Any]:
    """Validate one immutable controller-to-native artifact gate projection."""

    required = {
        "schema", "native_session_id", "bound_skill_names",
        "workflow_run_name", "contracts", "workspace_before",
    }
    if not isinstance(value, dict) or set(value) != required or value.get(
        "schema"
    ) != "chatds.deepseek-artifact-gate.v1":
        raise ValueError("deepseek_artifact_projection_invalid")
    native_session_id = value.get("native_session_id")
    bound = value.get("bound_skill_names")
    contracts = value.get("contracts")
    baseline = value.get("workspace_before")
    workflow_run_name = value.get("workflow_run_name")
    if (
        not isinstance(native_session_id, str)
        or SAFE_NATIVE_SESSION_ID.fullmatch(native_session_id) is None
        or not isinstance(bound, list)
        or len(bound) > MAX_ARTIFACT_CONTRACTS
        or len(set(bound)) != len(bound)
        or any(
            not isinstance(name, str)
            or SAFE_SKILL_NAME.fullmatch(name) is None
            for name in bound
        )
        or not isinstance(contracts, list)
        or len(contracts) > MAX_ARTIFACT_CONTRACTS
        or not isinstance(baseline, dict)
        or len(baseline) > 200_000
        or workflow_run_name is not None
        and (
            not isinstance(workflow_run_name, str)
            or SAFE_WORKFLOW_RUN_NAME.fullmatch(workflow_run_name) is None
        )
    ):
        raise ValueError("deepseek_artifact_projection_invalid")
    contract_names: set[str] = set()
    normalized_contracts: list[dict[str, Any]] = []
    for row in contracts:
        if not isinstance(row, dict):
            raise ValueError("deepseek_artifact_projection_invalid")
        skill_name = row.get("skill_name")
        if (
            not isinstance(skill_name, str)
            or SAFE_SKILL_NAME.fullmatch(skill_name) is None
        ):
            raise ValueError("deepseek_artifact_projection_invalid")
        contract_names.add(skill_name)
        normalized_contracts.append(dict(row))
    if any(name not in contract_names for name in bound):
        raise ValueError("deepseek_artifact_projection_invalid")
    normalized_baseline: dict[str, tuple[int, ...]] = {}
    for relative, identity in baseline.items():
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\x00" in relative
            or len(relative.encode("utf-8")) > 1024
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(identity, (list, tuple))
            or len(identity) != 6
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in identity
            )
        ):
            raise ValueError("deepseek_artifact_projection_invalid")
        normalized_baseline[relative] = tuple(identity)
    normalized = {
        "schema": "chatds.deepseek-artifact-gate.v1",
        "native_session_id": native_session_id,
        "bound_skill_names": sorted(bound),
        "workflow_run_name": workflow_run_name,
        "contracts": normalized_contracts,
        "workspace_before": normalized_baseline,
    }
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("deepseek_artifact_projection_invalid") from exc
    if not encoded or len(encoded) > MAX_ARTIFACT_PROJECTION_BYTES:
        raise ValueError("deepseek_artifact_projection_invalid")
    return normalized


def compile_deepseek_artifact_projection(
    *,
    contracts: object,
    bound_skill_name: str | None,
    workflow_projection: object,
    native_session_id: str,
    workspace_before: dict[str, tuple[int, ...]],
) -> dict[str, Any] | None:
    """Bind selected Skill artifact contracts to the pre-Turn frontier."""

    if contracts in (None, []):
        return None
    if not isinstance(contracts, list):
        raise ValueError("deepseek_artifact_projection_invalid")
    bound: list[str] = []
    workflow_run_name: str | None = None
    if workflow_projection is not None:
        if not isinstance(workflow_projection, dict):
            raise ValueError("deepseek_artifact_projection_invalid")
        workflow_skill = workflow_projection.get("skill_name")
        workflow_run_name = workflow_projection.get("run_name")
        if (
            not isinstance(workflow_skill, str)
            or bound_skill_name is not None
            and workflow_skill != bound_skill_name
        ):
            raise ValueError("deepseek_artifact_projection_invalid")
        bound.append(workflow_skill)
    elif bound_skill_name is not None:
        bound.append(bound_skill_name)
    try:
        copied_contracts = json.loads(json.dumps(
            contracts,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("deepseek_artifact_projection_invalid") from exc
    return validate_deepseek_artifact_projection({
        "schema": "chatds.deepseek-artifact-gate.v1",
        "native_session_id": native_session_id,
        "bound_skill_names": bound,
        "workflow_run_name": workflow_run_name,
        "contracts": copied_contracts,
        "workspace_before": workspace_before,
    })


def evaluate_deepseek_artifact_projection(
    *,
    projection: object,
    invoked_skill_names: Iterable[str],
    workspace_root: Path,
) -> dict[str, Any]:
    """Evaluate the current artifact frontier from immutable native evidence."""

    contract = validate_deepseek_artifact_projection(projection)
    active = set(contract["bound_skill_names"])
    for name in invoked_skill_names:
        if not isinstance(name, str) or SAFE_SKILL_NAME.fullmatch(name) is None:
            raise ValueError("deepseek_artifact_projection_invalid")
        active.add(name)
    return validate_artifact_contracts(
        contracts=contract["contracts"],
        active_skill_name=None,
        active_skill_names=active,
        before=contract["workspace_before"],
        after=workspace_snapshot(workspace_root),
        workspace_root=workspace_root,
    )
