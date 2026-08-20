"""Figure computation over one group of documents: a single (source, topic)
breakdown for one bucket.

A pure function over already-loaded rows, with no session and no query of
its own — the grouping pass in ``group.py`` decides what belongs in a group,
this only turns a group into numbers, which is what lets the exact same
computation run once for the fine-grained breakdown and again for every
coarser aggregate (design.md, "Success path": "reuses one computation").
"""

from __future__ import annotations

import dataclasses
import statistics

from mlsc.db.models import Document, Enrichment, SentimentLabel


@dataclasses.dataclass(frozen=True)
class DocumentRow:
    """The slice of a document and its enrichment this layer needs, so the
    computation below depends on a small shape rather than the ORM models."""

    author_hash: str
    rating: int | None
    engagement: int | None
    sentiment_score: float | None
    sentiment_label: SentimentLabel | None
    intent: str | None


@dataclasses.dataclass(frozen=True)
class GroupFigures:
    doc_count: int
    doc_count_share: float
    sentiment_mean: float | None
    sentiment_p25: float | None
    negativity_rate: float | None
    engagement_sum: int | None
    author_diversity: float | None
    rating_mean: float | None
    intent_counts: dict[str, int]


def row_from(document: Document, enrichment: Enrichment) -> DocumentRow:
    return DocumentRow(
        author_hash=document.author_hash,
        rating=document.rating,
        engagement=document.engagement,
        sentiment_score=enrichment.sentiment_score,
        sentiment_label=enrichment.sentiment_label,
        intent=enrichment.intent.value if enrichment.intent else None,
    )


def compute_figures(rows: list[DocumentRow], *, sample_size: int) -> GroupFigures:
    """Requirement 3: the share is the quantity a chart should plot, stored
    beside the count rather than computed at read time (design.md, "Domain
    shapes": ``doc_count_norm``)."""
    doc_count = len(rows)
    sentiment_scores = [row.sentiment_score for row in rows if row.sentiment_score is not None]
    ratings = [row.rating for row in rows if row.rating is not None]
    engagements = [row.engagement for row in rows if row.engagement is not None]
    negative_count = sum(1 for row in rows if row.sentiment_label is SentimentLabel.NEGATIVE)
    distinct_authors = {row.author_hash for row in rows}

    intent_counts: dict[str, int] = {}
    for row in rows:
        if row.intent is not None:
            intent_counts[row.intent] = intent_counts.get(row.intent, 0) + 1

    return GroupFigures(
        doc_count=doc_count,
        doc_count_share=doc_count / sample_size if sample_size else 0.0,
        sentiment_mean=statistics.fmean(sentiment_scores) if sentiment_scores else None,
        sentiment_p25=_percentile_25(sentiment_scores) if sentiment_scores else None,
        negativity_rate=negative_count / doc_count if doc_count else None,
        engagement_sum=sum(engagements) if engagements else None,
        author_diversity=len(distinct_authors) / doc_count if doc_count else None,
        rating_mean=statistics.fmean(ratings) if ratings else None,
        intent_counts=intent_counts,
    )


def _percentile_25(values: list[float]) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # statistics.quantiles requires at least two data points.
    return statistics.quantiles(ordered, n=4, method="inclusive")[0]
