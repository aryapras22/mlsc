"""The generation comparison: two tier configurations over the same
topics, scored on who, what and why separately (requirement 7) — kept
separate rather than averaged, because the published benchmark's finding
is precisely that the three fields do not score alike (design.md,
"Domain shapes": ``QualityField``).

There is no human-labelled reference text for WHO/WHAT/WHY in this system,
so "the reference" each arm is scored against is the evidence itself: the
topic's own representative documents, the same context both arms were
given. A field's score is how well its text is actually grounded in that
evidence — the mean cosine similarity, in embedding space, between the
field's text and the representative documents' — which is the same
groundedness this whole architecture already insists on (C4), applied as
a comparative measurement instead of a pass/fail gate.
"""

from __future__ import annotations

import dataclasses

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from mlsc.evaluation.measures import Measure, computed
from mlsc.llm.router import LlmRouter
from mlsc.pipeline.insights.context import TopicContext
from mlsc.pipeline.insights.prompts import GenerationFailed, generate_opportunity


@dataclasses.dataclass(frozen=True)
class Embedder:
    """The same embedding interface ``document-enrichment`` uses, injected
    so this measure never constructs its own model."""

    encode: object  # Callable[[list[str]], list[list[float]]]


async def measure_generation(
    contexts: list[TopicContext], *, arms: dict[str, LlmRouter], embedder: Embedder
) -> dict[str, dict[str, Measure]]:
    """Requirement 7: for each arm, generate an opportunity per topic and
    score who, what and why against that topic's own evidence. Returns
    ``{arm_name: {"who": Measure, "what": Measure, "why": Measure}}``.
    """
    results: dict[str, dict[str, Measure]] = {}
    for arm_name, router in arms.items():
        who_scores: list[float] = []
        what_scores: list[float] = []
        why_scores: list[float] = []
        attempted = 0

        for context in contexts:
            attempted += 1
            try:
                completion = await generate_opportunity(router, context)
            except GenerationFailed:
                continue

            evidence_texts = [representative.excerpt for representative in context.representatives]
            who_scores.append(_groundedness(completion.value.who, evidence_texts, embedder))
            what_scores.append(_groundedness(completion.value.what, evidence_texts, embedder))
            why_scores.append(_groundedness(completion.value.why, evidence_texts, embedder))

        computed_over = f"{len(who_scores)} of {attempted} topics generated"
        results[arm_name] = {
            "who": _field_measure(f"generation_who_{arm_name}", who_scores, computed_over),
            "what": _field_measure(f"generation_what_{arm_name}", what_scores, computed_over),
            "why": _field_measure(f"generation_why_{arm_name}", why_scores, computed_over),
        }
    return results


def _groundedness(field_text: str, evidence_texts: list[str], embedder: Embedder) -> float:
    if not field_text.strip() or not evidence_texts:
        return 0.0
    field_embedding = embedder.encode([field_text])[0]
    evidence_embeddings = embedder.encode(evidence_texts)
    similarities = cosine_similarity([field_embedding], evidence_embeddings)[0]
    return float(np.mean(similarities))


def _field_measure(name: str, scores: list[float], computed_over: str) -> Measure:
    if not scores:
        from mlsc.evaluation.measures import unavailable_no_labels

        return unavailable_no_labels(name, computed_over=computed_over)
    return computed(name, float(np.mean(scores)), sample_size=len(scores), computed_over=computed_over)
