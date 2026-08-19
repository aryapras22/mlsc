"""FastAPI app factory. Routers only; no business logic lives here."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from mlsc.api.monitors import router as monitors_router
from mlsc.application.monitors import MonitorService
from mlsc.bootstrap import start_process


@asynccontextmanager
async def _lifespan(app: FastAPI):
    startup = await start_process()
    app.state.startup = startup
    app.state.monitor_service = MonitorService(startup.session_factory)
    try:
        yield
    finally:
        await startup.engine.dispose()
        await startup.redis.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="mlsc", lifespan=_lifespan)
    app.include_router(monitors_router)
    return app


app = create_app()
