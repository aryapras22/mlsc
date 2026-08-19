"""Payload validation against an adapter's declared expectations.

The single trust boundary for response bodies: everything downstream trusts that
a payload reaching it matched its declared expectations, and re-checks nothing.
Checks run content type, then item path, then required fields, then the
healthy-row floor, in that order — each named failure means later checks
never ran, so a caller need not distinguish "which of several things is wrong".
"""

from __future__ import annotations

import json
from typing import Any

from mlsc.core.fetch.contracts import (
    FetchExpectations,
    IllegitimatelyEmpty,
    MissingRequiredFields,
    UnexpectedContentType,
)


def validate(
    *, content_type: str, body: bytes, expectations: FetchExpectations
) -> list[dict[str, Any]]:
    """Return the validated item list, or raise a named failure.

    ``body`` is decoded only after the content type matches, so an HTML error
    page from a source declaring JSON is reported as ``UnexpectedContentType``
    rather than a JSON decode error.
    """
    _check_content_type(content_type, expectations)
    decoded = _decode_json(body)
    items = _walk_item_path(decoded, expectations)
    _check_required_fields(items, expectations)
    _check_row_count_floor(items, expectations)
    return items


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise MissingRequiredFields(missing=("<unparseable body>",)) from None


def _check_content_type(content_type: str, expectations: FetchExpectations) -> None:
    actual = content_type.split(";", 1)[0].strip().lower()
    expected = expectations.content_type.strip().lower()
    if actual != expected:
        raise UnexpectedContentType(expected=expected, actual=actual)


def _walk_item_path(body: Any, expectations: FetchExpectations) -> list[dict[str, Any]]:
    current = body
    for key in expectations.item_path:
        if not isinstance(current, dict) or key not in current:
            raise MissingRequiredFields(missing=(key,))
        current = current[key]
    if not isinstance(current, list):
        raise MissingRequiredFields(missing=expectations.item_path or ("<root>",))
    return current


def _check_required_fields(
    items: list[dict[str, Any]], expectations: FetchExpectations
) -> None:
    if not expectations.required_fields:
        return
    missing: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            missing.update(expectations.required_fields)
            continue
        missing.update(field for field in expectations.required_fields if field not in item)
    if missing:
        raise MissingRequiredFields(missing=tuple(sorted(missing)))


def _check_row_count_floor(
    items: list[dict[str, Any]], expectations: FetchExpectations
) -> None:
    if len(items) == 0 and expectations.min_rows_when_healthy >= 1:
        raise IllegitimatelyEmpty(min_rows_when_healthy=expectations.min_rows_when_healthy)
