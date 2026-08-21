"""Label ingestion: the strictest validation boundary in the system.

Requiring an external reference on every event label and rejecting a
document label whose provenance is the system's own output is what makes
this an evaluation rather than a tautology (design.md, "Trust boundary").
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import Document, DocumentLabel, EventLabel, LabelSet, Purpose
from mlsc.repositories.evaluation import DocumentLabelRepository, EventLabelRepository, LabelSetRepository

_SYSTEM_PROVENANCE_MARKERS = ("pipeline", "system", "auto", "model")


class LabelContaminated(ValueError):
    """A label's provenance traces back to the system's own output rather
    than an independent source (design.md, "Named failures")."""


class DocumentNotFound(KeyError):
    def __init__(self, document_id: uuid.UUID) -> None:
        super().__init__(str(document_id))
        self.document_id = document_id


class LabelIngestionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_label_set(self, monitor_id: uuid.UUID, *, purpose: Purpose, notes: str | None) -> uuid.UUID:
        async with self._session_factory() as session:
            label_set = LabelSet(id=uuid.uuid4(), monitor_id=monitor_id, purpose=purpose, notes=notes)
            LabelSetRepository(session).insert(label_set)
            await session.commit()
            return label_set.id

    async def add_document_label(
        self, label_set_id: uuid.UUID, *, document_id: uuid.UUID, is_relevant: bool, labelled_by: str
    ) -> uuid.UUID:
        """Requirement 2: a human rater's direct judgement on a real
        document. ``labelled_by`` naming a system component is rejected —
        a relevance label derived from the pipeline's own relevance score
        would measure the pipeline against itself.
        """
        if not labelled_by.strip():
            raise LabelContaminated("labelled_by must name a human rater")
        if any(marker in labelled_by.lower() for marker in _SYSTEM_PROVENANCE_MARKERS):
            raise LabelContaminated(f"labelled_by {labelled_by!r} names a system component, not a human rater")

        async with self._session_factory() as session:
            document = await session.get(Document, document_id)
            if document is None:
                raise DocumentNotFound(document_id)

            label = DocumentLabel(
                id=uuid.uuid4(), label_set_id=label_set_id, document_id=document_id,
                is_relevant=is_relevant, labelled_by=labelled_by,
            )
            DocumentLabelRepository(session).insert(label)
            await session.commit()
            return label.id

    async def add_event_label(
        self,
        label_set_id: uuid.UUID,
        *,
        monitor_id: uuid.UUID,
        occurred_on: date,
        kind: str,
        description: str,
        external_reference: str,
    ) -> uuid.UUID:
        """Requirement 4: every event label must carry a non-empty external
        reference — a public changelog, news article, or forum
        announcement about the monitored entity. A reference pointing back
        into this system (a trend event id, an insight id) is exactly the
        contamination ``LabelContaminated`` exists to catch."""
        if not external_reference.strip():
            raise LabelContaminated("event labels must carry a non-empty external reference")
        if _references_own_output(external_reference):
            raise LabelContaminated(
                f"external_reference {external_reference!r} points back into this system's own output"
            )

        async with self._session_factory() as session:
            label = EventLabel(
                id=uuid.uuid4(), label_set_id=label_set_id, monitor_id=monitor_id,
                occurred_on=occurred_on, kind=kind, description=description,
                external_reference=external_reference,
            )
            EventLabelRepository(session).insert(label)
            await session.commit()
            return label.id


def _references_own_output(external_reference: str) -> bool:
    lowered = external_reference.lower()
    return any(
        marker in lowered
        for marker in ("trend_events/", "insights/", "generation_skips/", "/monitors/")
    )
