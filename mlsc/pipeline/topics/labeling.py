"""Keyword extraction and labelling for a discovered candidate.

c-TF-IDF concatenates each candidate's documents into one pseudo-document and
asks which terms are distinctive to that pseudo-document relative to the
others in the same discovery pass (learn.md, "c-TF-IDF names a cluster").
The keywords are the fallback label when the LLM is unreachable, which is why
they are computed first and unconditionally.
"""

from __future__ import annotations

from pydantic import BaseModel
from sklearn.feature_extraction.text import TfidfVectorizer

from mlsc.llm.base import Completion, ProviderUnreachable, SchemaViolation
from mlsc.llm.prompts import load_prompt
from mlsc.llm.router import LlmRouter, Tier

PROMPT_VERSION = "v1"
_TOP_KEYWORDS = 8
_EXAMPLE_DOCUMENTS = 5


class LabelUnavailable(RuntimeError):
    """Label generation failed after its retry; the caller falls back to
    keywords rather than losing the topic (design.md, "Failure strategy")."""


class TopicLabel(BaseModel):
    label: str


def extract_keywords(pseudo_documents: list[str], *, index: int) -> list[str]:
    """The keywords distinctive to ``pseudo_documents[index]`` among the others
    in this same discovery pass (c-TF-IDF, one class per candidate)."""
    vectorizer = TfidfVectorizer(max_features=2000, stop_words="english")
    matrix = vectorizer.fit_transform(pseudo_documents)
    terms = vectorizer.get_feature_names_out()
    row = matrix[index].toarray()[0]
    top_indices = row.argsort()[::-1][:_TOP_KEYWORDS]
    return [terms[i] for i in top_indices if row[i] > 0]


async def generate_label(
    router: LlmRouter, *, keywords: list[str], example_texts: list[str]
) -> Completion:
    """One completion naming the group; raises ``LabelUnavailable`` after the
    provider's own retry is exhausted."""
    provider = router.for_tier(Tier.LABELING)
    prompt_template = load_prompt("topic_label", PROMPT_VERSION)
    examples = "\n".join(f"- {text}" for text in example_texts[:_EXAMPLE_DOCUMENTS])
    prompt = prompt_template.format(keywords=", ".join(keywords), examples=examples)
    try:
        return await provider.complete(
            prompt=prompt, schema=TopicLabel, prompt_version=PROMPT_VERSION
        )
    except (SchemaViolation, ProviderUnreachable) as error:
        raise LabelUnavailable(str(error)) from error
