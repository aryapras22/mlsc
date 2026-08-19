from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, MetaData, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative root whose metadata is the Alembic autogenerate target.

    Alembic owns every schema object; nothing may call ``Base.metadata.create_all``.
    """

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


class TargetType(str, Enum):
    """What a monitor watches. Closed: seed validation depends on knowing which."""

    PRODUCT = "product"
    THEME = "theme"


class MonitorStatus(str, Enum):
    """A monitor's lifecycle. There is no terminal success state (handoff §1.2)."""

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class Monitor(Base):
    """What the system watches. Every document, topic, and metric is owned by one."""

    __tablename__ = "monitors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str]
    target_type: Mapped[TargetType]
    seed: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[MonitorStatus] = mapped_column(default=MonitorStatus.ACTIVE)
    retention_days: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    registration: Mapped[ScheduleRegistration | None] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )


class ScheduleRegistration(Base):
    """The cron expression and timezone Beat projects for one monitor.

    At most one row per monitor: the unique constraint on ``monitor_id`` is what
    actually guarantees "at most one active registration" against a race, not
    application code (learn.md, "One transaction across a state machine").
    """

    __tablename__ = "schedule_registrations"
    __table_args__ = (UniqueConstraint("monitor_id", name="uq_schedule_registrations_monitor_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE")
    )
    cron_expression: Mapped[str]
    timezone: Mapped[str]

    monitor: Mapped[Monitor] = relationship(back_populates="registration")
