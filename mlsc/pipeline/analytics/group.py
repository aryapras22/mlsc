"""Groups a bucket's enriched documents by source and topic, then reuses
``compute_figures`` for the fine-grained rows and every coarser aggregate.

A negativity rate computed three different ways — once by source, once by
topic, once overall — will eventually disagree by rounding. Building every
breakdown from one grouping pass over the same rows is what design.md's
"Success path" means by "reuses one computation rather than three".
"""

from __future__ import annotations

import uuid

from mlsc.db.models import SourceName
from mlsc.pipeline.analytics.normalization import SampleContext
from mlsc.pipeline.analytics.rollup import DocumentRow, GroupFigures, compute_figures


class GroupedFigures:
    """One row the caller can write, carrying the key that identifies it.

    ``source_name`` and ``topic_id`` are each ``None`` to mean "every value",
    matching ``DailyMetric``'s all-value admission (design.md, "Domain
    shapes": ``MetricKey``)."""

    __slots__ = ("source_name", "topic_id", "sample_size", "quota_hit", "figures")

    def __init__(
        self,
        *,
        source_name: SourceName | None,
        topic_id: uuid.UUID | None,
        sample_size: int,
        quota_hit: bool,
        figures: GroupFigures,
    ) -> None:
        self.source_name = source_name
        self.topic_id = topic_id
        self.sample_size = sample_size
        self.quota_hit = quota_hit
        self.figures = figures


def rollup_bucket(
    rows_by_source_topic: dict[tuple[SourceName, uuid.UUID | None], list[DocumentRow]],
    contexts_by_source: dict[SourceName, SampleContext],
) -> list[GroupedFigures]:
    """Every breakdown for one bucket: by (source, topic), by source, by
    topic, and monitor-level, all from the same input rows.

    ``rows_by_source_topic`` groups by ``topic_id=None`` for a document with
    no assignment yet — such a document still counts toward its source's and
    the monitor's totals, it simply has no topic-level row.
    """
    results: list[GroupedFigures] = []

    # by_source_by_topic: the finest breakdown, one row per topic actually
    # present for a source (never the all-topics row here).
    for (source_name, topic_id), rows in rows_by_source_topic.items():
        context = contexts_by_source.get(source_name)
        if context is None:
            continue  # the source is absent for this bucket; nothing to write
        results.append(
            GroupedFigures(
                source_name=source_name,
                topic_id=topic_id,
                sample_size=context.sample_size,
                quota_hit=context.quota_hit,
                figures=compute_figures(rows, sample_size=context.sample_size),
            )
        )

    # by_source: every topic's rows for that source, aggregated together.
    rows_by_source: dict[SourceName, list[DocumentRow]] = {}
    for (source_name, _topic_id), rows in rows_by_source_topic.items():
        rows_by_source.setdefault(source_name, []).extend(rows)
    for source_name, rows in rows_by_source.items():
        context = contexts_by_source.get(source_name)
        if context is None:
            continue
        results.append(
            GroupedFigures(
                source_name=source_name,
                topic_id=None,
                sample_size=context.sample_size,
                quota_hit=context.quota_hit,
                figures=compute_figures(rows, sample_size=context.sample_size),
            )
        )

    # by_topic: every source's rows for that topic, aggregated together,
    # against the monitor's total sample across every present source.
    total_sample_size = sum(context.sample_size for context in contexts_by_source.values())
    any_quota_hit = any(context.quota_hit for context in contexts_by_source.values())

    rows_by_topic: dict[uuid.UUID | None, list[DocumentRow]] = {}
    for (source_name, topic_id), rows in rows_by_source_topic.items():
        if source_name not in contexts_by_source:
            continue
        rows_by_topic.setdefault(topic_id, []).extend(rows)
    for topic_id, rows in rows_by_topic.items():
        if topic_id is None:
            continue  # the all-source, no-topic row is the monitor-level row below
        results.append(
            GroupedFigures(
                source_name=None,
                topic_id=topic_id,
                sample_size=total_sample_size,
                quota_hit=any_quota_hit,
                figures=compute_figures(rows, sample_size=total_sample_size),
            )
        )

    # monitor_level: every row from every present source, regardless of topic.
    # rows_by_source still holds an absent source's rows (grouped above from
    # rows_by_source_topic before that source was excluded), so they must be
    # filtered out here too rather than counted toward a monitor total whose
    # sample size does not include them. Written only when at least one
    # source is present: with every source absent there is no sample at all,
    # and a monitor-level row of doc_count=0 over sample_size=0 would be
    # exactly the zero-standing-in-for-absence requirement 4 forbids.
    if contexts_by_source:
        all_rows = [
            row
            for source_name, rows in rows_by_source.items()
            if source_name in contexts_by_source
            for row in rows
        ]
        results.append(
            GroupedFigures(
                source_name=None,
                topic_id=None,
                sample_size=total_sample_size,
                quota_hit=any_quota_hit,
                figures=compute_figures(all_rows, sample_size=total_sample_size),
            )
        )

    return results
