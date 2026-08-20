"""Intent classification, batched through the router with a declared schema."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from mlsc.db.models import Intent
from mlsc.llm.base import Completion
from mlsc.llm.prompts import load_prompt
from mlsc.llm.router import LlmRouter, Tier

PROMPT_VERSION = "v1"


class IntentResult(BaseModel):
    document_id: str
    intent: Intent
    confidence: float


class IntentBatchResult(BaseModel):
    results: list[IntentResult]


async def classify_intents(
    router: LlmRouter, documents: list[tuple[uuid.UUID, str]]
) -> Completion:
    """One completion per batch of documents; the caller decides batch size."""
    provider = router.for_tier(Tier.INTENT)
    prompt_template = load_prompt("intent", PROMPT_VERSION)
    formatted = "\n".join(f"{doc_id}: {text}" for doc_id, text in documents)
    prompt = prompt_template.format(documents=formatted)
    return await provider.complete(
        prompt=prompt, schema=IntentBatchResult, prompt_version=PROMPT_VERSION
    )
