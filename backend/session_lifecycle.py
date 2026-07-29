"""Shared authority boundary for session-scoped control-plane mutations."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session
from models import Conversation
from workspace import require_session_workspace_active
from workspace_lock import WorkspaceMutationLockError


KNOWN_SOURCE_SKILL_TRY_TIMEOUT_SECONDS = 0.25


@asynccontextmanager
async def session_skill_lifecycle_lock(
    user_id: str,
    session_id: str,
    *,
    bounded: bool = False,
):
    """Acquire the canonical session Skill lock, optionally fail-fast.

    Known self/cross calls originate inside an already-held source turn. They
    must never wait indefinitely for a Skill owner that may itself be waiting
    for that turn's maintenance lease.
    """

    from routers import skill_router

    lock = skill_router._skill_install_lock(
        str(user_id),
        str(session_id),
    )
    acquired = False
    try:
        if bounded:
            try:
                await asyncio.wait_for(
                    lock.acquire(),
                    timeout=KNOWN_SOURCE_SKILL_TRY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise WorkspaceMutationLockError(
                    "Session lifecycle lock is busy.",
                    code="workspace_lock_timeout",
                ) from exc
        else:
            await lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()


async def require_owned_active_session(
    user_id: str,
    session_id: str,
) -> Conversation:
    """Resolve one owner from a fresh snapshot and reject lifecycle fences."""

    safe_user = str(user_id)
    safe_session = str(session_id)
    async with async_session() as db:
        conversation = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == safe_session,
                    Conversation.user_id == safe_user,
                )
            )
        ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(404, "Conversation not found")
    # Ownership is intentionally resolved before the filesystem coordinate is
    # inspected: a caller cannot use another user's or a nonexistent session
    # identifier as a workspace-marker oracle.
    require_session_workspace_active(safe_user, safe_session)
    return conversation


@asynccontextmanager
async def session_control_plane_mutation(
    user_id: str,
    session_id: str,
    *,
    source_session_id: str | None = None,
    acquire_skill_lock: bool = True,
    acquire_maintenance_lease: bool = True,
) -> AsyncIterator[tuple[AsyncSession, Conversation]]:
    """Linearize a control-plane write with fork/delete/chat maintenance.

    The owner query is made from a session opened only after all requested
    lifecycle locks are held. Pending and deletion fences are checked both
    around authority resolution and after the mutation body. Callers that
    commit database state should additionally check the fence immediately
    before that commit.
    """

    safe_user = str(user_id)
    safe_session = str(session_id)
    known_source = (
        str(source_session_id)
        if source_session_id is not None
        else None
    )
    if known_source is not None:
        # A chat turn already owns its source maintenance lease. Acquiring a
        # different target maintenance lease synchronously would allow the
        # classic A-turn -> B-turn / B-turn -> A-turn ABBA deadlock. Cross
        # mutations instead take only the target's canonical Skill lifecycle
        # lock, which is also the fork/delete pending-marker authority. A
        # self-call reuses its already-held turn and needs no additional lock.
        acquire_maintenance_lease = False
        acquire_skill_lock = True
    from routers.chat_router import conversation_maintenance_lease

    async with AsyncExitStack() as locks:
        if acquire_skill_lock:
            await locks.enter_async_context(
                session_skill_lifecycle_lock(
                    safe_user,
                    safe_session,
                    bounded=known_source is not None,
                )
            )
        if acquire_maintenance_lease:
            await locks.enter_async_context(
                conversation_maintenance_lease(safe_session)
            )
        async with async_session() as db:
            conversation = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.id == safe_session,
                        Conversation.user_id == safe_user,
                    )
                )
            ).scalar_one_or_none()
            if conversation is None:
                raise HTTPException(404, "Conversation not found")
            require_session_workspace_active(safe_user, safe_session)
            try:
                yield db, conversation
                require_session_workspace_active(safe_user, safe_session)
            except BaseException:
                # Roll back any still-open unit of work before releasing the
                # target lifecycle lock. A prior successful commit remains
                # durable, but cross-session fork/delete cannot publish a new
                # fence while the canonical target Skill lock is held.
                try:
                    await db.rollback()
                except BaseException:
                    pass
                raise
