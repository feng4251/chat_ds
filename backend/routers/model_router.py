from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import User, CustomModelConfig
from schemas import CustomModelConfigCreate, CustomModelConfigOut
from auth import get_current_user

router = APIRouter(prefix="/api/models/config", tags=["model_config"])

@router.get("", response_model=list[CustomModelConfigOut])
async def list_configs(cur_user=Depends(get_current_user), db=Depends(get_db)):
    r = await db.execute(
        select(CustomModelConfig).where(CustomModelConfig.user_id == cur_user.id)
    )
    return r.scalars().all()

@router.post("", response_model=CustomModelConfigOut)
async def create_config(data: CustomModelConfigCreate,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    from routers.chat_router import BUILTIN
    if data.model_id in BUILTIN:
        raise HTTPException(400, f"Model id {data.model_id} is reserved by a built-in model")
    existing = (await db.execute(
        select(CustomModelConfig).where(
            CustomModelConfig.user_id == cur_user.id,
            CustomModelConfig.model_id == data.model_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, f"Model {data.model_id} already configured")

    cm = CustomModelConfig(
        user_id=cur_user.id,
        model_id=data.model_id,
        model_name=data.model_name,
        provider=data.provider,
        base_url=data.base_url,
        api_key=data.api_key,
        is_multimodal=data.is_multimodal,
        extra_headers=data.extra_headers,
    )
    db.add(cm)
    await db.commit()
    await db.refresh(cm)
    return cm

@router.delete("/{cfg_id}")
async def delete_config(cfg_id: str,
    cur_user=Depends(get_current_user), db=Depends(get_db)):
    cfg = (await db.execute(
        select(CustomModelConfig).where(
            CustomModelConfig.id == cfg_id,
            CustomModelConfig.user_id == cur_user.id,
        )
    )).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "Not found")
    await db.delete(cfg)
    await db.commit()
    return {"ok": True}
