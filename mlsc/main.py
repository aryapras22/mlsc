"""FastAPI app factory. Routers only; no business logic lives here."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from mlsc.api.monitors import router as monitors_router
from mlsc.api.runs import router as runs_router
from mlsc.api.v1.alerts import router as alerts_router
from mlsc.api.v1.documents import router as documents_router
from mlsc.api.v1.insights import router as insights_router
from mlsc.api.v1.metrics import router as metrics_router
from mlsc.application.monitors import MonitorService
from mlsc.application.runs import RunService
from mlsc.bootstrap import start_process
from mlsc.core.locks import RunLock
from mlsc.tasks.celery_dispatcher import CeleryDispatcher


@asynccontextmanager
async def _lifespan(app: FastAPI):
    startup = await start_process()
    app.state.startup = startup
    app.state.monitor_service = MonitorService(startup.session_factory)
    app.state.run_service = RunService(
        startup.session_factory, RunLock(startup.redis), CeleryDispatcher()
    )
    try:
        yield
    finally:
        await startup.engine.dispose()
        await startup.redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="mlsc", lifespan=_lifespan)
    app.include_router(monitors_router)
    app.include_router(runs_router)
    app.include_router(metrics_router)
    app.include_router(insights_router)
    app.include_router(documents_router)
    app.include_router(alerts_router)
    return app


app = create_app()
