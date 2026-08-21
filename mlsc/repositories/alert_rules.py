"""Persistence for alert rules and their deliveries.

Takes a caller-owned ``AsyncSession`` and never commits, matching every
other repository in this codebase.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import AlertRule, Channel, Delivery, DeliveryState, ReadAlertKind


class AlertRuleNotFound(KeyError):
    def __init__(self, rule_id: uuid.UUID) -> None:
        super().__init__(str(rule_id))
        self.rule_id = rule_id


class AlertRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, rule: AlertRule) -> None:
        self._session.add(rule)

    async def get(self, rule_id: uuid.UUID) -> AlertRule:
        rule = await self._session.get(AlertRule, rule_id)
        if rule is None:
            raise AlertRuleNotFound(rule_id)
        return rule

    async def enabled_for(self, monitor_id: uuid.UUID, *, kind: ReadAlertKind) -> list[AlertRule]:
        """Requirement 8: only rules of ``kind`` are ever returned, so a
        product event cannot be matched against a scraper rule by a caller
        forgetting to filter."""
        result = await self._session.execute(
            select(AlertRule).where(
                AlertRule.monitor_id == monitor_id, AlertRule.kind == kind,
                AlertRule.enabled.is_(True),
            )
        )
        return list(result.scalars().all())

    async def find_equivalent(
        self, monitor_id: uuid.UUID, *, kind: ReadAlertKind, channel: Channel, target: str, conditions: dict
    ) -> AlertRule | None:
        result = await self._session.execute(
            select(AlertRule).where(
                AlertRule.monitor_id == monitor_id, AlertRule.kind == kind,
                AlertRule.channel == channel, AlertRule.target == target,
                AlertRule.conditions == conditions,
            )
        )
        return result.scalar_one_or_none()


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def create(self, *, rule_id: uuid.UUID, event_id: uuid.UUID) -> Delivery:
        delivery = Delivery(id=uuid.uuid4(), rule_id=rule_id, event_id=event_id)
        self._session.add(delivery)
        return delivery

    async def pending(self) -> list[Delivery]:
        """Both ``pending`` and ``failed`` deliveries are due for another
        attempt (requirement 9) — only ``delivered`` and ``abandoned`` are
        terminal."""
        result = await self._session.execute(
            select(Delivery).where(Delivery.state.in_((DeliveryState.PENDING, DeliveryState.FAILED)))
        )
        return list(result.scalars().all())

    def mark(self, delivery: Delivery, *, state: DeliveryState, error: str | None) -> None:
        delivery.attempts += 1
        delivery.state = state
        delivery.last_error = error
