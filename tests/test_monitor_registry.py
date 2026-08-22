"""Tests for the monitor registry.

Run against the local Compose PostgreSQL (docker-compose.local.yml). Async
work runs through ``asyncio.run`` inside ordinary sync test functions, matching
``test_bootstrap.py``: no async test plugin is pinned in ``environment.yml``.

Requirements: 1, 3, 4, 5, 6, 7.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from typing import TypeVar

import pytest
from sqlalchemy import pool, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.beat import STATIC_SCHEDULE, MonitorAwareScheduler, SchedulePlanner
from mlsc.db.models import Base, MonitorStatus, ScheduleRegistration, TargetType
from mlsc.repositories.monitors import MonitorNotFound
from mlsc.schemas.monitors import MonitorCreateRequest, MonitorUpdateRequest
from mlsc.worker import app

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc"

T = TypeVar("T")


def run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


def _product_request(**overrides: object) -> MonitorCreateRequest:
    base: dict[str, object] = dict(
        name="Roblox",
        target_type=TargetType.PRODUCT,
        seed={"identifiers": ["com.roblox.client"]},
        cron_expression="0 3 * * *",
        timezone="Asia/Jakarta",
        retention_days=90,
    )
    base.update(overrides)
    return MonitorCreateRequest(**base)


async def _reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (OperationalError, OSError):
        return False


async def _reset_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def session_factory() -> async_sessionmaker:
    # NullPool: each test helper calls asyncio.run separately, and a pooled
    # asyncpg connection created under one run's event loop cannot be reused
    # under the next (same constraint as mlsc/beat.py and migrations/env.py).
    engine = create_async_engine(LOCAL_DATABASE_URL, poolclass=pool.NullPool)
    if not run(_reachable(engine)):
        run(engine.dispose())
        pytest.skip("local Compose PostgreSQL is not reachable at localhost:55433")
    run(_reset_schema(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    run(engine.dispose())


@pytest.fixture
def service(session_factory: async_sessionmaker) -> MonitorService:
    return MonitorService(session_factory)


@pytest.fixture
def planner(session_factory: async_sessionmaker) -> SchedulePlanner:
    return SchedulePlanner(session_factory)


class TestRegistrationStateMachine:
    def test_create_persists_active_with_a_projected_registration(
        self, service: MonitorService
    ) -> None:
        created = run(service.create(_product_request()))

        assert created.status is MonitorStatus.ACTIVE
        assert created.projected is True
        assert created.cron_expression == "0 3 * * *"

    def test_pause_drops_projection_but_keeps_the_schedule(
        self, service: MonitorService
    ) -> None:
        created = run(service.create(_product_request()))

        paused = run(service.update(created.id, MonitorUpdateRequest(status=MonitorStatus.PAUSED)))

        assert paused.status is MonitorStatus.PAUSED
        assert paused.projected is False
        assert paused.cron_expression == "0 3 * * *"

    def test_resume_re_projects_the_retained_schedule(self, service: MonitorService) -> None:
        created = run(service.create(_product_request()))
        run(service.update(created.id, MonitorUpdateRequest(status=MonitorStatus.PAUSED)))

        resumed = run(
            service.update(created.id, MonitorUpdateRequest(status=MonitorStatus.ACTIVE))
        )

        assert resumed.status is MonitorStatus.ACTIVE
        assert resumed.projected is True
        assert resumed.cron_expression == "0 3 * * *"

    def test_archive_drops_projection_permanently(self, service: MonitorService) -> None:
        created = run(service.create(_product_request()))

        archived = run(
            service.update(created.id, MonitorUpdateRequest(status=MonitorStatus.ARCHIVED))
        )

        assert archived.status is MonitorStatus.ARCHIVED
        assert archived.projected is False


class TestTransactionalIntegrity:
    def test_a_failed_create_leaves_no_partial_monitor_row(
        self, service: MonitorService, session_factory: async_sessionmaker
    ) -> None:
        """A monitor row and its registration are one atomic fact.

        Reusing the same id for a second create collides on the primary key
        during the second write inside the same transaction, after the first
        write (the monitor row) already flushed but before either commits.
        """
        fixed_id = uuid.uuid4()
        broken_service = MonitorService(session_factory, uuid_source=lambda: fixed_id)
        run(broken_service.create(_product_request(name="First")))

        with pytest.raises(Exception):  # noqa: B017 - any DB integrity error is acceptable
            run(broken_service.create(_product_request(name="Second")))

        listed = run(service.list_all())
        assert [m.name for m in listed] == ["First"]

    def test_update_of_unknown_monitor_raises_monitor_not_found(
        self, service: MonitorService
    ) -> None:
        with pytest.raises(MonitorNotFound):
            run(service.update(uuid.uuid4(), MonitorUpdateRequest(name="x")))


class TestBoundaryRejection:
    def test_blank_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="name"):
            _product_request(name="   ")

    def test_six_field_cron_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cron_expression"):
            _product_request(cron_expression="* * * * * *")

    def test_unparseable_cron_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cron_expression"):
            _product_request(cron_expression="not a cron")

    def test_unknown_timezone_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            _product_request(timezone="Nowhere/Fake")

    def test_seed_not_matching_target_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="seed"):
            _product_request(seed={"description": "wrong shape for a product"})

    def test_theme_seed_requires_a_description(self) -> None:
        with pytest.raises(ValueError, match="seed"):
            _product_request(target_type=TargetType.THEME, seed={"identifiers": ["x"]})


class TestProjectionDeterminism:
    def test_two_ticks_with_one_unparseable_row_stay_consistent(
        self,
        service: MonitorService,
        planner: SchedulePlanner,
        session_factory: async_sessionmaker,
    ) -> None:
        healthy = run(service.create(_product_request(name="Healthy")))
        broken = run(service.create(_product_request(name="Broken", timezone="UTC")))

        first_tick = run(planner.project())
        assert set(first_tick) == {f"monitor:{healthy.id}", f"monitor:{broken.id}"}

        async def corrupt_broken_registration() -> None:
            async with session_factory() as session:
                await session.execute(
                    update(ScheduleRegistration)
                    .where(ScheduleRegistration.monitor_id == broken.id)
                    .values(cron_expression="not a cron")
                )
                await session.commit()

        run(corrupt_broken_registration())

        second_tick = run(planner.project())

        assert set(second_tick) == {f"monitor:{healthy.id}"}
        assert str(first_tick[f"monitor:{healthy.id}"]["schedule"]) == str(
            second_tick[f"monitor:{healthy.id}"]["schedule"]
        )

    def test_paused_monitor_is_not_projected(
        self, service: MonitorService, planner: SchedulePlanner
    ) -> None:
        created = run(service.create(_product_request()))
        run(service.update(created.id, MonitorUpdateRequest(status=MonitorStatus.PAUSED)))

        entries = run(planner.project())

        assert entries == {}


class TestStaticScheduleSurvivesRepeatedMerge:
    def test_static_entries_survive_two_consecutive_syncs(
        self, planner: SchedulePlanner
    ) -> None:
        """merge_inplace (celery.beat.Scheduler) pops any schedule key absent
        from what it's given, so the five static cadence entries must be
        re-included on every tick, not just the first, or they vanish on the
        second (design.md, "Success path": the five fixed cadences)."""
        scheduler = MonitorAwareScheduler(app, planner=planner, lazy=True)
        try:
            scheduler._sync_from_planner()
            scheduler._sync_from_planner()

            assert set(STATIC_SCHEDULE) <= set(scheduler.schedule)
        finally:
            scheduler.close()
