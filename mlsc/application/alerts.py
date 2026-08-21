"""Rule management: create and list alert rules for a monitor.

Rejects an equivalent rule at creation (requirement 8's `RuleConflict`) —
same kind, channel, target and conditions — rather than accumulating
duplicate deliveries for the same match.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import AlertRule
from mlsc.repositories.alert_rules import AlertRuleRepository
from mlsc.schemas.alerts import AlertRuleCreateRequest


class RuleConflict(RuntimeError):
    """An equivalent rule already exists for this monitor."""


class AlertRuleService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, monitor_id: uuid.UUID, request: AlertRuleCreateRequest) -> AlertRule:
        async with self._session_factory() as session:
            repo = AlertRuleRepository(session)
            existing = await repo.find_equivalent(
                monitor_id, kind=request.kind, channel=request.channel,
                target=request.target, conditions=request.conditions,
            )
            if existing is not None:
                raise RuleConflict(str(existing.id))

            rule = AlertRule(
                id=uuid.uuid4(), monitor_id=monitor_id, kind=request.kind,
                conditions=request.conditions, channel=request.channel,
                target=request.target, enabled=request.enabled,
            )
            repo.insert(rule)
            await session.commit()
            return rule

    async def list_for_monitor(self, monitor_id: uuid.UUID) -> list[AlertRule]:
        async with self._session_factory() as session:
            result = await session.execute(select(AlertRule).where(AlertRule.monitor_id == monitor_id))
            return list(result.scalars().all())
