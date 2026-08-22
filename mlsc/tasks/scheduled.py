"""The Celery tasks a run's lifecycle enqueues: start it, then collect for it.

`run_monitor` wires the `monitor:{uuid}` entry from monitor-registry's
SchedulePlanner (mlsc/beat.py) to RunService.start, carrying only the monitor
id per that design's projection contract. `dispatch_run` is what
`CeleryDispatcher.dispatch_run` enqueues from inside `RunService.start`; it
loads the run's monitor id and hands both to `mlsc.tasks.dispatch.dispatch_run`
(on-demand-collection design.md, "Where the task lives and how it gets its
dependencies"). `run_override` is what `CeleryDispatcher.dispatch_override`
enqueues from inside `OverrideService.submit`, matching the same pattern
(monitor-repair-overrides design.md, "Dependencies, injected").
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


@app.task(name="mlsc.dispatch_run")
def dispatch_run(run_id: str) -> None:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `run_monitor` above."""
    import uuid

    from mlsc.bootstrap import build_redis_client
    from mlsc.config import load_settings
    from mlsc.core.fetch.assembly import build_fetch_client
    from mlsc.core.locks import RunLock
    from mlsc.application.runs import RunService
    from mlsc.db.models import IngestionRun
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.tasks.celery_dispatcher import CeleryDispatcher
    from mlsc.tasks.dispatch import dispatch_run as run_dispatch_pipeline

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    redis = build_redis_client(settings.redis)
    lock = RunLock(redis)
    run_service = RunService(session_factory, lock, CeleryDispatcher())
    fetch_client = build_fetch_client(redis)
    run_uuid = uuid.UUID(run_id)

    async def _run() -> None:
        async with session_factory() as session:
            run = await session.get(IngestionRun, run_uuid)
            assert run is not None
            monitor_id = run.monitor_id

        await run_dispatch_pipeline(
            session_factory=session_factory,
            run_service=run_service,
            fetch_client=fetch_client,
            run_id=run_uuid,
            monitor_id=monitor_id,
        )

    asyncio.run(_run())


@app.task(name="mlsc.run_override")
def run_override(job_id: str) -> None:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `dispatch_run` above."""
    import uuid

    from mlsc.bootstrap import build_redis_client
    from mlsc.config import ConfigurationError, load_settings
    from mlsc.core.fetch.assembly import build_fetch_client
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.llm.router import LlmRouter
    from mlsc.pipeline.enrich import Embedder, SentimentScorer
    from mlsc.tasks.overrides import run_override as run_override_job

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    redis = build_redis_client(settings.redis)
    fetch_client = build_fetch_client(redis)

    # Only a stage_rerun of the intent stage needs this, and the kind isn't
    # known until the job loads — unlike the daily pipeline, an unconfigured
    # tier here must not fail every override, only the ones that use it
    # (mlsc.pipeline.enrich already treats a None router as "skip intent").
    try:
        llm_router: LlmRouter | None = LlmRouter.from_configuration()
    except ConfigurationError:
        llm_router = None

    asyncio.run(
        run_override_job(
            session_factory,
            job_id=uuid.UUID(job_id),
            fetch_client=fetch_client,
            embedder=Embedder(),
            sentiment_scorer=SentimentScorer(),
            llm_router=llm_router,
        )
    )


@app.task(name="mlsc.run_alert_delivery")
def run_alert_delivery() -> int:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `run_override` above.

    No monitor argument: a recurring sweep across every pending or
    previously failed delivery, independent of any one monitor's own
    collection schedule (design.md, "Success path": "already fans across
    every rule itself")."""
    from mlsc.bootstrap import build_redis_client
    from mlsc.config import load_settings
    from mlsc.core.fetch.assembly import build_webhook_sender
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.tasks.alerts import deliver_pending

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    redis = build_redis_client(settings.redis)
    sender = build_webhook_sender(redis)

    return asyncio.run(deliver_pending(session_factory, sender=sender))
