"""Monitor validation and transactional Monitor/ScheduleRegistration use cases.

``MonitorService`` owns the transaction boundary: create and update each run as
one commit across the monitor row and its schedule registration, so a monitor
can never persist as active with its registration missing (design.md, "Failure
strategy").
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import Monitor, MonitorStatus
from mlsc.repositories.monitors import MonitorRepository, ScheduleRegistrationRepository
from mlsc.schemas.monitors import (
    MonitorCreateRequest,
    MonitorResponse,
    MonitorUpdateRequest,
    validate_seed,
)


class UuidSource(Protocol):
    def __call__(self) -> uuid.UUID: ...


class MonitorService:
    """Injected with a session factory and a UUID source; constructs no engine itself."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        uuid_source: UuidSource = uuid.uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._uuid_source = uuid_source

    async def create(self, request: MonitorCreateRequest) -> MonitorResponse:
        async with self._session_factory() as session:
            monitor = Monitor(
                id=self._uuid_source(),
                name=request.name,
                target_type=request.target_type,
                seed=request.seed,
                schedule=request.cron_expression,
                timezone=request.timezone,
                status=MonitorStatus.ACTIVE,
                retention_days=request.retention_days,
            )
            monitors = MonitorRepository(session)
            registrations = ScheduleRegistrationRepository(session)
            monitors.insert(monitor)
            await session.flush()
            await registrations.upsert_active(
                monitor.id, request.cron_expression, request.timezone
            )
            await session.commit()
            return MonitorResponse.from_monitor(await monitors.get(monitor.id))

    async def update(
        self, monitor_id: uuid.UUID, request: MonitorUpdateRequest
    ) -> MonitorResponse:
        async with self._session_factory() as session:
            monitors = MonitorRepository(session)
            registrations = ScheduleRegistrationRepository(session)

            monitor = await monitors.get(monitor_id)

            if request.name is not None:
                monitor.name = request.name
            if request.seed is not None:
                validate_seed(monitor.target_type, request.seed)
                monitor.seed = request.seed
            if request.cron_expression is not None:
                monitor.schedule = request.cron_expression
            if request.timezone is not None:
                monitor.timezone = request.timezone
            if request.retention_days is not None:
                monitor.retention_days = request.retention_days
            if request.status is not None:
                monitor.status = request.status

            if monitor.status is MonitorStatus.ACTIVE:
                await registrations.upsert_active(monitor.id, monitor.schedule, monitor.timezone)
            else:
                await registrations.delete_all_for(monitor.id)

            await session.commit()
            return MonitorResponse.from_monitor(await monitors.get(monitor.id))

    async def get(self, monitor_id: uuid.UUID) -> MonitorResponse:
        async with self._session_factory() as session:
            monitor = await MonitorRepository(session).get(monitor_id)
            return MonitorResponse.from_monitor(monitor)

    async def list_all(self) -> list[MonitorResponse]:
        async with self._session_factory() as session:
            monitors = await MonitorRepository(session).list_all()
            return [MonitorResponse.from_monitor(monitor) for monitor in monitors]
