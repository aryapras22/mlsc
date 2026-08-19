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
) -> list[Any]:
    """Return the validated item list, or raise a named failure.

    ``body`` is decoded only after the content type matches, so an HTML error
    page from a source declaring JSON is reported as ``UnexpectedContentType``
    rather than a JSON decode error.
    """
    _check_content_type(content_type, expectations)
    decoded = _decode_body(body, expectations)
    items = _walk_item_path(decoded, expectations)
    if expectations.body_format == "json":
        _check_required_fields(items, expectations)
    _check_row_count_floor(items, expectations)
    return items


def _decode_body(body: bytes, expectations: FetchExpectations) -> Any:
    if expectations.body_format == "google_batchexecute":
        return _decode_google_batchexecute(body)
    return _decode_json(body)


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise MissingRequiredFields(missing=("<unparseable body>",)) from None


_BATCHEXECUTE_XSSI_PREFIX = b")]}'"


def _decode_google_batchexecute(body: bytes) -> Any:
    """Strip Google's XSSI prefix, then unwrap the doubly JSON-encoded envelope.

    The response is ``)]}'\\n\\n`` followed by a JSON array whose relevant
    element is itself a JSON-encoded string carrying the real payload, rather
    than a nested JSON value. Both decode steps can fail independently, and
    either failure means the same thing to a caller: the body did not match
    what was expected.
    """
    text = body.lstrip()
    if text.startswith(_BATCHEXECUTE_XSSI_PREFIX):
        text = text[len(_BATCHEXECUTE_XSSI_PREFIX) :]
    outer = _decode_json(text)
    if not isinstance(outer, list) or not outer or not isinstance(outer[0], list):
        raise MissingRequiredFields(missing=("<unparseable batchexecute envelope>",))
    inner_row = outer[0]
    if len(inner_row) < 3 or not isinstance(inner_row[2], str):
        raise MissingRequiredFields(missing=("<unparseable batchexecute envelope>",))
    return _decode_json(inner_row[2])


def _check_content_type(content_type: str, expectations: FetchExpectations) -> None:
    actual = content_type.split(";", 1)[0].strip().lower()
    expected = expectations.content_type.strip().lower()
    if actual != expected:
        raise UnexpectedContentType(expected=expected, actual=actual)


def _walk_item_path(body: Any, expectations: FetchExpectations) -> list[Any]:
    current = body
    for key in expectations.item_path:
        if isinstance(key, int):
            if not isinstance(current, list) or not (-len(current) <= key < len(current)):
                raise MissingRequiredFields(missing=(str(key),))
            current = current[key]
        else:
            if not isinstance(current, dict) or key not in current:
                raise MissingRequiredFields(missing=(key,))
            current = current[key]
    if not isinstance(current, list):
        raise MissingRequiredFields(
            missing=tuple(str(k) for k in expectations.item_path) or ("<root>",)
        )
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
