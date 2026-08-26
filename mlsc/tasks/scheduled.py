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
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import Monitor, MonitorStatus
from mlsc.worker import app

logger = logging.getLogger(__name__)


async def _for_each_active_monitor(
    session_factory: async_sessionmaker[AsyncSession],
    work: Callable[[uuid.UUID], Awaitable[None]],
) -> None:
    """Requirement 8/9: loop every active, non-paused, non-archived monitor,
    calling ``work`` for each. One monitor's ``work`` raising is logged with
    its monitor id and the loop continues, matching how `rollup_daily` and
    `evaluate_alerts` already treat one bad unit inside a batch
    (design.md, "Failure strategy").

    Only this function's own session loads the monitor list; ``work`` opens
    and closes its own session per monitor, matching every function it will
    call (design.md, "Dependencies, injected")."""
    async with session_factory() as session:
        result = await session.execute(
            select(Monitor.id).where(Monitor.status == MonitorStatus.ACTIVE)
        )
        monitor_ids = list(result.scalars().all())

    for monitor_id in monitor_ids:
        try:
            await work(monitor_id)
        except Exception:  # noqa: BLE001 - one bad monitor must not abort the batch
            logger.exception("cadence work failed for monitor %s", monitor_id)
            continue


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

    import httpx

    from mlsc.bootstrap import build_redis_client
    from mlsc.config import load_settings
    from mlsc.core.fetch.assembly import build_fetch_client
    from mlsc.core.locks import RunLock
    from mlsc.application.runs import RunService
    from mlsc.db.models import IngestionRun
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.sources.news.extract import TrafilaturaExtractor
    from mlsc.sources.news.resolve import HttpxRedirectResolver
    from mlsc.tasks.celery_dispatcher import CeleryDispatcher
    from mlsc.tasks.dispatch import dispatch_run as run_dispatch_pipeline

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    redis = build_redis_client(settings.redis)
    lock = RunLock(redis)
    run_service = RunService(session_factory, lock, CeleryDispatcher())
    fetch_client = build_fetch_client(redis)
    # One httpx client for both: each does a plain GET against a publisher
    # host, so a second connection pool would buy nothing.
    news_http = httpx.AsyncClient()
    resolver = HttpxRedirectResolver(news_http)
    extractor = TrafilaturaExtractor(news_http)
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
            resolver=resolver,
            extractor=extractor,
        )

    asyncio.run(_run())


@app.task(name="mlsc.run_override")
def run_override(job_id: str) -> None:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `dispatch_run` above."""
    import uuid

    import httpx

    from mlsc.bootstrap import build_redis_client
    from mlsc.config import ConfigurationError, load_settings
    from mlsc.core.fetch.assembly import build_fetch_client
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.llm.router import LlmRouter
    from mlsc.pipeline.enrich import Embedder, SentimentScorer
    from mlsc.sources.news.extract import TrafilaturaExtractor
    from mlsc.sources.news.resolve import HttpxRedirectResolver
    from mlsc.tasks.overrides import run_override as run_override_job

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    redis = build_redis_client(settings.redis)
    fetch_client = build_fetch_client(redis)
    news_http = httpx.AsyncClient()

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
            resolver=HttpxRedirectResolver(news_http),
            extractor=TrafilaturaExtractor(news_http),
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


@app.task(name="mlsc.run_weekly_discovery")
def run_weekly_discovery() -> None:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `run_alert_delivery` above.

    No monitor argument: fires once weekly across every active monitor's
    residue pool, independent of any one monitor's own collection schedule
    (design.md, "Success path")."""
    from mlsc.config import ConfigurationError, load_settings, load_topic_thresholds
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.llm.router import LlmRouter
    from mlsc.pipeline.topics.discovery import UmapReducer
    from mlsc.tasks.topics import discover_topics, mark_dormant_topics

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    thresholds = load_topic_thresholds()
    reducer = UmapReducer()

    # Discovery's label generation tolerates a None router by falling back to
    # keyword-only labels (mlsc.tasks.topics.discover_topics), so an
    # unconfigured tier degrades this cadence rather than crashing it,
    # matching `run_override`'s existing tolerance for the same condition.
    try:
        llm_router: LlmRouter | None = LlmRouter.from_configuration()
    except ConfigurationError:
        llm_router = None

    async def work(monitor_id: uuid.UUID) -> None:
        await discover_topics(
            session_factory,
            monitor_id=monitor_id,
            thresholds=thresholds,
            llm_router=llm_router,
            reducer=reducer,
        )
        await mark_dormant_topics(
            session_factory,
            monitor_id=monitor_id,
            thresholds=thresholds,
        )

    asyncio.run(_for_each_active_monitor(session_factory, work))


@app.task(name="mlsc.run_monthly_refit")
def run_monthly_refit() -> None:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `run_weekly_discovery`
    above.

    No monitor argument: fires once monthly across every active monitor's
    topic registry, independent of any one monitor's own collection schedule
    (design.md, "Success path")."""
    from mlsc.config import load_settings, load_topic_thresholds
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.pipeline.topics.discovery import HdbscanClusterer
    from mlsc.pipeline.topics.refit import refit_registry

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)
    thresholds = load_topic_thresholds()
    clusterer = HdbscanClusterer(min_cluster_size=thresholds.min_cluster_size)

    async def work(monitor_id: uuid.UUID) -> None:
        await refit_registry(
            session_factory,
            monitor_id=monitor_id,
            thresholds=thresholds,
            clusterer=clusterer,
        )

    asyncio.run(_for_each_active_monitor(session_factory, work))


@app.task(name="mlsc.run_retention_sweep")
def run_retention_sweep() -> None:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `run_monthly_refit`
    above.

    No monitor argument: fires daily across every active monitor's
    retention window, independent of any one monitor's own collection
    schedule (design.md, "Success path")."""
    from mlsc.config import load_settings
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.tasks.retention import enforce_retention

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)

    async def work(monitor_id: uuid.UUID) -> None:
        await enforce_retention(session_factory, monitor_id)

    asyncio.run(_for_each_active_monitor(session_factory, work))


@app.task(name="mlsc.run_stability_snapshot")
def run_stability_snapshot() -> None:
    """Celery entrypoint. Builds its own dependencies rather than importing a
    process-wide singleton, matching the pattern in `run_retention_sweep`
    above.

    No monitor argument: fires weekly across every active monitor's current
    document-to-topic assignments, independent of any one monitor's own
    collection schedule (design.md, "Success path")."""
    from mlsc.config import load_settings
    from mlsc.db.session import build_engine, build_session_factory
    from mlsc.tasks.maintenance import take_snapshot

    settings = load_settings()
    engine = build_engine(settings.postgres)
    session_factory = build_session_factory(engine)

    async def work(monitor_id: uuid.UUID) -> None:
        await take_snapshot(session_factory, monitor_id=monitor_id)

    asyncio.run(_for_each_active_monitor(session_factory, work))
