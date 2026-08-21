"""Theme query expansion: turns a description into search queries.

Rehomes the published pipeline's case-study-to-queries flow (handoff §1.4)
as theme seeding, through the same tier-router-and-schema shape every other
LLM call in this system uses (mlsc/pipeline/intent.py).
"""

from __future__ import annotations

from pydantic import BaseModel

from mlsc.llm.base import Completion
from mlsc.llm.prompts import load_prompt
from mlsc.llm.router import LlmRouter, Tier
from mlsc.pipeline.enrich import Embedder

PROMPT_VERSION = "v1"

_MAX_QUERIES = 8


class QueriesUnusable(RuntimeError):
    """Expansion produced no queries, or more than the bound allows.

    A model asked for queries will happily return forty, and each one
    multiplies every discovery pass and its quota (learn.md, "Query
    expansion is a precision-recall trade") — bounding the set here is a
    correctness concern, not tidiness.
    """


class GeneratedQuery(BaseModel):
    text: str
    rationale: str


class GeneratedQuerySet(BaseModel):
    queries: list[GeneratedQuery]


async def generate_theme_queries(router: LlmRouter, description: str) -> Completion:
    """One completion expanding ``description`` into a bounded, non-empty
    query set. Raises ``QueriesUnusable`` if the result is empty or too
    large; the caller decides how that failure is recorded."""
    provider = router.for_tier(Tier.LABELING)
    prompt_template = load_prompt("theme_query", PROMPT_VERSION)
    prompt = prompt_template.format(description=description)
    completion = await provider.complete(
        prompt=prompt, schema=GeneratedQuerySet, prompt_version=PROMPT_VERSION
    )
    if not completion.value.queries or len(completion.value.queries) > _MAX_QUERIES:
        raise QueriesUnusable(
            f"expansion returned {len(completion.value.queries)} queries, "
            f"expected 1 to {_MAX_QUERIES}"
        )
    return completion


def reference_embeddings_for_basis(
    basis: str, *, description: str, queries: list[str], corpus_embeddings: list[list[float]],
    embedder: Embedder,
) -> list[list[float]]:
    """Build the reference vector set a ``ThemeRelevanceScorer`` compares
    each document against, for the configured basis (theme-monitors
    requirement 7; ``RelevanceBasis``).

    Kept separate from the scorer itself: the scorer only ever compares two
    sets of vectors, and this is the one place that knows how each basis is
    turned into those vectors, so a later change to how a basis is built
    touches this function, not the scorer or its call site.
    """
    if basis == "description":
        return embedder.encode([description])
    if basis == "queries":
        return embedder.encode(queries) if queries else []
    return corpus_embeddings
