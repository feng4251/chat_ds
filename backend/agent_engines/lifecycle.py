"""Cross-process recovery for persisted native Agent Engine sessions."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from config import settings
from database import async_session
from models import AgentEngineRawEvent, AgentEngineSession, AgentRun

from .base import ENGINE_ID_CLAUDE_CODE
from .registry import build_agent_engine_registry


async def revoke_stale_native_runs_on_backend_startup() -> int:
    """Reconcile the only crash window between projection and checkpoint commit.

    A successful native Turn is promoted only when both the durable AgentRun
    projection and the lossless Supervisor terminal agree on success. All
    other active mappings are revoked before the Backend starts accepting new
    Turns. This preserves a committed candidate across a process crash without
    ever resuming an unprojected or partially audited transcript.
    """

    if not settings.claude_code_engine_enabled:
        return 0
    async with async_session() as db:
        rows = (await db.execute(
            select(AgentEngineSession).where(
                AgentEngineSession.engine_id == ENGINE_ID_CLAUDE_CODE,
                AgentEngineSession.active_run_id.is_not(None),
            )
        )).scalars().all()
        if not rows:
            return 0
        engine = build_agent_engine_registry().get(ENGINE_ID_CLAUDE_CODE)
        revoked = 0
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
            terminal_status = (
                _raw_terminal_status(terminal_rows[0])
                if len(terminal_rows) == 1
                else None
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
        await db.commit()
        return revoked


def _raw_terminal_status(row: AgentEngineRawEvent | None) -> str | None:
    if row is None:
        return None
    try:
        envelope = json.loads(row.payload)
    except (TypeError, ValueError):
        return None
    event = envelope.get("event") if isinstance(envelope, dict) else None
    if not isinstance(event, dict):
        return None
    status = str(event.get("status") or "")
    return status if status in {"succeeded", "failed", "cancelled"} else None
