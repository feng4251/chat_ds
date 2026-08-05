import asyncio
import logging
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from config import settings
from database import init_db

from routers.auth_router import router as auth_router
from routers.chat_router import (
    router as chat_router,
    shutdown_chat_background_tasks,
)
from routers.conv_router import router as conv_router
from routers.model_router import router as model_router
from routers.skill_router import router as skill_router
from routers.mcp_router import router as mcp_router
from routers.workspace_router import router as workspace_router
from routers.hook_router import router as hook_router
from routers.schedule_router import router as schedule_router, internal_router
from routers.internal_session_router import router as internal_session_router
from scheduler import (
    scheduler_loop,
    shutdown_scheduled_job_executions,
)
from stream_observability import set_service_shutdown_started
from workspace_lock import WorkspaceMutationLockError
from workspace_reconciler import (
    periodic_workspace_reconciler,
    reconcile_orphan_session_workspaces,
)
from storage_attestation import (
    storage_attestations_match,
    storage_root_attestation,
)
from agent_engines.lifecycle import revoke_stale_native_runs_on_backend_startup


logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    set_service_shutdown_started(False)
    await init_db()
    revoked_native_runs = await revoke_stale_native_runs_on_backend_startup()
    if revoked_native_runs:
        logger.warning(
            "Revoked %s stale native Agent Engine run(s) during startup",
            revoked_native_runs,
        )
    try:
        workspace_reconcile = await reconcile_orphan_session_workspaces(
            clear_live_tombstones=True,
        )
    except Exception as exc:
        logger.error(
            "Startup workspace reconcile failed safely: error_type=%s",
            type(exc).__name__,
        )
        raise RuntimeError(
            "Startup workspace reconciliation failed safely."
        ) from None
    logger.info("Startup workspace reconcile: %s", workspace_reconcile)
    workspace_reconcile_stop = asyncio.Event()
    workspace_reconcile_task = asyncio.create_task(
        periodic_workspace_reconciler(workspace_reconcile_stop)
    )
    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        set_service_shutdown_started(True)
        # Stop the producer loop before taking any execution snapshot. This
        # prevents a final scheduler tick from creating a flight after the
        # shutdown drain has begun.
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Scheduler loop failed during shutdown")
        scheduler_shutdown = await shutdown_scheduled_job_executions()
        if not scheduler_shutdown["success"]:
            logger.error(
                "Scheduled execution shutdown left %s residual flight(s)",
                scheduler_shutdown["residual_count"],
            )
        await shutdown_chat_background_tasks()
        workspace_reconcile_stop.set()
        try:
            await asyncio.wait_for(
                workspace_reconcile_task,
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            workspace_reconcile_task.cancel()
            try:
                await workspace_reconcile_task
            except asyncio.CancelledError:
                pass

app = FastAPI(title=settings.app_title, lifespan=lifespan)


@app.exception_handler(WorkspaceMutationLockError)
async def workspace_mutation_lock_error_handler(
    _request: Request,
    exc: WorkspaceMutationLockError,
) -> JSONResponse:
    """Expose bounded contention without leaking filesystem details."""

    headers = (
        {"Retry-After": "1"}
        if exc.code in {
            "workspace_lock_timeout",
            "workspace_session_pending",
        }
        else None
    )
    return JSONResponse(
        status_code=exc.http_status_code,
        content={
            "detail": exc.public_message,
            "code": exc.code,
        },
        headers=headers,
    )


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
    local_storage = storage_root_attestation("/app/data")
    if local_storage["available"] is not True:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "title": settings.app_title,
                "code": "storage_root_unavailable",
                "storage": local_storage,
            },
        )
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{settings.harness_url}/health")
        response.raise_for_status()
        harness_health = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "title": settings.app_title,
                "code": "harness_health_unavailable",
                "storage": local_storage,
            },
        )
    remote_storage = (
        harness_health.get("storage")
        if isinstance(harness_health, dict)
        else None
    )
    if not storage_attestations_match(local_storage, remote_storage):
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "title": settings.app_title,
                "code": "shared_storage_identity_mismatch",
                "storage": local_storage,
            },
        )
    return {
        "status": "ok",
        "title": settings.app_title,
        "storage": local_storage,
    }
