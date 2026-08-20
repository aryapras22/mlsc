"""The Celery task Beat's projected entry invokes: trigger today's run for one monitor.

Wires the `monitor:{uuid}` entry from monitor-registry's SchedulePlanner
(mlsc/beat.py) to RunService.start, carrying only the monitor id per that
design's projection contract.
"""

from __future__ import annotations

import asyncio
from datetime import date

from mlsc.worker import app


@app.task(name="mlsc.run_monitor")
def run_monitor(monitor_id: str) -> str:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in mlsc/beat.py."""
    import uuid

    from mlsc.bootstrap import build_redis_client
    from mlsc.config import load_settings
    from mlsc.core.locks import RunLock
    from mlsc.application.runs import RunService
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.tasks.celery_dispatcher import CeleryDispatcher

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    redis = build_redis_client(settings.redis)
    lock = RunLock(redis)
    run_service = RunService(session_factory, lock, CeleryDispatcher())

    run_id = asyncio.run(run_service.start(uuid.UUID(monitor_id), date.today()))
    return str(run_id)
