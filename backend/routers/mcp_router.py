"""Backend-owned MCP declarations for native per-Turn engine runtimes."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select

from auth import get_current_user
from database import get_db
from models import MCPServerRegistration, User
from native_mcp import (
    NativeMCPError,
    canonical_mcp_json,
    effective_mcp_servers,
    normalize_mcp_declaration,
    normalize_mcp_name,
)
from session_lifecycle import (
    require_owned_active_session,
    session_control_plane_mutation,
)


router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class MCPServerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    session_id: Optional[str] = None
    url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    transport: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    @field_validator("name")
    @classmethod
    def name_must_be_valid(cls, value: str) -> str:
        try:
            return normalize_mcp_name(value)
        except NativeMCPError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("transport")
    @classmethod
    def transport_must_be_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in {"http", "sse", "stdio"}:
            raise ValueError("transport must be one of: http, sse, stdio")
        return normalized

    @model_validator(mode="after")
    def declaration_must_be_native_compatible(self):
        try:
            normalize_mcp_declaration(self.declaration())
        except NativeMCPError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def declaration(self) -> dict[str, Any]:
        return {
            "type": self.transport,
            "url": self.url,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "headers": self.headers,
        }


def _public_server(
    name: str,
    declaration: dict[str, Any],
    *,
    scope: str,
) -> dict[str, Any]:
    """Never reflect credential-bearing headers, env, or argv to the UI."""

    return {
        "name": name,
        "transport": declaration["type"],
        "scope": scope,
        "connected": False,
        "connection_lifecycle": "isolated_per_turn",
        "tool_count": None,
    }


async def _exact_registration(
    db,
    *,
    user_id: str,
    session_id: str | None,
    name: str,
) -> MCPServerRegistration | None:
    scope_predicate = (
        MCPServerRegistration.session_id.is_(None)
        if session_id is None
        else MCPServerRegistration.session_id == session_id
    )
    return (await db.execute(
        select(MCPServerRegistration).where(
            MCPServerRegistration.user_id == user_id,
            scope_predicate,
            MCPServerRegistration.name == name,
        )
    )).scalar_one_or_none()


async def _upsert_registration(
    db,
    *,
    user_id: str,
    session_id: str | None,
    name: str,
    declaration: dict[str, Any],
) -> MCPServerRegistration:
    row = await _exact_registration(
        db,
        user_id=user_id,
        session_id=session_id,
        name=name,
    )
    payload = canonical_mcp_json(declaration)
    if row is None:
        row = MCPServerRegistration(
            user_id=user_id,
            session_id=session_id,
            name=name,
            config_json=payload,
        )
        db.add(row)
    else:
        row.config_json = payload
    await db.commit()
    await db.refresh(row)
    return row


@router.get("/servers")
async def list_servers(
    session_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(user.id)
    if session_id is not None:
        await require_owned_active_session(user_id, session_id)
        declarations = await effective_mcp_servers(
            db,
            user_id=user_id,
            session_id=session_id,
        )
        session_names = set((await db.execute(
            select(MCPServerRegistration.name).where(
                MCPServerRegistration.user_id == user_id,
                MCPServerRegistration.session_id == session_id,
            )
        )).scalars().all())
        await require_owned_active_session(user_id, session_id)
        return {
            "servers": [
                _public_server(
                    name,
                    declaration,
                    scope="session" if name in session_names else "user",
                )
                for name, declaration in declarations.items()
            ]
        }

    rows = list((await db.execute(
        select(MCPServerRegistration).where(
            MCPServerRegistration.user_id == user_id,
            MCPServerRegistration.session_id.is_(None),
        ).order_by(MCPServerRegistration.name)
    )).scalars().all())
    servers = []
    for row in rows:
        try:
            declaration = normalize_mcp_declaration(
                json.loads(row.config_json)
            )
        except (NativeMCPError, TypeError, ValueError) as exc:
            raise HTTPException(
                500,
                f"Stored MCP declaration is invalid: {row.name}",
            ) from exc
        servers.append(_public_server(row.name, declaration, scope="user"))
    return {"servers": servers}


@router.post("/servers")
async def add_server(
    config: MCPServerConfig,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(user.id)
    declaration = normalize_mcp_declaration(config.declaration())
    if config.session_id is None:
        from routers import skill_router
        async with skill_router._skill_install_lock(user_id, None):
            row = await _upsert_registration(
                db,
                user_id=user_id,
                session_id=None,
                name=config.name,
                declaration=declaration,
            )
    else:
        async with session_control_plane_mutation(
            user_id,
            config.session_id,
            acquire_skill_lock=True,
        ):
            row = await _upsert_registration(
                db,
                user_id=user_id,
                session_id=config.session_id,
                name=config.name,
                declaration=declaration,
            )
    return {
        "success": True,
        "server": _public_server(
            row.name,
            declaration,
            scope="session" if row.session_id is not None else "user",
        ),
    }


@router.delete("/servers/{name}")
async def delete_server(
    name: str,
    session_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    try:
        normalized_name = normalize_mcp_name(name)
    except NativeMCPError as exc:
        raise HTTPException(400, str(exc)) from exc
    user_id = str(user.id)

    async def remove() -> bool:
        row = await _exact_registration(
            db,
            user_id=user_id,
            session_id=session_id,
            name=normalized_name,
        )
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
        return True

    if session_id is None:
        from routers import skill_router
        async with skill_router._skill_install_lock(user_id, None):
            removed = await remove()
    else:
        async with session_control_plane_mutation(
            user_id,
            session_id,
            acquire_skill_lock=True,
        ):
            removed = await remove()
    return {"success": True, "removed": removed, "name": normalized_name}
