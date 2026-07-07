"""User-configurable lifecycle webhooks with bounded, signed delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from config import settings
from database import async_session
from models import EventHook

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()


def _url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if settings.allow_private_hook_urls:
        return True
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            return False
    return True


async def _deliver(
    hook: EventHook,
    event_type: str,
    payload: dict,
) -> None:
    if not _url_allowed(hook.url):
        logger.warning("Blocked unsafe hook URL for hook=%s", hook.id)
        return
    body = json.dumps({
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hook_id": hook.id,
        "data": payload,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "chat-ds-hooks/1.0",
    }
    if hook.secret:
        headers["X-Chat-DS-Signature"] = "sha256=" + hmac.new(
            hook.secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=settings.hook_timeout_seconds) as client:
            response = await client.post(hook.url, content=body, headers=headers)
        if response.status_code >= 400:
            logger.warning(
                "Hook delivery failed hook=%s event=%s status=%s",
                hook.id, event_type, response.status_code,
            )
    except Exception:
        logger.exception("Hook delivery error hook=%s event=%s", hook.id, event_type)


async def emit_event(
    user_id: str,
    event_type: str,
    payload: dict,
    conversation_id: str | None = None,
) -> None:
    """Schedule matching hooks without delaying the caller."""
    async with async_session() as db:
        hooks = (await db.execute(
            select(EventHook).where(
                EventHook.user_id == user_id,
                EventHook.enabled.is_(True),
            )
        )).scalars().all()
    for hook in hooks:
        if hook.conversation_id and hook.conversation_id != conversation_id:
            continue
        try:
            events = json.loads(hook.events)
        except (TypeError, json.JSONDecodeError):
            continue
        if event_type not in events and "*" not in events:
            continue
        task = asyncio.create_task(_deliver(hook, event_type, payload))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
