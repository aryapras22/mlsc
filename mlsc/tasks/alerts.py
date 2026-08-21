"""Alert evaluation and delivery: a task, never a side effect of detection
(design.md, "Alternatives": "Alert delivery inline in the detection task").

Matches a rule's events by its own kind only (requirement 8), enforced by
the repository query rather than a filter someone could forget. A failed
send is retried and the delivery stays visible rather than lost
(requirement 9); one rule failing to evaluate does not silence the others
(design.md, "Failure strategy").
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.core.fetch.webhook import WebhookSender, WebhookUnreachable
from mlsc.db.models import AlertRule, Channel, DeliveryState, ReadAlertKind, TrendEvent
from mlsc.repositories.alert_rules import AlertRuleRepository, DeliveryRepository
from mlsc.repositories.trends import TrendEventRepository

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3


async def evaluate_alerts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    bucket: date,
) -> int:
    """Requirement 7/8: match this bucket's product events against every
    enabled product rule for the monitor, creating one pending delivery per
    match. Returns the count of deliveries created."""
    async with session_factory() as session:
        rules = await AlertRuleRepository(session).enabled_for(monitor_id, kind=ReadAlertKind.PRODUCT)
        if not rules:
            return 0

        events = await TrendEventRepository(session).events_for_bucket(monitor_id, bucket)

        delivery_repo = DeliveryRepository(session)
        created = 0
        for rule in rules:
            try:
                created += _match_and_create(delivery_repo, rule=rule, events=events)
            except Exception:  # noqa: BLE001 - one bad rule must not silence the others
                logger.exception("alert rule %s failed to evaluate", rule.id)
                continue

        await session.commit()
    return created


def _match_and_create(delivery_repo: DeliveryRepository, *, rule: AlertRule, events: list[TrendEvent]) -> int:
    kind_filter = rule.conditions.get("event_kind")
    matches = [event for event in events if kind_filter is None or event.kind.value == kind_filter]
    for event in matches:
        delivery_repo.create(rule_id=rule.id, event_id=event.id)
    return len(matches)


async def deliver_pending(
    session_factory: async_sessionmaker[AsyncSession], *, sender: WebhookSender
) -> int:
    """Requirement 9: send every pending delivery, retry on failure up to
    the attempt limit, then mark it abandoned rather than discard it."""
    delivered = 0
    async with session_factory() as session:
        delivery_repo = DeliveryRepository(session)
        pending = await delivery_repo.pending()

        for delivery in pending:
            rule = await session.get(AlertRule, delivery.rule_id)
            if rule is None or rule.channel is not Channel.WEBHOOK:
                delivery_repo.mark(delivery, state=DeliveryState.ABANDONED, error="rule missing or not a webhook")
                continue

            try:
                await sender.send(rule.target, {"rule_id": str(rule.id), "event_id": str(delivery.event_id)})
            except WebhookUnreachable as error:
                if delivery.attempts + 1 >= _MAX_ATTEMPTS:
                    delivery_repo.mark(delivery, state=DeliveryState.ABANDONED, error=str(error))
                else:
                    delivery_repo.mark(delivery, state=DeliveryState.FAILED, error=str(error))
                continue

            delivery_repo.mark(delivery, state=DeliveryState.DELIVERED, error=None)
            delivered += 1

        await session.commit()
    return delivered
