"""Persistence for label sets, labels, snapshots, and reports.

Takes a caller-owned ``AsyncSession`` and never commits, matching every
other repository in this codebase.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import (
    DocumentLabel,
    EventLabel,
    LabelSet,
    Purpose,
    Report,
    Snapshot,
)


class LabelSetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, label_set: LabelSet) -> None:
        self._session.add(label_set)

    async def get(self, label_set_id: uuid.UUID) -> LabelSet | None:
        return await self._session.get(LabelSet, label_set_id)

    async def latest_for(self, monitor_id: uuid.UUID, *, purpose: Purpose) -> LabelSet | None:
        result = await self._session.execute(
            select(LabelSet).where(LabelSet.monitor_id == monitor_id, LabelSet.purpose == purpose)
            .order_by(LabelSet.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()


class DocumentLabelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, label: DocumentLabel) -> None:
        self._session.add(label)

    async def for_label_set(self, label_set_id: uuid.UUID) -> list[DocumentLabel]:
        result = await self._session.execute(
            select(DocumentLabel).where(DocumentLabel.label_set_id == label_set_id)
        )
        return list(result.scalars().all())


class EventLabelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, label: EventLabel) -> None:
        self._session.add(label)

    async def for_label_set(self, label_set_id: uuid.UUID) -> list[EventLabel]:
        result = await self._session.execute(
            select(EventLabel).where(EventLabel.label_set_id == label_set_id)
        )
        return list(result.scalars().all())


class SnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def freeze(self, monitor_id: uuid.UUID, taken_on: date, assignments: dict[str, str]) -> Snapshot:
        """Requirement 3/C12: re-running the same monitor and day converges
        on the same row rather than duplicating it — the snapshot job is
        idempotent the same way every other daily task in this codebase is.
        """
        values = {
            "id": uuid.uuid4(), "monitor_id": monitor_id, "taken_on": taken_on, "assignments": assignments,
        }
        statement = insert(Snapshot).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["monitor_id", "taken_on"], set_={"assignments": statement.excluded.assignments},
        ).returning(Snapshot)
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def adjacent_pairs(self, monitor_id: uuid.UUID, *, limit: int) -> list[tuple[Snapshot, Snapshot]]:
        """The most recent ``limit`` consecutive snapshot pairs, newest
        first — what stability is measured between (design.md, "Success
        path": "for each adjacent snapshot pair")."""
        result = await self._session.execute(
            select(Snapshot).where(Snapshot.monitor_id == monitor_id).order_by(Snapshot.taken_on.desc())
            .limit(limit + 1)
        )
        snapshots = list(result.scalars().all())
        return list(zip(snapshots, snapshots[1:], strict=False))


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, report: Report) -> None:
        self._session.add(report)
