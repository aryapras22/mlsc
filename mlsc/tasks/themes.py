"""Theme seeding tasks: query generation and entity discovery.

Each function receives a session factory and the collaborators it needs and
does no framework work itself — Celery entrypoints in the same shape as
``mlsc/tasks/scheduled.py`` wrap these, matching every other task in the
codebase (design.md, "Dependencies, injected").
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sqlalchemy import select

from mlsc.config import load_theme_relevance_settings
from mlsc.core.fetch.client import FetchClient
from mlsc.db.models import (
    DiscoverySurface,
    Document,
    Enrichment,
    EntityCandidate,
    NoDiscoveryReason,
    RelevanceBasis,
    SourceName,
)
from mlsc.db.models import DiscoveryOutcome as DiscoveryOutcomeRow
from mlsc.llm.router import LlmRouter
from mlsc.pipeline.enrich import Embedder
from mlsc.pipeline.relevance import ThemeRelevanceScorer
from mlsc.pipeline.themes import generate_theme_queries, reference_embeddings_for_basis
from mlsc.repositories.themes import EntityCandidateRepository, ThemeSeedRepository
from mlsc.sources.appstore import AppStoreAdapter, AppStoreCollectionFailed
from mlsc.sources.hackernews import HackerNewsAdapter, HackerNewsCollectionFailed
from mlsc.sources.news.adapter import NewsAdapter, NewsCollectionFailed
from mlsc.sources.news.extract import ArticleExtractor
from mlsc.sources.news.resolve import RedirectResolver
from mlsc.sources.play import PlayAdapter, PlayCollectionFailed

_SURFACE_BY_SOURCE = {
    DiscoverySurface.APP_STORE_SEARCH: "appstore",
    DiscoverySurface.PLAY_SEARCH: "play",
    DiscoverySurface.NEWS_QUERY: "news",
    DiscoverySurface.FEED_DISCOVERY: "hackernews",
}


async def run_query_generation(
    session_factory: async_sessionmaker[AsyncSession], *, monitor_id: uuid.UUID, llm_router: LlmRouter
) -> None:
    """Requirement 2: expand the seed's description into queries, storing
    every one unaccepted so a user reviews before discovery uses any.

    ``QueriesUnusable`` is left uncaught here — the seed keeps its previous
    query set rather than being overwritten with nothing, and the caller
    (a Celery entrypoint) is responsible for surfacing the failure.
    """
    async with session_factory() as session:
        repository = ThemeSeedRepository(session)
        seed = await repository.get_by_monitor(monitor_id)
        description = seed.description

    completion = await generate_theme_queries(llm_router, description)

    async with session_factory() as session:
        repository = ThemeSeedRepository(session)
        await repository.upsert(
            monitor_id,
            description=description,
            queries=[
                {"text": query.text, "rationale": query.rationale, "accepted": False}
                for query in completion.value.queries
            ],
            provenance={
                "provider": completion.provider, "model": completion.model,
                "prompt_version": completion.prompt_version,
            },
        )
        await session.commit()


async def run_discovery(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> DiscoveryOutcomeRow:
    """Requirement 3: search every discovery surface with every accepted
    query, mapping results to candidates with their reason and originating
    query. One surface failing does not stop the others (design.md,
    "Failure strategy": "one discovery surface failing").
    """
    async with session_factory() as session:
        seed_repository = ThemeSeedRepository(session)
        seed = await seed_repository.get_by_monitor(monitor_id)
        queries = [q["text"] for q in seed.queries if q.get("accepted")]

    if not queries:
        return await _record_outcome(
            session_factory, monitor_id, queries_used=[], proposed=0,
            reason=NoDiscoveryReason.NO_QUERIES,
        )

    candidate_repository_rejections: dict[DiscoverySurface, set[str]] = {}
    async with session_factory() as session:
        candidate_repository = EntityCandidateRepository(session)
        for surface, source_name in _SURFACE_BY_SOURCE.items():
            candidate_repository_rejections[surface] = await candidate_repository.rejected_entity_refs(
                monitor_id, SourceName(source_name)
            )

    proposed = 0
    rejected_known = 0
    for surface in DiscoverySurface:
        adapter = _build_adapter(surface, fetch_client, resolver, extractor)
        for query in queries:
            try:
                results = await adapter.discover(query)
            except (
                AppStoreCollectionFailed, PlayCollectionFailed,
                NewsCollectionFailed, HackerNewsCollectionFailed,
            ):
                continue

            for candidate in _map_results(monitor_id, surface, query, results):
                if candidate.entity_ref in candidate_repository_rejections[surface]:
                    rejected_known += 1
                    continue
                async with session_factory() as session:
                    await EntityCandidateRepository(session).upsert_proposed(candidate)
                    await session.commit()
                proposed += 1

    reason = NoDiscoveryReason.EVERY_SURFACE_EMPTY if proposed == 0 else None
    return await _record_outcome(
        session_factory, monitor_id, queries_used=queries, proposed=proposed,
        rejected_known=rejected_known, reason=reason,
    )


def _build_adapter(surface, fetch_client, resolver, extractor):  # noqa: ANN001, ANN201
    if surface is DiscoverySurface.APP_STORE_SEARCH:
        return AppStoreAdapter(fetch_client)
    if surface is DiscoverySurface.PLAY_SEARCH:
        return PlayAdapter(fetch_client)
    if surface is DiscoverySurface.NEWS_QUERY:
        return NewsAdapter(fetch_client, query="", resolver=resolver, extractor=extractor)
    return HackerNewsAdapter(fetch_client, query="")


def _map_results(
    monitor_id: uuid.UUID, surface: DiscoverySurface, query: str, results: list
) -> list[EntityCandidate]:  # noqa: ANN401
    source_name = SourceName(_SURFACE_BY_SOURCE[surface])
    candidates: list[EntityCandidate] = []
    for result in results:
        entity_ref, display_name, reason = _describe(surface, query, result)
        candidates.append(
            EntityCandidate(
                id=uuid.uuid4(), monitor_id=monitor_id, source_name=source_name,
                entity_ref=entity_ref, display_name=display_name, reason=reason,
                proposed_by_query=query, provenance={"surface": surface.value},
            )
        )
    return candidates


def _describe(surface: DiscoverySurface, query: str, result) -> tuple[str, str, str]:  # noqa: ANN401
    if surface is DiscoverySurface.APP_STORE_SEARCH:
        # entity_ref is the numeric App Store id, matching what
        # _validate_appstore_config requires and what AppStoreAdapter.fetch
        # uses as its ``entity`` — not the bundle id, which is only a
        # display detail here.
        return (
            result.app_id, f"{result.title} ({result.bundle_id})",
            f"matched query {query!r} in the App Store catalog search",
        )
    if surface is DiscoverySurface.PLAY_SEARCH:
        return (
            result.app_id, result.title,
            f"matched query {query!r} in the Play Store catalog search",
        )
    if surface is DiscoverySurface.NEWS_QUERY:
        titles = "; ".join(result.matched_titles[:3])
        return (query, query, f"query {query!r} surfaces news coverage: {titles}")
    titles = "; ".join(result.matched_titles[:3])
    return (query, query, f"query {query!r} surfaces Hacker News discussion: {titles}")


async def build_theme_relevance_context(
    session_factory: async_sessionmaker[AsyncSession], *, monitor_id: uuid.UUID, embedder: Embedder
):
    """Requirement 7: build the ``ThemeRelevanceContext`` the enrichment
    task's ``RELEVANCE`` stage needs for this theme monitor, for whichever
    basis is configured.

    A late import inside ``mlsc.tasks.enrich`` would create a cycle
    (``enrich`` -> ``themes`` -> ``enrich``), so the return type is left
    unannotated here; the caller in ``mlsc/tasks/dispatch.py`` constructs
    the actual ``ThemeRelevanceContext``.
    """
    from mlsc.tasks.enrich import ThemeRelevanceContext

    settings = load_theme_relevance_settings()
    async with session_factory() as session:
        seed = await ThemeSeedRepository(session).get_by_monitor(monitor_id)
        accepted_queries = [q["text"] for q in seed.queries if q.get("accepted")]

        corpus_embeddings: list[list[float]] = []
        if settings.basis == "corpus_centroid":
            result = await session.execute(
                select(Enrichment.embedding)
                .join(Document, Document.id == Enrichment.document_id)
                .where(Document.monitor_id == monitor_id, Enrichment.embedding.is_not(None))
            )
            corpus_embeddings = [row[0] for row in result.all()]

    reference_embeddings = reference_embeddings_for_basis(
        settings.basis, description=seed.description, queries=accepted_queries,
        corpus_embeddings=corpus_embeddings, embedder=embedder,
    )
    return ThemeRelevanceContext(
        ThemeRelevanceScorer(threshold=settings.threshold),
        reference_embeddings=reference_embeddings,
        basis=RelevanceBasis(settings.basis),
    )


async def _record_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    monitor_id: uuid.UUID,
    *,
    queries_used: list[str],
    proposed: int,
    rejected_known: int = 0,
    reason: NoDiscoveryReason | None,
) -> DiscoveryOutcomeRow:
    async with session_factory() as session:
        outcome = DiscoveryOutcomeRow(
            id=uuid.uuid4(), monitor_id=monitor_id, queries_used=queries_used,
            proposed=proposed, rejected_known=rejected_known, reason=reason,
        )
        session.add(outcome)
        await session.commit()
        return outcome
