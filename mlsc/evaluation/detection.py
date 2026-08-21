"""Event matching: pairing detected events to a labelled event set within a
tolerance window, and deriving precision, recall and lead time per method
and for the ensemble (requirement 4).

Matching is monitor-level, not topic-level: a labelled real-world event
("an outage", "a feature launch") is a fact about the monitored target, not
about which topic the system happened to attach a change to, so any fired
event within the tolerance window counts as a detection of that label.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import DetectionMethod, EventLabel, TrendEvent
from mlsc.evaluation.measures import Measure, computed, unavailable_no_labels

_DEFAULT_TOLERANCE_DAYS = 3


@dataclasses.dataclass(frozen=True)
class MatchResult:
    label: EventLabel
    matched_event_id: uuid.UUID
    lead_time_days: int
    method: DetectionMethod | None


def match_events(
    events: list[TrendEvent], labels: list[EventLabel], *, tolerance_days: int
) -> list[MatchResult]:
    """Greedily pairs each label to its closest unmatched event within
    ``tolerance_days``, earliest events first, so an early detection is
    preferred over a late one when both are candidates for the same label.

    ``lead_time_days`` is ``label.occurred_on - event.detected_on``:
    positive means the system fired before the labelled event's own date,
    negative means after.
    """
    unmatched_events = sorted(events, key=lambda event: event.detected_on)
    matches: list[MatchResult] = []
    used_event_ids: set[uuid.UUID] = set()

    for label in labels:
        candidates = [
            event for event in unmatched_events
            if event.id not in used_event_ids
            and abs((event.detected_on - label.occurred_on).days) <= tolerance_days
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda event: abs((event.detected_on - label.occurred_on).days))
        used_event_ids.add(best.id)
        matches.append(
            MatchResult(
                label=label, matched_event_id=best.id,
                lead_time_days=(label.occurred_on - best.detected_on).days, method=best.method,
            )
        )
    return matches


async def measure_detection(
    session: AsyncSession, *, monitor_id: uuid.UUID, labels: list[EventLabel],
    tolerance_days: int = _DEFAULT_TOLERANCE_DAYS,
) -> dict[str, tuple[Measure, Measure, Measure]]:
    """One (precision, recall, lead_time) triple per detection method, plus
    one for ``"ensemble"`` — every method's events pooled together.
    """
    if not labels:
        return {
            "ensemble": (
                unavailable_no_labels("detection_precision_ensemble", computed_over="0 event labels"),
                unavailable_no_labels("detection_recall_ensemble", computed_over="0 event labels"),
                unavailable_no_labels("detection_lead_time_ensemble", computed_over="0 event labels"),
            )
        }

    result = await session.execute(select(TrendEvent).where(TrendEvent.monitor_id == monitor_id))
    events = list(result.scalars().all())

    results: dict[str, tuple[Measure, Measure, Measure]] = {}
    for method in DetectionMethod:
        method_events = [event for event in events if event.method is method]
        results[method.value] = _triple(method_events, labels, tolerance_days=tolerance_days, arm=method.value)

    results["ensemble"] = _triple(events, labels, tolerance_days=tolerance_days, arm="ensemble")
    return results


def _triple(
    events: list[TrendEvent], labels: list[EventLabel], *, tolerance_days: int, arm: str
) -> tuple[Measure, Measure, Measure]:
    matches = match_events(events, labels, tolerance_days=tolerance_days)

    precision = len(matches) / len(events) if events else 0.0
    recall = len(matches) / len(labels)
    lead_time = sum(match.lead_time_days for match in matches) / len(matches) if matches else 0.0

    computed_over = f"{len(events)} events against {len(labels)} labels"
    return (
        computed(f"detection_precision_{arm}", precision, sample_size=len(events), computed_over=computed_over),
        computed(f"detection_recall_{arm}", recall, sample_size=len(labels), computed_over=computed_over),
        computed(f"detection_lead_time_{arm}", lead_time, sample_size=len(matches), computed_over=computed_over),
    )
