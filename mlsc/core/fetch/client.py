"""Composes breaker, cache, throttle, transport, and validator into one fetch call.

This is the one shared component every adapter must go through (requirement 1);
there is no way to reach a transport except through ``FetchClient.get``.

``get`` always returns a ``FetchOutcome`` — it never raises a guardrail failure.
The outcome's ``status`` says what happened and its ``payload`` carries either
the validated items (``OK``) or the named failure that explains why not. This is
what lets a validation failure or an open breaker be reported rather than
silently becoming an empty result (requirement 4, 5, 8).
"""

from __future__ import annotations

import asyncio
import time

from mlsc.core.fetch.breaker import Breaker
from mlsc.core.fetch.cache import ResponseCache
from mlsc.core.fetch.contracts import (
    BreakerOpen,
    FetchExpectations,
    FetchGuardrailError,
    FetchOutcome,
    FetchRequest,
    FetchStatus,
    TransportFailure,
)
from mlsc.core.fetch.throttle import HostBudget, Throttle
from mlsc.core.fetch.transports import Transport
from mlsc.core.fetch.validate import validate


class FetchClient:
    """The single entry point for every outbound source request."""

    def __init__(
        self,
        *,
        breaker: Breaker,
        cache: ResponseCache,
        throttle: Throttle,
        plain_transport: Transport,
        impersonating_transport: Transport,
        host_budget: HostBudget,
        max_transport_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._breaker = breaker
        self._cache = cache
        self._throttle = throttle
        self._plain_transport = plain_transport
        self._impersonating_transport = impersonating_transport
        self._host_budget = host_budget
        self._max_transport_retries = max_transport_retries
        self._retry_backoff_seconds = retry_backoff_seconds

    async def get(self, request: FetchRequest, expectations: FetchExpectations) -> FetchOutcome:
        try:
            await self._breaker.check(request.host_key)
        except BreakerOpen as open_breaker:
            return _outcome(FetchStatus.SKIPPED_BREAKER_OPEN, open_breaker)

        cached = await self._cache.lookup(request)
        if cached is not None:
            return cached

        await self._throttle.acquire(request.host_key, self._host_budget)

        transport = (
            self._plain_transport
            if request.client_profile.value == "plain"
            else self._impersonating_transport
        )

        started_at = time.monotonic()
        try:
            response = await self._send_with_retry(transport, request)
        except TransportFailure as failure:
            return _outcome(FetchStatus.TRANSPORT_FAILED, failure)
        duration = time.monotonic() - started_at

        try:
            items = validate(
                content_type=response.content_type,
                body=response.body,
                expectations=expectations,
            )
        except FetchGuardrailError as failure:
            await self._breaker.record_success(request.host_key)
            return _outcome(
                FetchStatus.VALIDATION_FAILED,
                failure,
                library_version=response.library_version,
                duration_seconds=duration,
            )

        await self._breaker.record_success(request.host_key)

        outcome = FetchOutcome(
            status=FetchStatus.OK,
            payload=items,
            served_from_cache=False,
            library_version=response.library_version,
            duration_seconds=duration,
        )
        await self._cache.store(request, outcome)
        return outcome

    async def _send_with_retry(self, transport: Transport, request: FetchRequest):
        attempt = 0
        while True:
            try:
                return await transport.send(request)
            except TransportFailure:
                await self._breaker.record_failure(request.host_key)
                attempt += 1
                if attempt > self._max_transport_retries:
                    raise
                await asyncio.sleep(self._retry_backoff_seconds * (2 ** (attempt - 1)))


def _outcome(
    status: FetchStatus,
    failure: FetchGuardrailError,
    *,
    library_version: str = "",
    duration_seconds: float = 0.0,
) -> FetchOutcome:
    return FetchOutcome(
        status=status,
        payload=failure,
        served_from_cache=False,
        library_version=library_version,
        duration_seconds=duration_seconds,
    )
