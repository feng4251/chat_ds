import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from config import settings
from database import init_db

from routers.auth_router import router as auth_router
from routers.chat_router import router as chat_router
from routers.conv_router import router as conv_router
from routers.model_router import router as model_router
from routers.skill_router import router as skill_router
from routers.mcp_router import router as mcp_router
from routers.workspace_router import router as workspace_router
from routers.hook_router import router as hook_router
from routers.schedule_router import router as schedule_router, internal_router
from routers.internal_session_router import router as internal_session_router
from scheduler import scheduler_loop

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass

app = FastAPI(title=settings.app_title, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.cors_origins else settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(conv_router)
app.include_router(model_router)
app.include_router(skill_router)
app.include_router(mcp_router)
app.include_router(workspace_router)
app.include_router(hook_router)
app.include_router(schedule_router)
app.include_router(internal_router)
app.include_router(internal_session_router)

@app.get("/api/health")
async def health():
    return {"status": "ok", "title": settings.app_title}
