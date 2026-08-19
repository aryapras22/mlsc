"""Fetch domain shapes: requests, outcomes, expectations, and named failures.

An adapter declares ``FetchExpectations`` as data, not a method, so what a healthy
response looks like can be inspected and tested without running a fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class ClientProfile(Enum):
    """Which transport a request uses. Selected per host, never per call site."""

    PLAIN = "plain"
    IMPERSONATING = "impersonating"


class FetchStatus(Enum):
    """The only outcomes a fetch can report.

    There is no ``empty_ok`` member. An empty result is either legitimate for that
    source, in which case it is ``OK`` with zero items, or it is not, in which case
    it is ``VALIDATION_FAILED``. The adapter's expectations decide which.
    """

    OK = "ok"
    VALIDATION_FAILED = "validation_failed"
    TRANSPORT_FAILED = "transport_failed"
    SKIPPED_BREAKER_OPEN = "skipped_breaker_open"


@dataclass(frozen=True)
class FetchExpectations:
    """What a healthy response looks like, declared by the adapter as a value.

    ``item_path`` is the sequence of keys/indices to walk from the parsed body to
    the list of items, e.g. ``("data", "items")``. An empty tuple means the body
    itself is the item list.

    ``body_format`` selects how the raw body is decoded before ``item_path`` is
    walked. ``"json"`` is a plain JSON document. ``"google_batchexecute"`` is
    Google's RPC envelope — a ``)]}'`` XSSI prefix followed by a JSON array whose
    third element is itself a JSON-encoded string carrying the real payload.
    Google Play's review endpoint uses this format; nothing else needs it yet.
    Required-field checking does not apply to this format because its items are
    positional arrays, not objects with named keys.
    """

    content_type: str
    item_path: tuple[str | int, ...] = ()
    required_fields: tuple[str, ...] = ()
    min_rows_when_healthy: int = 0
    body_format: Literal["json", "google_batchexecute"] = "json"


@dataclass(frozen=True)
class FetchRequest:
    """One request. Identical requests within a collection window share a cache entry.

    ``body`` is for POST requests whose payload is not query parameters, such as
    Google Play's review RPC, which POSTs a form-encoded ``f.req=...`` body.
    """

    url: str
    host_key: str
    client_profile: ClientProfile
    method: str = "GET"
    query: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    collection_window: str = ""


@dataclass(frozen=True)
class FetchOutcome:
    """What the caller needs to record: status, payload, and provenance."""

    status: FetchStatus
    payload: Any
    served_from_cache: bool
    library_version: str
    duration_seconds: float


class FetchGuardrailError(RuntimeError):
    """Base for every named failure a fetch can raise."""


@dataclass
class UnexpectedContentType(FetchGuardrailError):
    expected: str
    actual: str

    def __str__(self) -> str:
        return f"expected content type {self.expected!r}, got {self.actual!r}"


@dataclass
class MissingRequiredFields(FetchGuardrailError):
    missing: tuple[str, ...]

    def __str__(self) -> str:
        return f"response items are missing required fields: {', '.join(self.missing)}"


@dataclass
class IllegitimatelyEmpty(FetchGuardrailError):
    """Zero items from a source declaring ``min_rows_when_healthy >= 1``.

    This is the load-bearing failure type: it exists so the condition has a name
    a caller must handle, rather than being representable as a successful empty list.
    """

    min_rows_when_healthy: int

    def __str__(self) -> str:
        return (
            f"response had zero items but the source declares at least "
            f"{self.min_rows_when_healthy} when healthy"
        )


@dataclass
class BreakerOpen(FetchGuardrailError):
    host_key: str
    opened_until: float

    def __str__(self) -> str:
        return f"{self.host_key} is cooling until {self.opened_until}"


@dataclass
class TransportFailure(FetchGuardrailError):
    host_key: str
    cause: BaseException = field(repr=False)

    def __str__(self) -> str:
        return f"transport failure against {self.host_key}: {type(self.cause).__name__}"
