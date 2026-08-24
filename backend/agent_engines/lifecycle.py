"""Cross-process recovery for persisted native Agent Engine sessions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import func, select

from config import settings
from database import async_session
from models import (
    AgentEngineRawEvent,
    AgentEngineSession,
    AgentRun,
    AgentRunEvent,
    Message,
)

from .base import ENGINE_ID_CLAUDE_CODE, ENGINE_ID_DEEPSEEK_HARNESS
from .registry import build_agent_engine_registry


ACTIVE_RUN_STATUSES = frozenset({
    "pending", "planned", "queued", "running", "committing",
})


def _deepseek_terminal_receipt(
    envelope: Mapping[str, Any] | None,
) -> tuple[int, str, dict[str, Any]] | None:
    if not isinstance(envelope, Mapping):
        return None
    sequence = envelope.get("seq")
    event = envelope.get("event")
    if (
        not isinstance(sequence, int)
        or sequence < 1
        or not isinstance(event, Mapping)
        or event.get("type") != "chatds.supervisor.terminal"
    ):
        return None
    status = str(event.get("status") or "")
    if status not in {"succeeded", "failed", "cancelled"}:
        return None
    return sequence, status, dict(event)


async def _project_recovered_deepseek_terminal(
    db,
    *,
    run: AgentRun,
    envelope: Mapping[str, Any],
) -> None:
    """Replay one supervisor terminal without manufacturing native success."""

    receipt = _deepseek_terminal_receipt(envelope)
    if receipt is None:
        raise RuntimeError("deepseek_recovered_terminal_invalid")
    sequence, native_status, native_event = receipt
    serialized = json.dumps(
        dict(envelope),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    raw = (await db.execute(
        select(AgentEngineRawEvent).where(
            AgentEngineRawEvent.run_id == run.id,
            AgentEngineRawEvent.seq == sequence,
        )
    )).scalar_one_or_none()
    if raw is None:
        db.add(AgentEngineRawEvent(
            user_id=run.user_id,
            conversation_id=run.conversation_id,
            run_id=run.id,
            engine_id=ENGINE_ID_DEEPSEEK_HARNESS,
            seq=sequence,
            native_event_type="chatds.supervisor.terminal",
            payload=serialized,
            payload_sha256=digest,
        ))
    elif raw.payload_sha256 != digest:
        raise RuntimeError("deepseek_recovered_terminal_conflict")

    if native_status == "succeeded" and run.status != "succeeded":
        projected_status = "failed"
        event_type = "run.failed"
        finish_reason = "terminal_projection_interrupted"
        error = (
            "DeepSeek Harness completed, but Backend restarted before the "
            "assistant response and controller-owned effects were committed."
        )
    elif native_status == "succeeded":
        projected_status = "succeeded"
        event_type = "run.completed"
        finish_reason = "stop"
        error = None
    elif native_status == "cancelled":
        projected_status = "cancelled"
        event_type = "run.cancelled"
        finish_reason = "cancelled"
        error = None
    else:
        projected_status = "failed"
        event_type = "run.failed"
        finish_reason = "failed"
        error = str(native_event.get("error") or "deepseek_native_run_failed")[:4000]

    existing_terminal = (await db.execute(
        select(AgentRunEvent.id).where(
            AgentRunEvent.conversation_id == run.conversation_id,
            AgentRunEvent.run_id == run.id,
            AgentRunEvent.event_type.in_(
                ("run.completed", "run.failed", "run.cancelled")
            ),
        ).limit(1)
    )).scalar_one_or_none()
    if existing_terminal is None:
        next_sequence = int((await db.execute(
            select(func.max(AgentRunEvent.seq)).where(
                AgentRunEvent.conversation_id == run.conversation_id,
                AgentRunEvent.run_id == run.id,
            )
        )).scalar_one() or 0) + 1
        payload = {
            "authoritative": True,
            "finish_reason": finish_reason,
            "terminal_reason": error or finish_reason,
            "error": error,
            "recovery_source": "supervisor_terminal_replay",
            "native_terminal_seq": sequence,
        }
        if native_event.get("error_stage"):
            payload["error_stage"] = str(native_event["error_stage"])[:128]
        db.add(AgentRunEvent(
            id=uuid.uuid4().hex,
            run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=run.user_id,
            parent_run_id=run.parent_run_id,
            seq=next_sequence,
            event_type=event_type,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ))

    run.status = projected_status
    run.finish_reason = finish_reason
    run.error = error
    run.ended_at = run.ended_at or datetime.utcnow()
    if projected_status == "failed":
        latest_role = (await db.execute(
            select(Message.role).where(
                Message.conversation_id == run.conversation_id
            ).order_by(Message.created_at.desc(), Message.id.desc()).limit(1)
        )).scalar_one_or_none()
        if latest_role == "user":
            db.add(Message(
                id=uuid.uuid4().hex,
                conversation_id=run.conversation_id,
                role="assistant",
                content=(
                    "⚠️ 本次任务执行失败："
                    f"{error or finish_reason}。原生终态已从会话账本恢复；"
                    "现有草稿和产物已保留。"
                ),
                model_id=run.resolved_model_id or run.requested_model_id,
                source=run.source,
                created_at=datetime.utcnow(),
            ))


async def revoke_stale_native_runs_on_backend_startup() -> int:
    """Reconcile the only crash window between projection and checkpoint commit.

    A successful native Turn is promoted only when both the durable AgentRun
    projection and the lossless Supervisor terminal agree on success. All
    other active mappings are revoked before the Backend starts accepting new
    Turns. This preserves a committed candidate across a process crash without
    ever resuming an unprojected or partially audited transcript.
    """

    if not (
        settings.claude_code_engine_enabled
        or settings.deepseek_harness_engine_enabled
    ):
        return 0
    registry = build_agent_engine_registry()
    async with async_session() as db:
        revoked = 0
        if settings.claude_code_engine_enabled:
            rows = (await db.execute(
                select(AgentEngineSession).where(
                    AgentEngineSession.engine_id == ENGINE_ID_CLAUDE_CODE,
                    AgentEngineSession.active_run_id.is_not(None),
                )
            )).scalars().all()
            engine = registry.get(ENGINE_ID_CLAUDE_CODE)
            for row in rows:
                active_run_id = str(row.active_run_id)
                run = await db.get(AgentRun, active_run_id)
                terminal_rows = list((await db.execute(
                    select(AgentEngineRawEvent)
                    .where(
                        AgentEngineRawEvent.run_id == active_run_id,
                        AgentEngineRawEvent.engine_id == ENGINE_ID_CLAUDE_CODE,
                        AgentEngineRawEvent.native_event_type
                        == "chatds.supervisor.terminal",
                    )
                    .order_by(AgentEngineRawEvent.seq)
                )).scalars().all())
                terminal_status, result_succeeded, checkpoint_observed = (
                    _raw_terminal_receipt(terminal_rows[0])
                    if len(terminal_rows) == 1
                    else (None, False, False)
                )
                if (
                    run is not None
                    and run.status == "succeeded"
                    and run.native_session_id
                    and terminal_status == "succeeded"
                ):
                    row.native_session_id = run.native_session_id
                    row.generation += 1
                    row.status = "idle"
                    row.active_run_id = None
                    row.last_event_seq = int((await db.execute(
                        select(func.max(AgentEngineRawEvent.seq)).where(
                            AgentEngineRawEvent.run_id == active_run_id
                        )
                    )).scalar_one() or 0)
                    row.error = None
                    continue
                if (
                    run is not None
                    and run.status == "failed"
                    and run.native_session_id
                    and terminal_status == "failed"
                    and result_succeeded
                    and checkpoint_observed
                ):
                    row.native_session_id = run.native_session_id
                    row.generation += 1
                    row.status = "failed"
                    row.active_run_id = None
                    row.last_event_seq = int((await db.execute(
                        select(func.max(AgentEngineRawEvent.seq)).where(
                            AgentEngineRawEvent.run_id == active_run_id
                        )
                    )).scalar_one() or 0)
                    row.error = run.error
                    continue
                if run is not None and run.status == "succeeded":
                    run.status = "failed"
                    run.finish_reason = "native_audit_incomplete"
                    run.error = (
                        "A durably projected Claude Code success had no unique "
                        "matching native terminal checkpoint during startup."
                    )
                success = await engine.cancel_run(
                    user_id=row.user_id,
                    conversation_id=row.conversation_id,
                    run_id=active_run_id,
                )
                if not success:
                    raise RuntimeError(
                        "A stale Claude Runner could not be revoked during Backend startup"
                    )
                if run is not None and run.status in ACTIVE_RUN_STATUSES:
                    next_sequence = int((await db.execute(
                        select(func.max(AgentRunEvent.seq)).where(
                            AgentRunEvent.conversation_id == run.conversation_id,
                            AgentRunEvent.run_id == run.id,
                        )
                    )).scalar_one() or 0) + 1
                    db.add(AgentRunEvent(
                        id=uuid.uuid4().hex,
                        run_id=run.id,
                        conversation_id=run.conversation_id,
                        user_id=run.user_id,
                        parent_run_id=run.parent_run_id,
                        seq=next_sequence,
                        event_type="run.cancelled",
                        payload=json.dumps({
                            "authoritative": True,
                            "finish_reason": "backend_process_restart",
                            "terminal_reason": "backend_process_restart",
                            "cancellation_source": "native_startup_revocation",
                        }, separators=(",", ":")),
                    ))
                    run.status = "cancelled"
                    run.finish_reason = "backend_process_restart"
                    run.error = None
                    run.ended_at = datetime.utcnow()
                row.status = (
                    str(run.status)
                    if run is not None and run.status in {"failed", "cancelled"}
                    else "cancelled"
                )
                row.active_run_id = None
                row.error = (
                    run.error
                    if run is not None and run.status in {"failed", "cancelled"}
                    else "backend_process_restart"
                )
                revoked += 1

        if settings.deepseek_harness_engine_enabled:
            deepseek_runs = (await db.execute(
                select(AgentRun).where(
                    AgentRun.engine_id == ENGINE_ID_DEEPSEEK_HARNESS,
                    AgentRun.status.in_(tuple(ACTIVE_RUN_STATUSES)),
                )
            )).scalars().all()
            engine = registry.get(ENGINE_ID_DEEPSEEK_HARNESS)
            for run in deepseek_runs:
                root_run_id = str(run.root_run_id or run.id)
                if run.parent_run_id is not None and run.id != root_run_id:
                    continue
                terminal = await engine.recover_terminal(
                    user_id=run.user_id,
                    conversation_id=run.conversation_id,
                    run_id=run.id,
                )
                if terminal is None:
                    success = await engine.cancel_run(
                        user_id=run.user_id,
                        conversation_id=run.conversation_id,
                        run_id=run.id,
                    )
                    if not success:
                        raise RuntimeError(
                            "A stale DeepSeek Harness run could not be revoked "
                            "during Backend startup"
                        )
                    terminal = await engine.recover_terminal(
                        user_id=run.user_id,
                        conversation_id=run.conversation_id,
                        run_id=run.id,
                    )
                if terminal is None:
                    raise RuntimeError(
                        "A revoked DeepSeek Harness run has no durable terminal"
                    )
                await _project_recovered_deepseek_terminal(
                    db, run=run, envelope=terminal
                )
                revoked += 1
        await db.commit()
        return revoked


def _raw_terminal_receipt(
    row: AgentEngineRawEvent | None,
) -> tuple[str | None, bool, bool]:
    if row is None:
        return None, False, False
    try:
        envelope = json.loads(row.payload)
    except (TypeError, ValueError):
        return None, False, False
    event = envelope.get("event") if isinstance(envelope, dict) else None
    if not isinstance(event, dict):
        return None, False, False
    status = str(event.get("status") or "")
    return (
        status if status in {"succeeded", "failed", "cancelled"} else None,
        event.get("result_succeeded") is True,
        event.get("checkpoint_observed") is True,
    )
