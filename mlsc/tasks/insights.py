"""The generation task: per active topic through the insight tier with one
retry, skipping rather than caveating, continuing past one topic's failure
(design.md, "Success path"; requirements 1, 2, 4, 5, 7).

The digest is composed last, over this pass's own written opportunities —
never from raw metrics — so it cannot assert a change no opportunity
supports (design.md, "Success path": "the cheapest available guard against
a fluent summary of nothing").
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import SkipReason, Topic, TopicStatus
from mlsc.llm.cache import LlmResponseCache
from mlsc.llm.router import LlmRouter
from mlsc.pipeline.insights import context as context_module
from mlsc.pipeline.insights import gating
from mlsc.pipeline.insights.grounding import EvidenceUnlinkable, validate as validate_evidence
from mlsc.pipeline.insights.prompts import (
    DIGEST_PROMPT_VERSION,
    OPPORTUNITY_PROMPT_VERSION,
    GenerationFailed,
    OpportunityOutput,
    generate_digest,
    generate_opportunity,
)
from mlsc.pipeline.insights.scoring import ScoreWeights, score as score_opportunity
from mlsc.pipeline.normalize import hash_content
from mlsc.repositories.insights import GenerationSkipRepository, InsightRepository
from mlsc.repositories.topics import TopicRepository

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class WrittenOpportunity:
    insight_id: uuid.UUID
    topic_id: uuid.UUID
    title: str
    body: str
    evidence_ids: list[str]


@dataclasses.dataclass(frozen=True)
class GenerationOutcome:
    opportunities_written: int
    skips_recorded: int
    digest_written: bool


async def generate_insights(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    period_start: date,
    period_end: date,
    llm_router: LlmRouter,
    cache: LlmResponseCache | None = None,
    score_weights: ScoreWeights | None = None,
) -> GenerationOutcome:
    score_weights = score_weights or ScoreWeights()

    async with session_factory() as session:
        topic_repo = TopicRepository(session)
        insight_repo = InsightRepository(session)

        topics = await topic_repo.list_for_monitor(monitor_id, statuses=(TopicStatus.ACTIVE,))
        written: list[WrittenOpportunity] = []
        skipped = 0

        day_trustworthy = await gating.day_is_trustworthy(
            session, monitor_id=monitor_id, period_start=period_start, period_end=period_end
        )

        for topic in topics:
            outcome = await _generate_for_topic(
                session, topic=topic, monitor_id=monitor_id, period_start=period_start,
                period_end=period_end, day_trustworthy=day_trustworthy, llm_router=llm_router,
                cache=cache, score_weights=score_weights, insight_repo=insight_repo,
            )
            if outcome is None:
                skipped += 1
                continue
            written.append(outcome)

        digest_written = False
        if written:
            digest_written = await _compose_digest(
                session, monitor_id=monitor_id, period_start=period_start, period_end=period_end,
                written=written, llm_router=llm_router, insight_repo=insight_repo,
            )

        await session.commit()

    return GenerationOutcome(
        opportunities_written=len(written), skips_recorded=skipped, digest_written=digest_written
    )


async def _generate_for_topic(
    session: AsyncSession,
    *,
    topic: Topic,
    monitor_id: uuid.UUID,
    period_start: date,
    period_end: date,
    day_trustworthy: bool,
    llm_router: LlmRouter,
    cache: LlmResponseCache | None,
    score_weights: ScoreWeights,
    insight_repo: InsightRepository,
) -> WrittenOpportunity | None:
    skip_repo = GenerationSkipRepository(session)

    def skip(reason: SkipReason) -> None:
        skip_repo.write(
            monitor_id=monitor_id, topic_id=topic.id, period_start=period_start,
            period_end=period_end, reason=reason,
        )

    if not day_trustworthy:
        skip(SkipReason.DAY_UNTRUSTWORTHY)
        return None

    try:
        topic_context = await context_module.assemble(
            session, topic=topic, period_start=period_start, period_end=period_end
        )
    except context_module.ContextEmpty:
        skip(SkipReason.EVIDENCE_TOO_THIN)
        return None

    if gating.evidence_is_thin(len(topic_context.representatives)):
        skip(SkipReason.EVIDENCE_TOO_THIN)
        return None

    has_change = await gating.has_gated_change(
        session, monitor_id=monitor_id, topic_id=topic.id, period_start=period_start, period_end=period_end
    )
    if not has_change:
        skip(SkipReason.NO_CHANGE_DETECTED)
        return None

    context_hash = _context_hash(topic_context)
    cached = await cache.get(context_hash, OPPORTUNITY_PROMPT_VERSION) if cache is not None else None
    if cached is not None:
        output = OpportunityOutput.model_validate(cached["value"])
        provider, model = cached["provider"], cached["model"]
    else:
        try:
            completion = await generate_opportunity(llm_router, topic_context)
        except GenerationFailed:
            logger.warning("opportunity generation failed for topic %s", topic.id)
            skip(SkipReason.GENERATION_FAILED)
            return None
        output, provider, model = completion.value, completion.provider, completion.model
        if cache is not None:
            await cache.put(
                context_hash, OPPORTUNITY_PROMPT_VERSION,
                {"value": output.model_dump(), "provider": provider, "model": model},
            )

    try:
        validate_evidence(topic_context, output.evidence_ids)
    except EvidenceUnlinkable:
        # Discard the whole completion, per design.md's failure strategy —
        # a model that invented one citation is not grounded in this context.
        logger.warning("evidence unlinkable for topic %s, discarding completion", topic.id)
        skip(SkipReason.GENERATION_FAILED)
        return None

    days_since_last_mention = max(0, (period_end - topic.last_seen).days)
    opportunity_score = score_opportunity(
        topic_context.statistics, days_since_last_mention=days_since_last_mention, weights=score_weights
    )

    insight_id = await insight_repo.upsert_opportunity(
        monitor_id=monitor_id, topic_id=topic.id, period_start=period_start, period_end=period_end,
        title=output.title, body=output.body, who=output.who, what=output.what, why=output.why,
        score=opportunity_score.value, score_components=opportunity_score.components,
        evidence_ids=output.evidence_ids, llm_provider=provider, llm_model=model,
        prompt_version=OPPORTUNITY_PROMPT_VERSION,
    )

    return WrittenOpportunity(
        insight_id=insight_id, topic_id=topic.id, title=output.title, body=output.body,
        evidence_ids=output.evidence_ids,
    )


async def _compose_digest(
    session: AsyncSession,
    *,
    monitor_id: uuid.UUID,
    period_start: date,
    period_end: date,
    written: list[WrittenOpportunity],
    llm_router: LlmRouter,
    insight_repo: InsightRepository,
) -> bool:
    summaries = [f"{opportunity.title}: {opportunity.body}" for opportunity in written]
    try:
        completion = await generate_digest(
            llm_router, period_start=period_start, period_end=period_end, opportunity_summaries=summaries
        )
    except GenerationFailed:
        # Fall back: keep the opportunities, record the digest as missing
        # (design.md, "Failure strategy": "Digest composition failure").
        logger.warning("digest generation failed for monitor %s period %s..%s", monitor_id, period_start, period_end)
        return False

    evidence_ids = sorted({evidence_id for opportunity in written for evidence_id in opportunity.evidence_ids})
    await insight_repo.upsert_digest(
        monitor_id=monitor_id, period_start=period_start, period_end=period_end,
        title=f"Digest {period_start.isoformat()}..{period_end.isoformat()}",
        body=completion.value.body, evidence_ids=evidence_ids,
        llm_provider=completion.provider, llm_model=completion.model,
        prompt_version=DIGEST_PROMPT_VERSION,
    )
    return True


def _context_hash(topic_context: context_module.TopicContext) -> str:
    representative_ids = sorted(str(representative.document_id) for representative in topic_context.representatives)
    return hash_content(
        str(topic_context.topic_id), topic_context.period_start.isoformat(),
        topic_context.period_end.isoformat(), *representative_ids,
    )
