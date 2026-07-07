import asyncio
import json
from typing import Optional
import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from config import settings
from database import get_db, async_session
from models import User, Conversation, Message, CustomModelConfig
from schemas import ChatRequest
from skills import SKILLS, run_skill_stream, SkillResult
from auth import get_current_user

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Background tasks (keep references so they don't get GC'd before completion).
_background_tasks: set[asyncio.Task] = set()


async def _persist_after_stream(
    conv_id: str,
    model_id: str,
    content: str,
    reasoning: str,
    skill_chain: Optional[str],
    first_user_content: str,
):
    """Save the assistant message (with reasoning + skill chain) + maybe
    generate title. Runs in its own db session so it survives the original
    request being cancelled (e.g. user refreshes mid-stream)."""
    async with async_session() as s:
        try:
            assistant_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=content,
                reasoning=reasoning or None,
                skill_chain=skill_chain or None,
                model_id=model_id,
            )
            s.add(assistant_msg)
            await s.commit()
        except Exception:
            return
        try:
            cnt = (await s.execute(
                select(func.count(Message.id)).where(Message.conversation_id == conv_id)
            )).scalar() or 0
            if cnt <= 2 and first_user_content:
                await _generate_title(conv_id, first_user_content, content, s)
        except Exception:
            pass


def _spawn_persist(
    conv_id: str,
    model_id: str,
    content: str,
    reasoning: str,
    skill_chain: Optional[str],
    first_user_content: str,
):
    t = asyncio.create_task(_persist_after_stream(
        conv_id, model_id, content, reasoning, skill_chain, first_user_content
    ))
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)


@router.get("/skills")
async def get_skills():
    return {"skills": SKILLS}

BUILTIN = {
    # Historical id — keep working for existing convs.
    # 10.10.132.2 now hosts MiniMax-M2 (was DeepSeek-V4-Pro earlier).
    "AgentModel": {
        "api_model": "AgentModel",
        "base_url": settings.agent_model_base_url,
        "api_key": settings.agent_model_api_key,
        "is_multimodal": False,
        "max_tokens": 49152,
        "display_name": "MiniMax-M2",
    },
    "deepseek_v4_flash": {
        "api_model": "AgentModel",
        "base_url": settings.deepseek_flash_base_url,
        "api_key": settings.deepseek_flash_api_key,
        "is_multimodal": False,
        "max_tokens": 262144,
        "display_name": "DeepSeek-V4-Flash",
    },
    "qwen3_6": {
        "api_model": "qwen3_6",
        "base_url": settings.qwen3_base_url,
        "api_key": settings.qwen3_api_key,
        "is_multimodal": True,
        "max_tokens": 65536,
        "display_name": "Qwen3-6 (多模态)",
    },
}

DEFAULT_CUSTOM_MAX_TOKENS = 32768


async def resolve_model(model_id: str, cur_user: User, db: AsyncSession):
    """Return (base_url, api_key, is_multimodal, max_tokens, api_model)."""
    if model_id in BUILTIN:
        c = BUILTIN[model_id]
        return c["base_url"], c["api_key"], c["is_multimodal"], c["max_tokens"], c["api_model"]
    r = await db.execute(
        select(CustomModelConfig).where(
            CustomModelConfig.user_id == cur_user.id,
            CustomModelConfig.model_id == model_id,
        )
    )
    cm = r.scalar_one_or_none()
    if cm:
        # Custom models: the user-supplied model_id is what we send to the endpoint.
        return cm.base_url, cm.api_key, cm.is_multimodal, DEFAULT_CUSTOM_MAX_TOKENS, cm.model_id
    raise HTTPException(400, f"Unknown model: {model_id}")

@router.get("/models")
async def get_models(cur_user=Depends(get_current_user), db=Depends(get_db)):
    # Pull builtin models from harness
    models: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.harness_url}/v1/models")
            if r.status_code == 200:
                data = r.json()
                for m in data.get("data", []):
                    mid = m["id"]
                    cfg = BUILTIN.get(mid, {})
                    models.append({
                        "id": mid,
                        "name": cfg.get("display_name", mid),
                        "provider": "builtin",
                        "is_multimodal": cfg.get("is_multimodal", False),
                    })
    except Exception:
        pass  # fall back to BUILTIN
    if not models:
        models = [
            {"id": mid, "name": cfg["display_name"], "provider": "builtin",
             "is_multimodal": cfg["is_multimodal"]}
            for mid, cfg in BUILTIN.items()
        ]
    # Merge custom models
    r = await db.execute(
        select(CustomModelConfig).where(CustomModelConfig.user_id == cur_user.id)
    )
    for cm in r.scalars().all():
        models.append({
            "id": cm.model_id, "name": cm.model_name,
            "provider": cm.provider, "is_multimodal": cm.is_multimodal,
        })
    return {"models": models}

def _skill_to_tools(skill_id: Optional[str]) -> list[str]:
    """Map a skill to harness tool names.  Returns empty list for now —
    skills.py preprocessing handles web_search / research.  In the future
    this will drive harness-side tool calling instead."""
    if not skill_id or skill_id == "general":
        return []
    # Future: harness-native tool calling
    # if skill_id == "web_search": return ["web_search"]
    # if skill_id == "research":   return ["web_search", "web_extract"]
    return []


def _detect_model(req: ChatRequest):
    """Auto-detect model if not explicitly provided."""
    if req.model_id:
        return req.model_id
    if req.image_urls:
        return "qwen3_6"
    return "AgentModel"

async def _generate_title(
    conv_id: str,
    first_user: str,
    first_assistant: str,
    db: AsyncSession,
):
    """Summarize the first QA exchange into a short conversation title using
    qwen3_6 (thinking disabled). Must NOT echo the user's literal question."""
    excerpt = f"用户:{first_user[:300].strip()}\n助手:{(first_assistant or '').strip()[:500]}"
    prompt = (
        "下面是一段对话的开头。请用 4 到 10 个字概括这段对话的核心主题,作为会话标题。\n"
        "硬性要求:\n"
        "1. 不能直接复述或抄写用户的原话,要做主题归纳\n"
        "2. 不要加引号、不要加句号、不要加任何解释\n"
        "3. 用对话本身所用的语言\n"
        "4. 只输出标题本身,一行\n\n"
        f"{excerpt}"
    )
    title = ""
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            async with c.stream(
                "POST",
                f"{settings.qwen3_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.qwen3_api_key}"},
                json={
                    "model": "qwen3_6",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 80,
                    "temperature": 0.3,
                    "stream": True,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ) as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[6:]
                    if chunk == "[DONE]":
                        break
                    try:
                        delta = json.loads(chunk)["choices"][0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            title += piece
                    except Exception:
                        continue
    except Exception:
        return

    title = title.strip().strip('"').strip("'").splitlines()[0].strip() if title.strip() else ""
    if not title or len(title) > 100:
        return
    r = await db.execute(select(Conversation).where(Conversation.id == conv_id))
    conv = r.scalar_one_or_none()
    if conv:
        conv.title = title
        await db.commit()

async def _chat_stream(req: ChatRequest, cur_user: User, db: AsyncSession):
    model_id = _detect_model(req)
    base_url, api_key, is_mm, max_tokens, api_model = await resolve_model(model_id, cur_user, db)

    # Create or verify conversation
    conv_id = req.conversation_id
    if not conv_id:
        conv = Conversation(user_id=cur_user.id, model_id=model_id)
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        conv_id = conv.id
    else:
        conv = (await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id, Conversation.user_id == cur_user.id
            )
        )).scalar_one_or_none()
        if not conv:
            raise HTTPException(404, "Conversation not found")

    # Load history before saving the new user message
    history_msgs = (await db.execute(
        select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
    )).scalars().all()
    history = [{"role": m.role, "content": m.content or ""} for m in history_msgs]

    # Save user message synchronously — must persist even if stream cancelled
    user_msg = Message(
        conversation_id=conv_id,
        role="user",
        content=req.content,
        image_urls=json.dumps(req.image_urls) if req.image_urls else None,
        model_id=model_id,
    )
    db.add(user_msg)
    await db.commit()

    async def generate():
        full_content = ""
        full_reasoning = ""
        skill_chain_lines: list[str] = []
        sr: Optional[SkillResult] = None
        error_message: Optional[str] = None

        try:
            # Run the skill — stream its progress to the client as skill_delta
            async for evt in run_skill_stream(req.skill_id, req.content, req.image_urls):
                if evt.get("type") == "progress":
                    msg = evt["msg"]
                    skill_chain_lines.append(msg)
                    yield f"data: {json.dumps({'skill_delta': msg, 'conversation_id': conv_id})}\n\n"
                elif evt.get("type") == "result":
                    sr = evt["result"]

            if sr is None:
                sr = SkillResult(
                    augmented_content=req.content,
                    augmented_image_urls=req.image_urls,
                )

            # Build final message list for the LLM
            system_msgs = [{"role": "system", "content": "You are a helpful AI assistant."}]
            if sr.system_note:
                system_msgs.append({"role": "system", "content": sr.system_note})
            final = system_msgs + history
            aug_images = sr.augmented_image_urls
            if aug_images:
                user_content = [
                    {"type": "image_url", "image_url": {"url": u}} for u in aug_images
                ]
                user_content.append({"type": "text", "text": sr.augmented_content})
                final.append({"role": "user", "content": user_content})
            else:
                final.append({"role": "user", "content": sr.augmented_content})

            # Stream from harness — wraps vLLM with optional tool calling
            try:
                tools = _skill_to_tools(req.skill_id)
                async with httpx.AsyncClient(timeout=600) as client:
                    async with client.stream(
                        "POST",
                        f"{settings.harness_url}/v1/chat/completions",
                        headers={"Content-Type": "application/json"},
                        json={
                            "model": model_id,
                            "messages": final,
                            "max_tokens": max_tokens,
                            "temperature": 0.6,
                            "stream": True,
                            "tools": tools,
                        },
                    ) as response:
                        if response.status_code >= 400:
                            body = (await response.aread()).decode("utf-8", "ignore")[:300]
                            error_message = f"Harness 返回 HTTP {response.status_code}:{body}"
                        else:
                            async for line in response.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                chunk = line[6:]
                                if chunk == "[DONE]":
                                    break
                                try:
                                    data = json.loads(chunk)
                                    delta = data["choices"][0].get("delta", {})
                                    # Harness tool_progress → skill_delta for frontend
                                    tp = delta.get("tool_progress")
                                    if tp:
                                        skill_chain_lines.append(tp)
                                        yield f"data: {json.dumps({'skill_delta': tp, 'conversation_id': conv_id})}\n\n"
                                    reasoning_piece = delta.get("reasoning") or ""
                                    content_piece = delta.get("content") or ""
                                    if reasoning_piece:
                                        full_reasoning += reasoning_piece
                                        yield f"data: {json.dumps({'reasoning_delta': reasoning_piece, 'conversation_id': conv_id})}\n\n"
                                    if content_piece:
                                        full_content += content_piece
                                        yield f"data: {json.dumps({'delta': content_piece, 'conversation_id': conv_id})}\n\n"
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass
            except httpx.ConnectError:
                error_message = f"无法连接到 Harness 服务 {settings.harness_url}。请检查 harness 容器是否在运行。"
            except httpx.TimeoutException:
                error_message = "Harness 服务响应超时。"
            except Exception as e:
                error_message = f"调用 Harness 时出错:{type(e).__name__}: {e}"

            if error_message and not full_content:
                # Surface the error as the assistant's content so the user sees it
                full_content = f"⚠️ {error_message}"
                yield f"data: {json.dumps({'delta': full_content, 'conversation_id': conv_id})}\n\n"
        finally:
            skill_chain_str = "\n".join(skill_chain_lines) if skill_chain_lines else None
            # Only persist if we actually have something to save
            if full_content or full_reasoning or skill_chain_str:
                _spawn_persist(
                    conv_id, model_id, full_content, full_reasoning,
                    skill_chain_str, req.content,
                )

    return StreamingResponse(generate(), media_type="text/event-stream")

@router.post("/completions")
async def chat_completion(req: ChatRequest,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    return await _chat_stream(req, cur_user, db)
