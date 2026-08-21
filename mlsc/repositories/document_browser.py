"""Keyset-paginated document reads with drill-down filters.

Cursor pagination rather than offset: the client passes an opaque position
(the last row's ``published_at`` and ``id``) rather than a row count, so the
page stays correct when documents are inserted between requests — which
happens continuously here (learn.md, "Cursor pagination").
"""

from __future__ import annotations

import base64
import uuid
from datetime import date, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Assignment, Document, SourceName


class CursorInvalid(ValueError):
    """The opaque cursor a client supplied does not decode to a known position."""


def encode_cursor(published_at: datetime, document_id: uuid.UUID) -> str:
    raw = f"{published_at.isoformat()}|{document_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        published_at_raw, document_id_raw = raw.split("|", 1)
        return datetime.fromisoformat(published_at_raw), uuid.UUID(document_id_raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise CursorInvalid(f"cursor {cursor!r} is not valid") from error


async def list_documents(
    session: AsyncSession,
    *,
    monitor_id: uuid.UUID,
    topic_id: uuid.UUID | None,
    source: SourceName | None,
    start: date | None,
    end: date | None,
    cursor: str | None,
    limit: int,
) -> tuple[list[tuple[Document, uuid.UUID | None]], str | None]:
    """Returns up to ``limit`` documents newest-first plus the next page's
    cursor, or ``None`` when this page is the last one."""
    query = (
        select(Document, Assignment.topic_id)
        .outerjoin(Assignment, Assignment.document_id == Document.id)
        .where(Document.monitor_id == monitor_id)
    )
    if topic_id is not None:
        query = query.where(Assignment.topic_id == topic_id)
    if source is not None:
        query = query.where(Document.source_name == source)
    if start is not None:
        query = query.where(Document.published_at >= start)
    if end is not None:
        query = query.where(Document.published_at <= end)
    if cursor is not None:
        cursor_published_at, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                Document.published_at < cursor_published_at,
                and_(Document.published_at == cursor_published_at, Document.id < cursor_id),
            )
        )

    query = query.order_by(Document.published_at.desc(), Document.id.desc()).limit(limit + 1)
    result = await session.execute(query)
    rows = result.all()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1][0].published_at, page[-1][0].id) if has_more else None
    return page, next_cursor
