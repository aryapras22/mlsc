"""Router-level tests for `mlsc/api/themes.py`, in isolation from
`ThemeJobService`/`ThemeService` and from `mlsc.main.create_app`'s real
lifespan. No database, no network: every dependency the router reads off
`request.app.state` is a fake built in this file, and the app under test
is a bare `FastAPI()` with only `themes_router` mounted.

This complements `tests/test_theme_monitors.py`'s application-layer tests
(task 9) rather than re-testing them: the point here is the router's
request/response mapping and its exception-to-status-code translation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mlsc.api.themes import router as themes_router
from mlsc.application.themes import CandidateNotViable, NotAThemeMonitor, ThemeJobOverlaps
from mlsc.db.models import CandidateState, SourceName, ThemeJobKind, ThemeJobStatus
from mlsc.repositories.monitors import MonitorNotFound
from mlsc.repositories.themes import EntityCandidateNotFound, ThemeSeedNotFound


class FakeThemeJobService:
    """Stands in for `ThemeJobService`: each submit method returns a
    canned id or raises a canned exception, controlled per test."""

    def __init__(self) -> None:
        self.generation_result = uuid.uuid4()
        self.discovery_result = uuid.uuid4()
        self.generation_error: Exception | None = None
        self.discovery_error: Exception | None = None

    async def submit_generation(self, monitor_id: uuid.UUID) -> uuid.UUID:
        if self.generation_error:
            raise self.generation_error
        return self.generation_result

    async def submit_discovery(self, monitor_id: uuid.UUID) -> uuid.UUID:
        if self.discovery_error:
            raise self.discovery_error
        return self.discovery_result


class FakeThemeService:
    """Stands in for `ThemeService`: every method returns a canned value
    or raises a canned exception, controlled per test."""

    def __init__(self) -> None:
        self.queries: list[dict] = []
        self.get_queries_error: Exception | None = None
        self.review_queries_error: Exception | None = None
        self.candidates: list = []
        self.accept_result = uuid.uuid4()
        self.accept_error: Exception | None = None
        self.reject_error: Exception | None = None

    async def get_queries(self, monitor_id: uuid.UUID) -> list[dict]:
        if self.get_queries_error:
            raise self.get_queries_error
        return self.queries

    async def review_queries(self, monitor_id: uuid.UUID, queries: list) -> None:
        if self.review_queries_error:
            raise self.review_queries_error

    async def list_candidates(self, monitor_id: uuid.UUID, *, state: CandidateState | None = None) -> list:
        return self.candidates

    async def accept_candidate(self, monitor_id: uuid.UUID, candidate_id: uuid.UUID) -> uuid.UUID:
        if self.accept_error:
            raise self.accept_error
        return self.accept_result

    async def reject_candidate(self, monitor_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
        if self.reject_error:
            raise self.reject_error


class _FakeSession:
    def __init__(self, jobs: dict[uuid.UUID, object]) -> None:
        self._jobs = jobs

    async def get(self, model: type, pk: uuid.UUID, populate_existing: bool = True) -> object | None:
        return self._jobs.get(pk)


class _FakeSessionContext:
    def __init__(self, jobs: dict[uuid.UUID, object]) -> None:
        self._jobs = jobs

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._jobs)

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeStartup:
    """Backs `request.app.state.startup.session_factory` for
    `get_theme_job`, the one route that reads a session directly instead
    of going through a fake service — `ThemeJobRepository.get` only ever
    calls `session.get(ThemeJob, job_id, ...)`, so an in-memory dict is
    enough to stand in for it without a real engine."""

    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, object] = {}

    def session_factory(self) -> _FakeSessionContext:
        return _FakeSessionContext(self.jobs)


def _theme_job(*, monitor_id: uuid.UUID, **overrides: object) -> SimpleNamespace:
    defaults = dict(
        id=uuid.uuid4(), monitor_id=monitor_id, kind=ThemeJobKind.QUERY_GENERATION,
        status=ThemeJobStatus.PENDING, submitted_at=datetime.now(timezone.utc),
        finished_at=None, error=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _candidate(*, monitor_id: uuid.UUID, **overrides: object) -> SimpleNamespace:
    defaults = dict(
        id=uuid.uuid4(), monitor_id=monitor_id, source_name=SourceName.APPSTORE,
        entity_ref="123456", display_name="Some App", reason="matched query",
        proposed_by_query="note app", provenance={"surface": "app_store_search"},
        state=CandidateState.PROPOSED, created_at=datetime.now(timezone.utc), reviewed_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class Harness(NamedTuple):
    client: TestClient
    jobs: FakeThemeJobService
    themes: FakeThemeService
    startup: FakeStartup


@pytest.fixture
def harness() -> Harness:
    app = FastAPI()
    app.include_router(themes_router)
    jobs = FakeThemeJobService()
    themes = FakeThemeService()
    startup = FakeStartup()
    app.state.theme_job_service = jobs
    app.state.theme_service = themes
    app.state.startup = startup
    return Harness(TestClient(app), jobs, themes, startup)


class TestSubmitRoutes:
    @pytest.mark.parametrize(
        "path,result_attr",
        [
            ("/monitors/{id}/theme/queries/generate", "generation_result"),
            ("/monitors/{id}/theme/discovery", "discovery_result"),
        ],
    )
    def test_a_submission_returns_202_with_the_job_id(
        self, harness: Harness, path: str, result_attr: str
    ) -> None:
        monitor_id = uuid.uuid4()
        response = harness.client.post(path.format(id=monitor_id))
        assert response.status_code == 202
        assert response.json() == {"job_id": str(getattr(harness.jobs, result_attr))}

    @pytest.mark.parametrize(
        "path,error_attr",
        [
            ("/monitors/{id}/theme/queries/generate", "generation_error"),
            ("/monitors/{id}/theme/discovery", "discovery_error"),
        ],
    )
    def test_a_non_theme_monitor_is_rejected_with_422(
        self, harness: Harness, path: str, error_attr: str
    ) -> None:
        monitor_id = uuid.uuid4()
        setattr(harness.jobs, error_attr, NotAThemeMonitor(monitor_id))
        response = harness.client.post(path.format(id=monitor_id))
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "path,error_attr",
        [
            ("/monitors/{id}/theme/queries/generate", "generation_error"),
            ("/monitors/{id}/theme/discovery", "discovery_error"),
        ],
    )
    def test_an_overlapping_job_is_rejected_with_409_naming_the_running_job(
        self, harness: Harness, path: str, error_attr: str
    ) -> None:
        monitor_id = uuid.uuid4()
        running_job_id = uuid.uuid4()
        setattr(harness.jobs, error_attr, ThemeJobOverlaps(running_job_id))
        response = harness.client.post(path.format(id=monitor_id))
        assert response.status_code == 409
        assert str(running_job_id) in response.json()["detail"]

    @pytest.mark.parametrize(
        "path,error_attr",
        [
            ("/monitors/{id}/theme/queries/generate", "generation_error"),
            ("/monitors/{id}/theme/discovery", "discovery_error"),
        ],
    )
    def test_a_missing_monitor_is_reported_as_404(
        self, harness: Harness, path: str, error_attr: str
    ) -> None:
        monitor_id = uuid.uuid4()
        setattr(harness.jobs, error_attr, MonitorNotFound(monitor_id))
        response = harness.client.post(path.format(id=monitor_id))
        assert response.status_code == 404


class TestQueryRoutes:
    def test_listing_queries_round_trips_the_seed_through_the_schema(self, harness: Harness) -> None:
        monitor_id = uuid.uuid4()
        harness.themes.queries = [
            {"text": "ai note taking app", "rationale": "direct match", "accepted": True}
        ]
        response = harness.client.get(f"/monitors/{monitor_id}/theme/queries")
        assert response.status_code == 200
        assert response.json() == harness.themes.queries

    def test_listing_queries_for_a_monitor_with_no_seed_is_404(self, harness: Harness) -> None:
        monitor_id = uuid.uuid4()
        harness.themes.get_queries_error = ThemeSeedNotFound(monitor_id)
        response = harness.client.get(f"/monitors/{monitor_id}/theme/queries")
        assert response.status_code == 404

    def test_reviewing_queries_returns_204(self, harness: Harness) -> None:
        monitor_id = uuid.uuid4()
        response = harness.client.put(
            f"/monitors/{monitor_id}/theme/queries",
            json={"queries": [{"text": "ai note taking app", "rationale": "specific"}]},
        )
        assert response.status_code == 204

    def test_reviewing_queries_for_a_monitor_with_no_seed_is_404(self, harness: Harness) -> None:
        monitor_id = uuid.uuid4()
        harness.themes.review_queries_error = ThemeSeedNotFound(monitor_id)
        response = harness.client.put(f"/monitors/{monitor_id}/theme/queries", json={"queries": []})
        assert response.status_code == 404


class TestCandidateRoutes:
    def test_listing_candidates_round_trips_through_the_schema(self, harness: Harness) -> None:
        monitor_id = uuid.uuid4()
        candidate = _candidate(monitor_id=monitor_id)
        harness.themes.candidates = [candidate]
        response = harness.client.get(f"/monitors/{monitor_id}/theme/candidates")
        assert response.status_code == 200
        [body] = response.json()
        assert body["id"] == str(candidate.id)
        assert body["source_name"] == candidate.source_name.value
        assert body["state"] == candidate.state.value

    def test_accepting_a_candidate_returns_200_with_the_new_source_id(self, harness: Harness) -> None:
        monitor_id, candidate_id = uuid.uuid4(), uuid.uuid4()
        response = harness.client.post(
            f"/monitors/{monitor_id}/theme/candidates/{candidate_id}/accept"
        )
        assert response.status_code == 200
        assert response.json() == {"source_id": str(harness.themes.accept_result)}

    def test_rejecting_a_candidate_returns_204(self, harness: Harness) -> None:
        monitor_id, candidate_id = uuid.uuid4(), uuid.uuid4()
        response = harness.client.post(
            f"/monitors/{monitor_id}/theme/candidates/{candidate_id}/reject"
        )
        assert response.status_code == 204

    @pytest.mark.parametrize("action", ["accept", "reject"])
    def test_an_unknown_candidate_is_reported_as_404(self, harness: Harness, action: str) -> None:
        monitor_id, candidate_id = uuid.uuid4(), uuid.uuid4()
        error = EntityCandidateNotFound(candidate_id)
        if action == "accept":
            harness.themes.accept_error = error
        else:
            harness.themes.reject_error = error
        response = harness.client.post(
            f"/monitors/{monitor_id}/theme/candidates/{candidate_id}/{action}"
        )
        assert response.status_code == 404

    def test_a_nonviable_accept_is_reported_as_422_with_the_reason(self, harness: Harness) -> None:
        monitor_id, candidate_id = uuid.uuid4(), uuid.uuid4()
        harness.themes.accept_error = CandidateNotViable(candidate_id, "app id already attached")
        response = harness.client.post(
            f"/monitors/{monitor_id}/theme/candidates/{candidate_id}/accept"
        )
        assert response.status_code == 422
        assert "app id already attached" in response.json()["detail"]


class TestJobPollingRoute:
    def test_polling_a_job_round_trips_through_the_schema(self, harness: Harness) -> None:
        monitor_id = uuid.uuid4()
        job = _theme_job(monitor_id=monitor_id, status=ThemeJobStatus.COMPLETE)
        harness.startup.jobs[job.id] = job
        response = harness.client.get(f"/monitors/{monitor_id}/theme/jobs/{job.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(job.id)
        assert body["status"] == "complete"

    def test_polling_an_unknown_job_is_404(self, harness: Harness) -> None:
        monitor_id, job_id = uuid.uuid4(), uuid.uuid4()
        response = harness.client.get(f"/monitors/{monitor_id}/theme/jobs/{job_id}")
        assert response.status_code == 404

    def test_polling_a_job_that_belongs_to_a_different_monitor_is_404(self, harness: Harness) -> None:
        # The route checks job.monitor_id against the path's monitor_id after
        # a successful lookup, not just whether the job exists at all.
        job = _theme_job(monitor_id=uuid.uuid4())
        harness.startup.jobs[job.id] = job
        other_monitor_id = uuid.uuid4()
        response = harness.client.get(f"/monitors/{other_monitor_id}/theme/jobs/{job.id}")
        assert response.status_code == 404
