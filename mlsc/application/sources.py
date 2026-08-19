"""Transactional use cases for attaching and managing a monitor's sources."""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import MonitorSource
from mlsc.repositories.sources import MonitorSourceRepository
from mlsc.schemas.sources import (
    MonitorSourceCreateRequest,
    MonitorSourceResponse,
    MonitorSourceUpdateRequest,
    instance_key_for,
)


class UuidSource(Protocol):
    def __call__(self) -> uuid.UUID: ...


class MonitorSourceService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        uuid_source: UuidSource = uuid.uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._uuid_source = uuid_source

    async def attach(
        self, monitor_id: uuid.UUID, request: MonitorSourceCreateRequest
    ) -> MonitorSourceResponse:
        instance_key = instance_key_for(request.source_name, request.config)
        async with self._session_factory() as session:
            source = MonitorSource(
                id=self._uuid_source(),
                monitor_id=monitor_id,
                source_name=request.source_name,
                instance_key=instance_key,
                config=request.config,
                daily_quota=request.daily_quota,
                enabled=request.enabled,
            )
            repository = MonitorSourceRepository(session)
            repository.insert(source)
            await session.commit()
            return MonitorSourceResponse.model_validate(await repository.get(source.id))

    async def update(
        self, source_id: uuid.UUID, request: MonitorSourceUpdateRequest
    ) -> MonitorSourceResponse:
        async with self._session_factory() as session:
            repository = MonitorSourceRepository(session)
            source = await repository.get(source_id)
            if request.daily_quota is not None:
                source.daily_quota = request.daily_quota
            if request.enabled is not None:
                source.enabled = request.enabled
            await session.commit()
            return MonitorSourceResponse.model_validate(await repository.get(source_id))

    async def list_for_monitor(self, monitor_id: uuid.UUID) -> list[MonitorSourceResponse]:
        async with self._session_factory() as session:
            sources = await MonitorSourceRepository(session).list_for_monitor(monitor_id)
            return [MonitorSourceResponse.model_validate(source) for source in sources]
