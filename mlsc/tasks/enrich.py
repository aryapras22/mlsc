"""Enrichment task, taking a stage set so a model upgrade re-runs one stage
without re-fetching anything (requirement 8, 9).
"""

from __future__ import annotations

import uuid
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import Document, Enrichment
from mlsc.llm.cache import LlmResponseCache
from mlsc.llm.router import LlmRouter
from mlsc.pipeline.duplicate import is_near_duplicate, simhash
from mlsc.pipeline.enrich import Embedder, SentimentScorer
from mlsc.pipeline.intent import PROMPT_VERSION, classify_intents
from mlsc.pipeline.language import detect_language
from mlsc.pipeline.normalize import clean_text, hash_content, strip_pii
from mlsc.pipeline.relevance import is_relevant, score_relevance

_INTENT_BATCH_SIZE = 40


class Stage(str, Enum):
    CLEAN = "clean"
    LANGUAGE = "language"
    RELEVANCE = "relevance"
    DUPLICATE = "duplicate"
    EMBED = "embed"
    SENTIMENT = "sentiment"
    INTENT = "intent"


ALL_STAGES = frozenset(Stage)


async def enrich_documents(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    stages: frozenset[Stage],
    embedder: Embedder,
    sentiment_scorer: SentimentScorer,
    llm_router: LlmRouter | None = None,
    llm_cache: LlmResponseCache | None = None,
    accepted_languages: frozenset[str] | None = None,
) -> int:
    """Enrich every document for a monitor missing at least one requested stage.

    Returns the count of documents written. Cleaning always runs first because
    every later stage reads the cleaned text (design.md, "Success path").
    """
    async with session_factory() as session:
        result = await session.execute(
            select(Document).where(Document.monitor_id == monitor_id)
        )
        documents = list(result.scalars().all())

        written = 0
        pending_intent: list[tuple[uuid.UUID, str]] = []
        content_hashes: dict[uuid.UUID, str] = {}
        seen_fingerprints: list[tuple[uuid.UUID, int]] = []

        for document in documents:
            enrichment_result = await session.execute(
                select(Enrichment).where(Enrichment.document_id == document.id)
            )
            enrichment = enrichment_result.scalar_one_or_none()
            if enrichment is None:
                enrichment = Enrichment(
                    id=uuid.uuid4(), document_id=document.id, model_versions={}
                )
                session.add(enrichment)

            cleaned = clean_text(document.body)
            stripped = strip_pii(cleaned)
            if Stage.CLEAN in stages and stripped is not None:
                document.body = stripped

            if Stage.LANGUAGE in stages:
                verdict = detect_language(stripped)
                if verdict is not None:
                    enrichment.language = verdict.code
                    enrichment.language_confidence = verdict.confidence
                if accepted_languages and verdict and verdict.code not in accepted_languages:
                    enrichment.is_relevant = False
                    enrichment.model_versions = {
                        **enrichment.model_versions, "language": "langdetect"
                    }
                    written += 1
                    continue

            if Stage.RELEVANCE in stages:
                score = score_relevance(stripped)
                enrichment.relevance_score = score
                enrichment.is_relevant = is_relevant(score)

            if Stage.DUPLICATE in stages and stripped:
                fingerprint = simhash(stripped)
                match = next(
                    (
                        seen_id
                        for seen_id, seen_fingerprint in seen_fingerprints
                        if is_near_duplicate(fingerprint, seen_fingerprint)
                    ),
                    None,
                )
                enrichment.near_duplicate_of = match
                seen_fingerprints.append((document.id, fingerprint))
                enrichment.model_versions = {
                    **enrichment.model_versions, "duplicate": str(fingerprint)
                }

            if Stage.EMBED in stages and stripped:
                [vector] = embedder.encode([stripped])
                enrichment.embedding = vector
                enrichment.model_versions = {
                    **enrichment.model_versions, "embed": "all-MiniLM-L6-v2"
                }

            if Stage.SENTIMENT in stages and stripped:
                sentiment = sentiment_scorer.score(stripped)
                enrichment.sentiment_score = sentiment.score
                enrichment.sentiment_label = sentiment.label
                enrichment.model_versions = {**enrichment.model_versions, "sentiment": "vader"}

            if Stage.INTENT in stages and stripped and llm_router is not None:
                content_hashes[document.id] = hash_content(stripped)
                pending_intent.append((document.id, stripped))

            written += 1

        if Stage.INTENT in stages and llm_router is not None and pending_intent:
            uncached: list[tuple[uuid.UUID, str]] = []
            for document_id, text in pending_intent:
                cached = (
                    await llm_cache.get(content_hashes[document_id], PROMPT_VERSION)
                    if llm_cache is not None
                    else None
                )
                if cached is None:
                    uncached.append((document_id, text))
                    continue
                await _apply_intent_result(session, document_id, cached)

            for start in range(0, len(uncached), _INTENT_BATCH_SIZE):
                batch = uncached[start : start + _INTENT_BATCH_SIZE]
                completion = await classify_intents(llm_router, batch)
                results_by_id = {r.document_id: r for r in completion.value.results}
                for document_id, _text in batch:
                    intent_result = results_by_id.get(str(document_id))
                    if intent_result is None:
                        continue
                    values = {
                        "intent": intent_result.intent.value,
                        "confidence": intent_result.confidence,
                        "provider": completion.provider,
                        "model": completion.model,
                        "prompt_version": completion.prompt_version,
                    }
                    await _apply_intent_result(session, document_id, values)
                    if llm_cache is not None:
                        await llm_cache.put(
                            content_hashes[document_id], PROMPT_VERSION, values
                        )

        await session.commit()
        return written


async def _apply_intent_result(
    session: AsyncSession, document_id: uuid.UUID, values: dict
) -> None:
    result = await session.execute(
        select(Enrichment).where(Enrichment.document_id == document_id)
    )
    enrichment = result.scalar_one()
    enrichment.intent = values["intent"]
    enrichment.intent_confidence = values["confidence"]
    enrichment.llm_provider = values["provider"]
    enrichment.llm_model = values["model"]
    enrichment.prompt_version = values["prompt_version"]
