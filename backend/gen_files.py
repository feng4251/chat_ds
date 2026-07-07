#!/usr/bin/env python3
"""Generate all backend router files and main.py."""
import os

BASE = "/nfs/yangbb/codes/chat_ds/backend"

def write(path, content):
    full = os.path.join(BASE, path)
    with open(full, "w") as f:
        f.write(content)
    print(f"  wrote {path}")

# -- conv_router --
write("routers/conv_router.py", """import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import User, Conversation, Message
from schemas import ConversationOut, ConversationTitle
from auth import get_current_user

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

@router.get("", response_model=list[ConversationOut])
async def list_convs(cur_user=Depends(get_current_user), db=Depends(get_db)):
    r = await db.execute(
        select(Conversation).where(Conversation.user_id==cur_user.id)
        .order_by(desc(Conversation.updated_at))
    )
    convs = r.scalars().all()
    out = []
    for c in convs:
        lm = (await db.execute(
            select(Message).where(Message.conversation_id==c.id)
            .order_by(desc(Message.created_at)).limit(1)
        )).scalar_one_or_none()
        cnt = (await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id==c.id)
        )).scalar() or 0
        out.append(ConversationOut(
            id=c.id, title=c.title, model_id=c.model_id,
            created_at=c.created_at, updated_at=c.updated_at,
            last_message=(lm.content[:100] if lm and lm.content else None),
            message_count=cnt,
        ))
    return out

@router.patch("/{cid}/title")
async def rename_conv(cid: str, data: ConversationTitle,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    conv = (await db.execute(
        select(Conversation).where(Conversation.id==cid, Conversation.user_id==cur_user.id)
    )).scalar_one_or_none()
    if not conv: raise HTTPException(404, "Not found")
    conv.title = data.title
    await db.commit()
    return {"ok": True}

@router.delete("/{cid}")
async def delete_conv(cid: strdec,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    conv = (await db.execute(
        select(Conversation).where(Conversation.id==cid, Conversation.user_id==cur_user.id)
    )).scalar_one_or_none()
    if not conv: raise HTTPException(404, "Not found")
    await db.delete(conv)
    await db.commit()
    return {"ok": True}

@router.get("/{cid}/messages")
async def get_msgs(cid: str,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    conv = (await db.execute(
        select(Conversation).where(Conversation.id==cid, Conversation.user_id==cur_user.id)
    )).scalar_one_or_none()
    if not conv: raise HTTPException(404, "Not found")
    msgs = (await db.execute(
        select(Message).where(Message.conversation_id==cid).order_by(Message.created_at)
    )).scalars().all()
    return [{
        "id": m.id, "role혀 m.role, "content": m.content,
        "image_urls": json.loads(m.image_urls) if m.image_urls else None,
        "model_id": m.model_id, "created_at": str(m.created_at),
    } for m in msgs]
""")

print("Done generating files.")
