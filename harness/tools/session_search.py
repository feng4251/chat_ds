"""Session Search Tool — Long-Term Conversation Recall.

Single-shape tool with three calling modes (inferred from args):

  1. DISCOVERY — pass ``query``. Runs FTS5 on messages.content, returns
     top N conversations with snippets, message windows, and bookends.

  2. SCROLL — pass ``session_id`` + ``around_message_id``. Returns a window
     of messages centered on the anchor.

  3. BROWSE — no args. Returns recent conversations chronologically.

All three modes operate directly on the chat_ds SQLite database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB path — mounted at /app/data/chat_ds.db in container, fallback to local
# ---------------------------------------------------------------------------
_DB_PATH = Path("/app/data/chat_ds.db")
if not _DB_PATH.exists():
    _DB_PATH = Path("/nfs/yangbb/codes/chat_ds/data/chat_ds.db")

# ---------------------------------------------------------------------------
# FTS5 bootstrap — create virtual table if it doesn't exist
# ---------------------------------------------------------------------------

_FTS5_READY = False


def _ensure_fts5() -> bool:
    """Create FTS5 virtual table on messages.content if not present. Idempotent."""
    global _FTS5_READY
    if _FTS5_READY:
        return True
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        # Check if FTS table exists
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        if not row:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                "USING fts5(content, content_rowid='rowid', tokenize='unicode61')"
            )
            # Populate from existing messages
            conn.execute(
                "INSERT INTO messages_fts(rowid, content) "
                "SELECT rowid, content FROM messages WHERE content IS NOT NULL AND content != ''"
            )
            conn.commit()
            logger.info("Created and populated messages_fts FTS5 table")
        _FTS5_READY = True
        conn.close()
        return True
    except Exception as e:
        logger.warning("FTS5 bootstrap failed: %s — session_search will be limited", e)
        return False


def _get_conn() -> sqlite3.Connection | None:
    """Get a read-only connection to the chat_ds DB."""
    if not _DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error("Failed to open DB: %s", e)
        return None


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def _format_timestamp(ts) -> str:
    """Convert a DATETIME string or timestamp to a human-readable date."""
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
    except (ValueError, OSError, OverflowError):
        pass
    return str(ts)


def _shape_message(m: dict | sqlite3.Row, anchor_id: int | None = None) -> dict:
    """Slim a message row for the tool response."""
    if isinstance(m, sqlite3.Row):
        m = dict(m)
    entry: dict[str, Any] = {
        "id": m.get("id"),
        "role": m.get("role"),
        "content": m.get("content"),
        "timestamp": str(m.get("created_at", "")),
    }
    if m.get("reasoning"):
        entry["reasoning"] = m["reasoning"]
    if anchor_id is not None and str(m.get("id")) == str(anchor_id):
        entry["anchor"] = True
    return {k: v for k, v in entry.items() if v is not None or k == "content"}


# ---------------------------------------------------------------------------
# Browse: list recent conversations
# ---------------------------------------------------------------------------

def _list_recent_sessions(conn, limit: int, current_session_id: str = "") -> str:
    """Return metadata for the most recent conversations."""
    try:
        rows = conn.execute(
            "SELECT id, user_id, title, model_id, created_at, updated_at "
            "FROM conversations "
            "WHERE (? = '' OR id != ?) "
            "ORDER BY updated_at DESC "
            "LIMIT ?",
            (current_session_id, current_session_id, limit + 5),
        ).fetchall()

        results = []
        for row in rows:
            sid = row["id"]
            if current_session_id and sid == current_session_id:
                continue
            # Count messages
            count_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE conversation_id = ?",
                (sid,),
            ).fetchone()
            msg_count = count_row["cnt"] if count_row else 0
            # Get preview (first 150 chars of first user message)
            preview_row = conn.execute(
                "SELECT content FROM messages "
                "WHERE conversation_id = ? AND role = 'user' "
                "ORDER BY created_at ASC LIMIT 1",
                (sid,),
            ).fetchone()
            preview = ""
            if preview_row and preview_row["content"]:
                preview = preview_row["content"][:150]
            results.append({
                "session_id": sid,
                "title": row["title"] or None,
                "model_id": row["model_id"],
                "started_at": str(row["created_at"]),
                "last_active": str(row["updated_at"]),
                "message_count": msg_count,
                "preview": preview,
            })
            if len(results) >= limit:
                break

        return json.dumps({
            "success": True,
            "mode": "browse",
            "results": results,
            "count": len(results),
            "message": (
                f"Showing {len(results)} most recent conversations. "
                "Pass query= to search, or session_id+around_message_id to scroll."
            ),
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Error listing recent conversations: %s", e, exc_info=True)
        return json.dumps({"error": f"Failed to list recent conversations: {e}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Scroll: messages around an anchor
# ---------------------------------------------------------------------------

def _scroll(
    conn,
    session_id: str,
    around_message_id: int,
    window: int = 5,
) -> str:
    """Return a window of messages centered on around_message_id."""
    if not session_id or not session_id.strip():
        return json.dumps({"error": "scroll requires session_id"}, ensure_ascii=False)
    session_id = session_id.strip()

    try:
        around_message_id = int(around_message_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "scroll requires integer around_message_id"}, ensure_ascii=False)

    window = max(1, min(window, 20))

    # Verify conversation exists
    conv = conn.execute(
        "SELECT id, title, model_id, created_at FROM conversations WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not conv:
        return json.dumps({"error": f"session_id not found: {session_id}"}, ensure_ascii=False)

    # Fetch window: messages before anchor
    before = conn.execute(
        "SELECT id, role, content, reasoning, created_at FROM messages "
        "WHERE conversation_id = ? AND rowid < (SELECT rowid FROM messages WHERE id = ?) "
        "ORDER BY rowid DESC LIMIT ?",
        (session_id, str(around_message_id), window),
    ).fetchall()
    before = list(reversed(before))

    # Anchor message
    anchor = conn.execute(
        "SELECT id, role, content, reasoning, created_at FROM messages "
        "WHERE id = ? AND conversation_id = ?",
        (str(around_message_id), session_id),
    ).fetchone()

    if not anchor:
        return json.dumps(
            {"error": f"around_message_id {around_message_id} not in session {session_id}"},
            ensure_ascii=False,
        )

    # Messages after anchor
    after = conn.execute(
        "SELECT id, role, content, reasoning, created_at FROM messages "
        "WHERE conversation_id = ? AND rowid > (SELECT rowid FROM messages WHERE id = ?) "
        "ORDER BY rowid ASC LIMIT ?",
        (session_id, str(around_message_id), window),
    ).fetchall()

    messages = before + [anchor] + after

    # Count remaining on each side
    before_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM messages "
        "WHERE conversation_id = ? AND rowid < (SELECT rowid FROM messages WHERE id = ?)",
        (session_id, str(around_message_id)),
    ).fetchone()
    after_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM messages "
        "WHERE conversation_id = ? AND rowid > (SELECT rowid FROM messages WHERE id = ?)",
        (session_id, str(around_message_id)),
    ).fetchone()

    return json.dumps({
        "success": True,
        "mode": "scroll",
        "session_id": session_id,
        "around_message_id": around_message_id,
        "session_meta": {
            "when": _format_timestamp(conv["created_at"]),
            "model": conv["model_id"],
            "title": conv["title"],
        },
        "window": window,
        "messages": [_shape_message(m, anchor_id=around_message_id) for m in messages],
        "messages_before": before_count["cnt"] if before_count else 0,
        "messages_after": after_count["cnt"] if after_count else 0,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Discovery: FTS5 search
# ---------------------------------------------------------------------------

def _discover(
    conn,
    query: str,
    role_filter: list[str] | None,
    limit: int,
    sort: str | None,
    current_session_id: str = "",
) -> str:
    """Discovery: FTS5 search with anchored window + bookends per hit."""
    if not _ensure_fts5():
        return json.dumps({
            "error": "FTS5 search is unavailable. Please use browse (no args) to see recent conversations.",
        }, ensure_ascii=False)

    roles = role_filter if role_filter else ["user", "assistant"]
    role_placeholders = ",".join("?" for _ in roles)

    try:
        # FTS5 search
        fts_rows = conn.execute(
            f"SELECT m.id, m.conversation_id, m.role, m.content, m.created_at, "
            f"  snippet(messages_fts, 1, '<b>', '</b>', '...', 40) as snippet "
            f"FROM messages_fts fts "
            f"JOIN messages m ON m.rowid = fts.rowid "
            f"WHERE messages_fts MATCH ? AND m.role IN ({role_placeholders}) "
            f"ORDER BY rank LIMIT 50",
            (query, *roles),
        ).fetchall()
    except Exception as e:
        logger.error("FTS5 search failed: %s", e, exc_info=True)
        return json.dumps({"error": f"Search failed: {e}"}, ensure_ascii=False)

    if not fts_rows:
        return json.dumps({
            "success": True,
            "mode": "discover",
            "query": query,
            "results": [],
            "count": 0,
            "message": "No matching conversations found.",
        }, ensure_ascii=False)

    # Dedupe by conversation_id, skip current session
    seen_convs: dict[str, dict] = {}
    for row in fts_rows:
        cid = row["conversation_id"]
        if current_session_id and cid == current_session_id:
            continue
        if cid not in seen_convs:
            seen_convs[cid] = dict(row)
        if len(seen_convs) >= limit:
            break

    results = []
    for cid, match_info in seen_convs.items():
        msg_id = match_info["id"]

        # Get session metadata
        conv = conn.execute(
            "SELECT id, title, model_id, created_at FROM conversations WHERE id = ?",
            (cid,),
        ).fetchone()

        # Get ±window messages around the match
        window = 5
        before = conn.execute(
            "SELECT id, role, content, reasoning, created_at FROM messages "
            "WHERE conversation_id = ? AND rowid < (SELECT rowid FROM messages WHERE id = ?) "
            "ORDER BY rowid DESC LIMIT ?",
            (cid, str(msg_id), window),
        ).fetchall()
        before = list(reversed(before))

        anchor = conn.execute(
            "SELECT id, role, content, reasoning, created_at FROM messages "
            "WHERE id = ?",
            (str(msg_id),),
        ).fetchone()

        after = conn.execute(
            "SELECT id, role, content, reasoning, created_at FROM messages "
            "WHERE conversation_id = ? AND rowid > (SELECT rowid FROM messages WHERE id = ?) "
            "ORDER BY rowid ASC LIMIT ?",
            (cid, str(msg_id), window),
        ).fetchall()

        messages = before + ([anchor] if anchor else []) + after

        # Bookends: first 3 user+assistant messages
        bookend_start = conn.execute(
            "SELECT id, role, content, reasoning, created_at FROM messages "
            "WHERE conversation_id = ? AND role IN ('user', 'assistant') "
            "ORDER BY rowid ASC LIMIT 3",
            (cid,),
        ).fetchall()

        # Bookends: last 3 user+assistant messages
        bookend_end = conn.execute(
            "SELECT id, role, content, reasoning, created_at FROM messages "
            "WHERE conversation_id = ? AND role IN ('user', 'assistant') "
            "ORDER BY rowid DESC LIMIT 3",
            (cid,),
        ).fetchall()
        bookend_end = list(reversed(bookend_end))

        # Count messages before/after in this window
        before_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages "
            "WHERE conversation_id = ? AND rowid < (SELECT rowid FROM messages WHERE id = ?)",
            (cid, str(msg_id)),
        ).fetchone()
        after_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM messages "
            "WHERE conversation_id = ? AND rowid > (SELECT rowid FROM messages WHERE id = ?)",
            (cid, str(msg_id)),
        ).fetchone()

        entry = {
            "session_id": cid,
            "when": _format_timestamp(conv["created_at"] if conv else match_info.get("created_at")),
            "model": conv["model_id"] if conv else "unknown",
            "title": conv["title"] if conv else None,
            "matched_role": match_info.get("role"),
            "match_message_id": msg_id,
            "snippet": match_info.get("snippet", ""),
            "bookend_start": [_shape_message(m) for m in bookend_start],
            "messages": [_shape_message(m, anchor_id=msg_id) for m in messages],
            "bookend_end": [_shape_message(m) for m in bookend_end],
            "messages_before": before_count["cnt"] if before_count else 0,
            "messages_after": after_count["cnt"] if after_count else 0,
        }
        results.append(entry)

    return json.dumps({
        "success": True,
        "mode": "discover",
        "query": query,
        "results": results,
        "count": len(results),
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main tool handler
# ---------------------------------------------------------------------------

async def session_search(
    query: str = "",
    limit: int = 3,
    sort: str | None = None,
    session_id: str = "",
    around_message_id: int | None = None,
    window: int = 5,
    role_filter: str = "",
    # Internal params (injected by dispatch)
    _current_session_id: str = "",
) -> str:
    """Single-shape tool. Mode inferred from which args are set.

    Discovery: pass ``query``.
    Scroll:    pass ``session_id`` + ``around_message_id``.
    Browse:    pass nothing.
    """
    conn = _get_conn()
    if conn is None:
        return json.dumps({
            "error": "Session database is not available. The data volume may not be mounted.",
        }, ensure_ascii=False)

    try:
        # Scroll shape — explicit anchor beats any query
        if session_id and session_id.strip() and around_message_id is not None:
            return _scroll(conn, session_id, around_message_id, window)

        # Limit clamp
        limit = max(1, min(int(limit), 10))

        # Browse shape
        if not query or not isinstance(query, str) or not query.strip():
            return _list_recent_sessions(conn, limit, _current_session_id)

        # Parse role_filter
        role_list: list[str] | None = None
        if role_filter and role_filter.strip():
            role_list = [r.strip() for r in role_filter.split(",") if r.strip()]

        # Normalise sort
        sort_norm: str | None = None
        if sort and isinstance(sort, str):
            candidate = sort.strip().lower()
            if candidate in ("newest", "oldest"):
                sort_norm = candidate

        return _discover(conn, query.strip(), role_list, limit, sort_norm, _current_session_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": (
        "Search past conversations stored in the local session database, or scroll "
        "inside one. FTS5-backed full-text retrieval over message content. No LLM "
        "calls — every shape returns actual messages from the DB.\n\n"
        "THREE CALLING SHAPES\n\n"
        "  1) DISCOVERY — pass `query`:\n"
        "     session_search(query=\"auth refactor\", limit=3)\n"
        "     Runs FTS5, returns top N conversations with snippet, ±5 message window "
        "around the match, and bookends (first/last 3 messages of the conversation).\n\n"
        "  2) SCROLL — pass `session_id` + `around_message_id`:\n"
        "     session_search(session_id=\"...\", around_message_id=\"abc123\", window=10)\n"
        "     Returns ±window messages centered on the anchor. Use after discovery "
        "when you need more context.\n"
        "       - Scroll FORWARD: pass messages[-1].id as around_message_id\n"
        "       - Scroll BACKWARD: pass messages[0].id as around_message_id\n\n"
        "  3) BROWSE — no args:\n"
        "     session_search()\n"
        "     Returns recent conversations: titles, previews, timestamps.\n\n"
        "FTS5 SYNTAX\n\n"
        "  Multi-word queries require all terms (AND is default). Use OR for broader "
        "recall, quoted phrases for exact match, or * prefix wildcards.\n\n"
        "WHEN TO USE\n\n"
        "  For \"what did we discuss about X\" / \"find the conversation where Z\" "
        "questions — before web search or filesystem inspection."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query (discovery shape). Keywords, phrases, or boolean "
                    "expressions. Omit to browse recent conversations."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Discovery shape only. Max conversations (default 3, max 10).",
                "default": 3,
            },
            "sort": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": "Discovery shape only. 'newest' for recency, 'oldest' for origin.",
            },
            "session_id": {
                "type": "string",
                "description": "Scroll shape. Conversation ID from a discovery result.",
            },
            "around_message_id": {
                "type": "string",
                "description": "Scroll shape. Message ID to center the window on.",
            },
            "window": {
                "type": "integer",
                "description": "Scroll shape only. Messages per side of anchor [1-20]. Default 5.",
                "default": 5,
            },
            "role_filter": {
                "type": "string",
                "description": "Optional. Comma-separated roles. Defaults to 'user,assistant'.",
            },
        },
        "required": [],
    },
}