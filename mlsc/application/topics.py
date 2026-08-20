"""User overrides on the topic registry, validated at the boundary.

Every operation here is a correction a human makes and every later automated
pass must respect (requirement 9). A manual assignment carries
``AssignmentMethod.MANUAL``, which is what ``refit_registry`` excludes from
its trailing window and what ``assign_topics`` never revisits since the
document already has an ``Assignment`` row — the respect is structural, not
a flag this module has to remember to check.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import (
    Assignment,
    AssignmentMethod,
    Document,
    LineageEvent,
    SplitProposal,
    SplitProposalStatus,
    TopicStatus,
)
from mlsc.repositories.topics import LineageRepository, TopicNotFound, TopicRepository


class DocumentNotFound(KeyError):
    def __init__(self, document_id: uuid.UUID) -> None:
        super().__init__(str(document_id))
        self.document_id = document_id


class SplitProposalNotFound(KeyError):
    def __init__(self, proposal_id: uuid.UUID) -> None:
        super().__init__(str(proposal_id))
        self.proposal_id = proposal_id


class MergeTargetInactive(RuntimeError):
    """A merge target must exist and be active (design.md, "Trust boundary")."""


class TopicService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def rename(self, topic_id: uuid.UUID, label: str) -> None:
        label = label.strip()
        if not label:
            raise ValueError("label must not be blank")
        async with self._session_factory() as session:
            topic = await TopicRepository(session).get(topic_id)
            topic.label = label
            topic.label_is_provisional = False
            await session.commit()

    async def set_pinned(self, topic_id: uuid.UUID, pinned: bool) -> None:
        async with self._session_factory() as session:
            topic = await TopicRepository(session).get(topic_id)
            topic.is_pinned = pinned
            await session.commit()

    async def merge(
        self, from_topic_id: uuid.UUID, into_topic_id: uuid.UUID, *, reason: str | None = None
    ) -> None:
        """A manual merge: every assignment on ``from_topic_id`` moves to
        ``into_topic_id``, its status becomes ``merged``, and both
        identifiers stay resolvable through the lineage row (requirement 5).
        """
        if from_topic_id == into_topic_id:
            raise ValueError("a topic cannot merge into itself")

        async with self._session_factory() as session:
            topics = TopicRepository(session)
            from_topic = await topics.get(from_topic_id)
            into_topic = await topics.get(into_topic_id)
            if into_topic.status is not TopicStatus.ACTIVE:
                raise MergeTargetInactive(str(into_topic_id))

            await session.execute(
                Assignment.__table__.update()
                .where(Assignment.topic_id == from_topic_id)
                .values(topic_id=into_topic_id)
            )
            from_topic.status = TopicStatus.MERGED
            from_topic.merged_into = into_topic_id

            LineageRepository(session).write(
                from_topic=from_topic_id, to_topic=into_topic_id, event=LineageEvent.MERGE, reason=reason
            )
            await session.commit()

    async def reject_split_proposal(self, proposal_id: uuid.UUID) -> None:
        """Dismiss a proposed split without performing it — a split is never
        auto-applied (requirement 8); this only closes the artefact."""
        async with self._session_factory() as session:
            proposal = await session.get(SplitProposal, proposal_id)
            if proposal is None:
                raise SplitProposalNotFound(proposal_id)
            proposal.status = SplitProposalStatus.DISMISSED
            await session.commit()

    async def reassign(
        self, document_id: uuid.UUID, topic_id: uuid.UUID, *, today: date | None = None
    ) -> None:
        """A manual assignment. Written with ``AssignmentMethod.MANUAL`` so no
        later automated pass reassigns this document (requirement 9)."""
        today = today or date.today()
        async with self._session_factory() as session:
            document = await session.get(Document, document_id)
            if document is None:
                raise DocumentNotFound(document_id)

            topics = TopicRepository(session)
            topic = await topics.get(topic_id)

            result = await session.execute(
                select(Assignment).where(Assignment.document_id == document_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                await session.delete(existing)
                await session.flush()

            session.add(
                Assignment(
                    id=uuid.uuid4(),
                    document_id=document_id,
                    topic_id=topic_id,
                    similarity=1.0,
                    method=AssignmentMethod.MANUAL,
                )
            )
            topics.touch_last_seen(topic, today=today)
            await session.commit()
