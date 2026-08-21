"""The document browser: search and drill-down filters, keyset-paginated.

Every figure in this API drills down to here — the raw documents behind any
number a chart shows (requirement 1).
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request, status

from mlsc.api.scoping import Scoping
from mlsc.db.models import SourceName
from mlsc.repositories.document_browser import CursorInvalid, list_documents
from mlsc.repositories.monitors import MonitorNotFound
from mlsc.schemas.metrics import DocumentPage, DocumentView

router = APIRouter(prefix="/monitors/{monitor_id}/documents", tags=["documents"])

_DEFAULT_PAGE_SIZE = 50


@router.get("", response_model=DocumentPage)
async def browse_documents(
    monitor_id: uuid.UUID,
    request: Request,
    topic_id: uuid.UUID | None = Query(default=None),
    source: SourceName | None = Query(default=None),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, gt=0, le=200),
) -> DocumentPage:
    async with request.app.state.startup.session_factory() as session:
        try:
            await Scoping(session).resolve(monitor_id)
        except MonitorNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None

        try:
            rows, next_cursor = await list_documents(
                session, monitor_id=monitor_id, topic_id=topic_id, source=source,
                start=start, end=end, cursor=cursor, limit=limit,
            )
        except CursorInvalid as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from None

        items = [
            DocumentView(
                id=document.id, source_name=document.source_name, url=document.url,
                body=document.body, published_at=document.published_at,
                rating=document.rating, topic_id=topic_id_for_row,
            )
            for document, topic_id_for_row in rows
        ]
        return DocumentPage(items=items, cursor=next_cursor, total_known=None)
