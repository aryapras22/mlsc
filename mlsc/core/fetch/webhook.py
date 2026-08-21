"""Sends a webhook delivery through the same breaker and throttle every
outbound request in this codebase goes through (design.md, "Dependencies,
injected": "the same shared client `fetch-guardrails` provides").

Not routed through ``FetchClient.get`` itself: that call is shaped around
parsing an expected item list out of a GET response and caching it, and a
webhook POST is neither idempotent nor a shape ``validate()`` should judge.
Reusing the breaker and throttle primitives directly gives a webhook target
the same politeness and circuit-breaking without stretching a scrape-shaped
abstraction to fit a delivery.
"""

from __future__ import annotations

import json

from mlsc.core.fetch.breaker import Breaker
from mlsc.core.fetch.contracts import BreakerOpen, ClientProfile, FetchRequest, TransportFailure
from mlsc.core.fetch.throttle import HostBudget, Throttle
from mlsc.core.fetch.transports import Transport


class WebhookUnreachable(RuntimeError):
    """The user-supplied endpoint failed — breaker open, transport failure,
    or a non-2xx response."""


class WebhookSender:
    def __init__(
        self, *, transport: Transport, breaker: Breaker, throttle: Throttle, host_budget: HostBudget
    ) -> None:
        self._transport = transport
        self._breaker = breaker
        self._throttle = throttle
        self._host_budget = host_budget

    async def send(self, url: str, payload: dict) -> None:
        host_key = url.split("/")[2] if "//" in url else url
        try:
            await self._breaker.check(host_key)
        except BreakerOpen as error:
            raise WebhookUnreachable(str(error)) from error

        await self._throttle.acquire(host_key, self._host_budget)

        request = FetchRequest(
            url=url, host_key=host_key, client_profile=ClientProfile.PLAIN, method="POST",
            headers=(("content-type", "application/json"),), body=json.dumps(payload).encode("utf-8"),
        )
        try:
            response = await self._transport.send(request)
        except TransportFailure as error:
            await self._breaker.record_failure(host_key)
            raise WebhookUnreachable(str(error)) from error

        if response.status_code >= 400:
            await self._breaker.record_failure(host_key)
            raise WebhookUnreachable(f"webhook {url} returned status {response.status_code}")

        await self._breaker.record_success(host_key)
