"""The declared output shapes for the opportunity and digest prompts, and
the calls that produce them — rehoming the prior codebase's WHO/WHAT/WHY
extraction to topic level (requirement 2, 6; learn.md, "Why topic level
beats document level").
"""

from __future__ import annotations

from pydantic import BaseModel

from mlsc.llm.base import Completion, ProviderUnreachable, SchemaViolation
from mlsc.llm.prompts import load_prompt
from mlsc.llm.router import LlmRouter, Tier
from mlsc.pipeline.insights.context import TopicContext

OPPORTUNITY_PROMPT_VERSION = "v1"
DIGEST_PROMPT_VERSION = "v1"


class GenerationFailed(RuntimeError):
    """Output did not validate after the provider's own retry (design.md,
    "Named failures": ``SchemaViolation``), or the provider was unreachable.
    The caller records ``generation_failed`` and moves on."""


class OpportunityOutput(BaseModel):
    title: str
    who: str
    what: str
    why: str
    body: str
    evidence_ids: list[str]


class DigestOutput(BaseModel):
    body: str


def render_opportunity_prompt(context: TopicContext) -> str:
    statistics_lines = [
        f"documents this period: {context.statistics.doc_count}",
        f"share of the topic's own sample: {context.statistics.doc_count_share:.2f}"
        if context.statistics.doc_count_share is not None else "share: unavailable",
        f"mean sentiment: {context.statistics.sentiment_mean:.2f}"
        if context.statistics.sentiment_mean is not None else "mean sentiment: unavailable",
        f"negativity rate: {context.statistics.negativity_rate:.2f}"
        if context.statistics.negativity_rate is not None else "negativity rate: unavailable",
        f"breadth across active sources: {context.statistics.breadth_ratio:.2f}",
        f"trend score: {context.statistics.trend_score:.2f}"
        if context.statistics.trend_score is not None else "trend score: unavailable",
        f"intent mix: {context.statistics.intent_mix}",
    ]
    document_lines = [
        f"- id={representative.document_id} source={representative.source.value} "
        f"rating={representative.rating}: {representative.excerpt}"
        for representative in context.representatives
    ]

    template = load_prompt("opportunity", OPPORTUNITY_PROMPT_VERSION)
    return template.format(
        topic_label=context.topic_label,
        period_start=context.period_start.isoformat(),
        period_end=context.period_end.isoformat(),
        statistics="\n".join(statistics_lines),
        documents="\n".join(document_lines),
    )


async def generate_opportunity(router: LlmRouter, context: TopicContext) -> Completion:
    """Requirement 4: the model receives the topic, its representatives and
    its statistics — never called per document. Raises ``GenerationFailed``
    after the provider's own retry is exhausted, per design.md's failure
    strategy for ``SchemaViolation``."""
    provider = router.for_tier(Tier.INSIGHT)
    prompt = render_opportunity_prompt(context)
    try:
        return await provider.complete(
            prompt=prompt, schema=OpportunityOutput, prompt_version=OPPORTUNITY_PROMPT_VERSION
        )
    except (SchemaViolation, ProviderUnreachable) as error:
        raise GenerationFailed(str(error)) from error


def render_digest_prompt(*, period_start, period_end, opportunity_summaries: list[str]) -> str:
    template = load_prompt("digest", DIGEST_PROMPT_VERSION)
    opportunities = "\n".join(f"- {summary}" for summary in opportunity_summaries)
    return template.format(
        period_start=period_start.isoformat(), period_end=period_end.isoformat(),
        opportunities=opportunities,
    )


async def generate_digest(router: LlmRouter, *, period_start, period_end, opportunity_summaries: list[str]) -> Completion:
    provider = router.for_tier(Tier.INSIGHT)
    prompt = render_digest_prompt(
        period_start=period_start, period_end=period_end, opportunity_summaries=opportunity_summaries
    )
    try:
        return await provider.complete(
            prompt=prompt, schema=DigestOutput, prompt_version=DIGEST_PROMPT_VERSION
        )
    except (SchemaViolation, ProviderUnreachable) as error:
        raise GenerationFailed(str(error)) from error
