"""Unit tests for the shared fetch component.

Requirements: 2, 3, 4, 5, 6.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from mlsc.core.fetch.breaker import Breaker, BreakerSettings
from mlsc.core.fetch.cache import ResponseCache
from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.contracts import (
    BreakerOpen,
    ClientProfile,
    FetchExpectations,
    FetchRequest,
    FetchStatus,
    IllegitimatelyEmpty,
    MissingRequiredFields,
    TransportFailure,
    UnexpectedContentType,
)
from mlsc.core.fetch.throttle import HostBudget, Throttle
from mlsc.core.fetch.transports import TransportResponse
from mlsc.core.fetch.validate import validate

HOST = "example.test"


class FakeRedis:
    """In-memory stand-in for the subset of the Redis API this feature uses."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self._store[key] = value if isinstance(value, str) else str(value)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._store.pop(key, None)

    async def incr(self, key: str) -> int:
        value = int(self._store.get(key, 0)) + 1
        self._store[key] = str(value)
        return value


class FrozenClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FixedRandom:
    def __init__(self, value: float) -> None:
        self.value = value

    def uniform(self, low: float, high: float) -> float:
        return self.value


class FakeTransport:
    def __init__(self, responses: list[TransportResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def send(self, request: FetchRequest) -> TransportResponse:
        self.calls += 1
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def json_response(body: Any, *, content_type: str = "application/json") -> TransportResponse:
    return TransportResponse(
        status_code=200,
        content_type=content_type,
        body=json.dumps(body).encode("utf-8"),
        library_version="fake/1.0",
    )


def request(*, profile: ClientProfile = ClientProfile.PLAIN) -> FetchRequest:
    return FetchRequest(url="https://example.test/items", host_key=HOST, client_profile=profile)


def expectations(**overrides: Any) -> FetchExpectations:
    base = dict(content_type="application/json", item_path=("items",), required_fields=("id",))
    base.update(overrides)
    return FetchExpectations(**base)


def build_client(
    *,
    transport: FakeTransport,
    clock: FrozenClock | None = None,
    breaker_settings: BreakerSettings | None = None,
) -> tuple[FetchClient, FakeRedis]:
    redis = FakeRedis()
    clock = clock or FrozenClock()
    throttle = Throttle(
        redis,
        clock=clock,
        random_source=FixedRandom(0.0),
    )
    breaker = Breaker(
        redis, breaker_settings or BreakerSettings(failure_threshold=3, cooldown_seconds=60.0), clock=clock
    )
    cache = ResponseCache(redis, ttl_seconds=3600)
    budget = HostBudget(
        capacity=100.0, refill_rate_per_second=1000.0, jitter_low_seconds=0.0, jitter_high_seconds=0.0
    )
    client = FetchClient(
        breaker=breaker,
        cache=cache,
        throttle=throttle,
        plain_transport=transport,
        impersonating_transport=transport,
        host_budget=budget,
        max_transport_retries=1,
        retry_backoff_seconds=0.0,
    )
    return client, redis


def as_body(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


class TestValidator:
    def test_wrong_content_type_is_reported(self) -> None:
        with pytest.raises(UnexpectedContentType):
            validate(content_type="text/html", body=b"<html/>", expectations=expectations())

    def test_missing_item_path_is_reported(self) -> None:
        with pytest.raises(MissingRequiredFields):
            validate(content_type="application/json", body=as_body({}), expectations=expectations())

    def test_item_missing_required_field_is_reported(self) -> None:
        body = as_body({"items": [{"id": "1"}, {}]})
        with pytest.raises(MissingRequiredFields):
            validate(content_type="application/json", body=body, expectations=expectations())

    def test_illegitimately_empty_is_reported(self) -> None:
        body = as_body({"items": []})
        with pytest.raises(IllegitimatelyEmpty):
            validate(
                content_type="application/json",
                body=body,
                expectations=expectations(min_rows_when_healthy=1),
            )

    def test_legitimately_empty_source_returns_empty_list(self) -> None:
        body = as_body({"items": []})
        items = validate(
            content_type="application/json",
            body=body,
            expectations=expectations(min_rows_when_healthy=0),
        )
        assert items == []

    def test_healthy_response_returns_items(self) -> None:
        body = as_body({"items": [{"id": "1"}, {"id": "2"}]})
        items = validate(content_type="application/json", body=body, expectations=expectations())
        assert items == [{"id": "1"}, {"id": "2"}]


def batchexecute_body(inner_payload: Any) -> bytes:
    import json as _json

    inner = _json.dumps(inner_payload)
    outer = _json.dumps([["wrb.fr", "oCPfdb", inner]])
    return b")]}'\n\n" + outer.encode()


class TestGoogleBatchexecuteFormat:
    def test_unwraps_the_envelope_and_returns_the_inner_list(self) -> None:
        body = batchexecute_body([[["id-1"], ["id-2"]]])
        expectations = FetchExpectations(
            content_type="application/json", item_path=(0,), body_format="google_batchexecute"
        )

        items = validate(content_type="application/json", body=body, expectations=expectations)

        assert items == [["id-1"], ["id-2"]]

    def test_empty_inner_list_is_illegitimately_empty(self) -> None:
        body = batchexecute_body([[]])
        expectations = FetchExpectations(
            content_type="application/json",
            item_path=(0,),
            min_rows_when_healthy=1,
            body_format="google_batchexecute",
        )

        with pytest.raises(IllegitimatelyEmpty):
            validate(content_type="application/json", body=body, expectations=expectations)

    def test_missing_xssi_prefix_and_garbage_body_is_reported(self) -> None:
        expectations = FetchExpectations(
            content_type="application/json", item_path=(0,), body_format="google_batchexecute"
        )

        with pytest.raises(MissingRequiredFields):
            validate(
                content_type="application/json",
                body=b"not the expected envelope at all",
                expectations=expectations,
            )

    def test_wrong_content_type_is_still_checked_first(self) -> None:
        expectations = FetchExpectations(
            content_type="application/json", item_path=(0,), body_format="google_batchexecute"
        )

        with pytest.raises(UnexpectedContentType):
            validate(content_type="text/html", body=b"<html/>", expectations=expectations)


class TestThrottle:
    def test_spaces_requests_by_refill_rate_under_a_frozen_clock(self) -> None:
        redis = FakeRedis()
        clock = FrozenClock()
        throttle = Throttle(redis, clock=clock, random_source=FixedRandom(0.0))
        budget = HostBudget(
            capacity=1.0, refill_rate_per_second=2.0, jitter_low_seconds=0.0, jitter_high_seconds=0.0
        )

        asyncio.run(throttle.acquire(HOST, budget))
        asyncio.run(throttle.acquire(HOST, budget))

        assert clock.slept == pytest.approx([0.5])

    def test_independent_hosts_do_not_share_a_budget(self) -> None:
        redis = FakeRedis()
        clock = FrozenClock()
        throttle = Throttle(redis, clock=clock, random_source=FixedRandom(0.0))
        budget = HostBudget(
            capacity=1.0, refill_rate_per_second=1.0, jitter_low_seconds=0.0, jitter_high_seconds=0.0
        )

        asyncio.run(throttle.acquire("host-a", budget))
        asyncio.run(throttle.acquire("host-b", budget))

        assert clock.slept == []

    def test_jitter_adds_a_gap_from_the_injected_random_source(self) -> None:
        redis = FakeRedis()
        clock = FrozenClock()
        throttle = Throttle(redis, clock=clock, random_source=FixedRandom(0.75))
        budget = HostBudget(
            capacity=5.0, refill_rate_per_second=1.0, jitter_low_seconds=0.5, jitter_high_seconds=1.0
        )

        asyncio.run(throttle.acquire(HOST, budget))

        assert clock.slept == [0.75]


class TestBreaker:
    def test_opens_after_the_failure_threshold_and_reports_skipped(self) -> None:
        redis = FakeRedis()
        clock = FrozenClock()
        breaker = Breaker(redis, BreakerSettings(failure_threshold=2, cooldown_seconds=30.0), clock=clock)

        asyncio.run(breaker.record_failure(HOST))
        asyncio.run(breaker.record_failure(HOST))

        with pytest.raises(BreakerOpen):
            asyncio.run(breaker.check(HOST))

    def test_half_open_after_cooldown_elapses(self) -> None:
        redis = FakeRedis()
        clock = FrozenClock()
        breaker = Breaker(redis, BreakerSettings(failure_threshold=1, cooldown_seconds=10.0), clock=clock)

        asyncio.run(breaker.record_failure(HOST))
        with pytest.raises(BreakerOpen):
            asyncio.run(breaker.check(HOST))

        clock.now += 10.0
        asyncio.run(breaker.check(HOST))

    def test_success_clears_the_failure_count(self) -> None:
        redis = FakeRedis()
        clock = FrozenClock()
        breaker = Breaker(redis, BreakerSettings(failure_threshold=2, cooldown_seconds=10.0), clock=clock)

        asyncio.run(breaker.record_failure(HOST))
        asyncio.run(breaker.record_success(HOST))
        asyncio.run(breaker.record_failure(HOST))

        asyncio.run(breaker.check(HOST))


class TestCache:
    def test_hit_avoids_a_second_request(self) -> None:
        transport = FakeTransport([json_response({"items": [{"id": "1"}]})])
        client, _ = build_client(transport=transport)
        req = request()

        first = asyncio.run(client.get(req, expectations()))
        second = asyncio.run(client.get(req, expectations()))

        assert first.status is FetchStatus.OK
        assert second.status is FetchStatus.OK
        assert second.served_from_cache
        assert transport.calls == 1

    def test_distinct_collection_windows_are_not_shared(self) -> None:
        transport = FakeTransport(
            [
                json_response({"items": [{"id": "1"}]}),
                json_response({"items": [{"id": "2"}]}),
            ]
        )
        client, _ = build_client(transport=transport)
        req_day_one = FetchRequest(
            url="https://example.test/items",
            host_key=HOST,
            client_profile=ClientProfile.PLAIN,
            collection_window="2026-08-18",
        )
        req_day_two = FetchRequest(
            url="https://example.test/items",
            host_key=HOST,
            client_profile=ClientProfile.PLAIN,
            collection_window="2026-08-19",
        )

        asyncio.run(client.get(req_day_one, expectations()))
        asyncio.run(client.get(req_day_two, expectations()))

        assert transport.calls == 2


class TestFetchClientValidationFailures:
    def test_wrong_content_type_never_becomes_a_successful_empty_result(self) -> None:
        transport = FakeTransport(
            [TransportResponse(status_code=200, content_type="text/html", body=b"<html/>", library_version="fake/1.0")]
        )
        client, _ = build_client(transport=transport)

        outcome = asyncio.run(client.get(request(), expectations()))

        assert outcome.status is FetchStatus.VALIDATION_FAILED
        assert isinstance(outcome.payload, UnexpectedContentType)

    def test_illegitimately_empty_never_becomes_a_successful_empty_result(self) -> None:
        transport = FakeTransport([json_response({"items": []})])
        client, _ = build_client(transport=transport)

        outcome = asyncio.run(
            client.get(request(), expectations(min_rows_when_healthy=1))
        )

        assert outcome.status is FetchStatus.VALIDATION_FAILED
        assert isinstance(outcome.payload, IllegitimatelyEmpty)

    def test_missing_required_fields_is_reported(self) -> None:
        transport = FakeTransport([json_response({"items": [{}]})])
        client, _ = build_client(transport=transport)

        outcome = asyncio.run(client.get(request(), expectations()))

        assert outcome.status is FetchStatus.VALIDATION_FAILED
        assert isinstance(outcome.payload, MissingRequiredFields)

    def test_legitimately_empty_source_is_ok(self) -> None:
        transport = FakeTransport([json_response({"items": []})])
        client, _ = build_client(transport=transport)

        outcome = asyncio.run(
            client.get(request(), expectations(min_rows_when_healthy=0))
        )

        assert outcome.status is FetchStatus.OK
        assert outcome.payload == []


class TestFetchClientBreakerIntegration:
    def test_open_breaker_reports_skipped_rather_than_empty(self) -> None:
        transport = FakeTransport(
            [
                TransportFailure(host_key=HOST, cause=OSError("refused")),
                TransportFailure(host_key=HOST, cause=OSError("refused")),
            ]
        )
        client, _ = build_client(
            transport=transport, breaker_settings=BreakerSettings(failure_threshold=1, cooldown_seconds=60.0)
        )

        first = asyncio.run(client.get(request(), expectations()))
        second = asyncio.run(client.get(request(), expectations()))

        assert first.status is FetchStatus.TRANSPORT_FAILED
        assert second.status is FetchStatus.SKIPPED_BREAKER_OPEN
        assert isinstance(second.payload, BreakerOpen)

    def test_transport_failure_retries_before_giving_up(self) -> None:
        transport = FakeTransport(
            [
                TransportFailure(host_key=HOST, cause=OSError("timeout")),
                json_response({"items": [{"id": "1"}]}),
            ]
        )
        client, _ = build_client(
            transport=transport, breaker_settings=BreakerSettings(failure_threshold=5, cooldown_seconds=60.0)
        )

        outcome = asyncio.run(client.get(request(), expectations()))

        assert outcome.status is FetchStatus.OK
        assert transport.calls == 2


class TestFetchClientLiveSource:
    """Exercises the client against a real benign source: Hacker News via Algolia.

    Skipped when the network is unavailable rather than failing the suite —
    fetch-guardrails requirement 1, 3, 6, 8.
    """

    HN_URL = "https://hn.algolia.com/api/v1/search"

    def _build_live_client(self) -> FetchClient:
        import httpx

        redis = FakeRedis()
        clock = FrozenClock()
        throttle = Throttle(redis, clock=clock, random_source=FixedRandom(0.0))
        breaker = Breaker(redis, BreakerSettings(failure_threshold=3, cooldown_seconds=30.0), clock=clock)
        cache = ResponseCache(redis, ttl_seconds=3600)
        budget = HostBudget(
            capacity=5.0, refill_rate_per_second=5.0, jitter_low_seconds=0.0, jitter_high_seconds=0.0
        )
        from mlsc.core.fetch.transports import PlainTransport

        plain = PlainTransport(httpx.AsyncClient())
        return FetchClient(
            breaker=breaker,
            cache=cache,
            throttle=throttle,
            plain_transport=plain,
            impersonating_transport=plain,
            host_budget=budget,
        )

    def test_live_fetch_validates_and_repeat_is_cached(self) -> None:
        pytest.importorskip("httpx")
        import httpx

        try:
            httpx.get(self.HN_URL, params={"query": "test", "tags": "story"}, timeout=5.0)
        except httpx.HTTPError:
            pytest.skip("network unavailable")

        client = self._build_live_client()
        req = FetchRequest(
            url=self.HN_URL,
            host_key="hn.algolia.com",
            client_profile=ClientProfile.PLAIN,
            query=(("query", "test"), ("tags", "story")),
        )
        live_expectations = FetchExpectations(
            content_type="application/json",
            item_path=("hits",),
            required_fields=("objectID", "created_at"),
            min_rows_when_healthy=1,
        )

        first = asyncio.run(client.get(req, live_expectations))
        second = asyncio.run(client.get(req, live_expectations))

        assert first.status is FetchStatus.OK
        assert not first.served_from_cache
        assert second.status is FetchStatus.OK
        assert second.served_from_cache

    def test_wrong_expectation_fails_loudly(self) -> None:
        pytest.importorskip("httpx")
        import httpx

        try:
            httpx.get(self.HN_URL, params={"query": "test", "tags": "story"}, timeout=5.0)
        except httpx.HTTPError:
            pytest.skip("network unavailable")

        client = self._build_live_client()
        req = FetchRequest(
            url=self.HN_URL,
            host_key="hn.algolia.com",
            client_profile=ClientProfile.PLAIN,
            query=(("query", "test"), ("tags", "story")),
            collection_window="wrong-expectation-case",
        )
        wrong_expectations = FetchExpectations(
            content_type="application/json",
            item_path=("hits",),
            required_fields=("this_field_does_not_exist",),
        )

        outcome = asyncio.run(client.get(req, wrong_expectations))

        assert outcome.status is FetchStatus.VALIDATION_FAILED
        assert isinstance(outcome.payload, MissingRequiredFields)
