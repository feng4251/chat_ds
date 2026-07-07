from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import User
from schemas import UserCreate, UserLogin, UserOut, TokenOut
from auth import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut)
async def register(data: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(
        select(User).where(User.username == data.username)
    )).scalar_one_or_none()

    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        username=data.username,
        email=data.email,
        hashed_password=hash_password(data.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token({"sub": user.id})
    return TokenOut(access_token=token, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenOut)
async def login(data: UserLogin, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(
        select(User).where(User.username == data.username)
    )).scalar_one_or_none()

    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.id})
    return TokenOut(access_token=token, user=UserOut.model_validate(user))
