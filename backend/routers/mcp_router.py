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
from models import User

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
HARNESS_URL = "http://harness:8020"


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
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.request(method, HARNESS_URL + path, **kwargs)
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
    return await _harness_request(
        "GET",
        "/internal/mcp/servers",
        params={
            "user_id": str(user.id),
            "session_id": session_id or "default",
        },
    )


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
    return await _harness_request(
        "POST", "/internal/mcp/server/add", json=payload
    )


@router.delete("/servers/{name}")
async def delete_server(
    name: str,
    session_id: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
):
    return await _harness_request(
        "POST",
        "/internal/mcp/server/remove",
        json={
            "name": name,
            "user_id": str(user.id),
            "session_id": session_id or "default",
        },
    )
