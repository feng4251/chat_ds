"""Authenticated MCP control-plane API.

The backend never edits MCP files directly. All mutations are delegated to the
harness, which owns persistence, connection state, and the session tool catalog.
"""

from __future__ import annotations

from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from auth import get_current_user
from config import settings
from models import User
from session_lifecycle import (
    require_owned_active_session,
    session_control_plane_mutation,
)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class MCPServerConfig(BaseModel):
    name: str
    session_id: Optional[str] = None
    url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    transport: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    timeout: int = 120

    @field_validator("name")
    @classmethod
    def name_must_be_valid(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 64:
            raise ValueError("name must contain 1-64 characters")
        return value

    @field_validator("transport")
    @classmethod
    def transport_must_be_valid(cls, value: str | None) -> str | None:
        if value is not None and value not in ("http", "sse", "stdio"):
            raise ValueError("transport must be one of: http, sse, stdio")
        return value


async def _harness_request(method: str, path: str, **kwargs) -> dict:
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-Internal-Token"] = settings.internal_api_token
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.request(
                method,
                settings.harness_url + path,
                headers=headers,
                **kwargs,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Harness unavailable: {exc}")
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text[:500],
        )
    return response.json()


@router.get("/servers")
async def list_servers(
    session_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
):
    scoped_session = session_id or "default"
    if scoped_session != "default":
        await require_owned_active_session(str(user.id), scoped_session)
    result = await _harness_request(
        "GET",
        "/internal/mcp/servers",
        params={
            "user_id": str(user.id),
            "session_id": scoped_session,
        },
    )
    if scoped_session != "default":
        await require_owned_active_session(str(user.id), scoped_session)
    return result


@router.post("/servers")
async def add_server(
    config: MCPServerConfig,
    user: User = Depends(get_current_user),
):
    if not config.url and not config.command:
        raise HTTPException(
            status_code=400,
            detail="Either url or command is required.",
        )
    payload = config.model_dump(exclude={"session_id"}, exclude_none=True)
    payload.update(
        user_id=str(user.id),
        session_id=config.session_id or "default",
    )
    scoped_session = config.session_id or "default"
    if scoped_session == "default":
        return await _harness_request(
            "POST", "/internal/mcp/server/add", json=payload
        )
    # Use the same Skill -> conversation lock order as install/fork/delete.
    # The Harness call is retained inside the lease so deletion cannot finish
    # while an MCP configuration write is in flight.
    async with session_control_plane_mutation(
        str(user.id),
        scoped_session,
        acquire_skill_lock=True,
    ):
        return await _harness_request(
            "POST", "/internal/mcp/server/add", json=payload
        )


@router.delete("/servers/{name}")
async def delete_server(
    name: str,
    session_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
):
    scoped_session = session_id or "default"
    payload = {
        "name": name,
        "user_id": str(user.id),
        "session_id": scoped_session,
    }
    if scoped_session == "default":
        return await _harness_request(
            "POST",
            "/internal/mcp/server/remove",
            json=payload,
        )
    async with session_control_plane_mutation(
        str(user.id),
        scoped_session,
        acquire_skill_lock=True,
    ):
        return await _harness_request(
            "POST",
            "/internal/mcp/server/remove",
            json=payload,
        )
