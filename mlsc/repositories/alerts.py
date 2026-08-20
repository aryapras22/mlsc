"""Scraper alert persistence. No notifier — delivery lives in metrics-read-api."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import AlertKind, ScraperAlert


class ScraperAlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def raise_alert(
        self,
        *,
        monitor_id: uuid.UUID,
        monitor_source_id: uuid.UUID,
        kind: AlertKind,
        observed: str | None = None,
        expected: str | None = None,
    ) -> ScraperAlert:
        alert = ScraperAlert(
            id=uuid.uuid4(),
            monitor_id=monitor_id,
            monitor_source_id=monitor_source_id,
            kind=kind,
            observed=observed,
            expected=expected,
        )
        self._session.add(alert)
        return alert
