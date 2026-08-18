"""Unit tests for the compatibility gate in scripts/verify_compatibility.py.

Requirements: 1.1-1.8, 4.2-4.5.

Pin values are read from the verifier's own EXPECTED table so these tests assert
gate behavior rather than restating a pin set that the spec workflow may revise.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = PROJECT_ROOT / "scripts" / "verify_compatibility.py"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_compatibility", VERIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_verifier()

INDETERMINATE_EVIDENCE = {
    "status": "indeterminate",
    "provider": None,
    "offering": None,
    "postgresql_major": 16,
    "timescaledb_release": None,
    "pgvector_release": None,
    "checks": {},
}


def manifest_text(
    name: str = "mlsc",
    channels: tuple[str, ...] = ("conda-forge", "nodefaults"),
    overrides: dict[str, str | None] | None = None,
) -> str:
    pins = dict(gate.EXPECTED)
    for package, pinned in (overrides or {}).items():
        if pinned is None:
            pins.pop(package, None)
        else:
            pins[package] = pinned
    lines = [f"name: {name}", "channels:"]
    lines += [f"  - {channel}" for channel in channels]
    lines.append("dependencies:")
    lines += [f"  - {package}={pinned}" for package, pinned in pins.items()]
    return "\n".join(lines) + "\n"


def passing_evidence() -> dict:
    return {
        "status": "verified",
        "provider": "example-managed-postgres",
        "offering": "example-offering",
        "postgresql_major": 16,
        "timescaledb_release": "2.29.1",
        "pgvector_release": "0.8.3",
        "checks": {
            check: {"status": "pass", "source": "https://example.invalid/evidence"}
            for check in gate.REQUIRED_PROVIDER_CHECKS
        },
    }


def matching_distribution_versions() -> dict[str, str]:
    return {
        gate.DISTRIBUTIONS.get(package, package): pinned
        for package, pinned in gate.EXPECTED.items()
        if package != "python"
    }


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "environment.yml"
    path.write_text(manifest_text())
    return path


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    path = tmp_path / "provider-compatibility.json"
    path.write_text(json.dumps(passing_evidence()))
    return path


@pytest.fixture
def matching_runtime(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    installed = matching_distribution_versions()
    monkeypatch.setattr(gate.platform, "python_version", lambda: gate.EXPECTED["python"])
    monkeypatch.setattr(gate, "version", lambda distribution: installed[distribution])
    return installed


@pytest.fixture
def gate_runner(capsys: pytest.CaptureFixture[str]):
    def run(manifest_path: Path, evidence_path: Path) -> tuple[int, dict]:
        original_argv = sys.argv
        sys.argv = ["verify_compatibility.py", str(manifest_path), str(evidence_path)]
        try:
            exit_code = gate.main()
        finally:
            sys.argv = original_argv
        return exit_code, json.loads(capsys.readouterr().out)

    return run


def test_parse_environment_reads_name_channels_and_exact_pins(tmp_path: Path):
    path = tmp_path / "environment.yml"
    path.write_text(manifest_text())

    name, channels, dependencies = gate.parse_environment(path)

    assert name == "mlsc"
    assert channels == ["conda-forge", "nodefaults"]
    assert dependencies == dict(gate.EXPECTED)


def test_parse_environment_skips_comments_and_unversioned_dependencies(tmp_path: Path):
    path = tmp_path / "environment.yml"
    path.write_text(
        "# local notes\n"
        "name: mlsc\n"
        "channels:\n"
        "  - conda-forge\n"
        "\n"
        "dependencies:\n"
        "  - python=3.12.8\n"
        "  - somepackage\n"
    )

    name, channels, dependencies = gate.parse_environment(path)

    assert (name, channels) == ("mlsc", ["conda-forge"])
    assert dependencies == {"python": "3.12.8"}


def test_exact_pin_mismatch_is_reported_with_expected_and_detected(
    tmp_path: Path, evidence: Path, matching_runtime, gate_runner
):
    path = tmp_path / "environment.yml"
    path.write_text(manifest_text(overrides={"alembic": "9.9.9"}))

    exit_code, report = gate_runner(path, evidence)

    assert exit_code == 1
    assert report["status"] == "failed"
    assert any(
        failure.startswith("manifest pin mismatch for alembic:")
        and gate.EXPECTED["alembic"] in failure
        and "9.9.9" in failure
        for failure in report["failures"]
    )


def test_missing_manifest_pin_is_reported_as_an_undeclared_pin(
    tmp_path: Path, evidence: Path, matching_runtime, gate_runner
):
    path = tmp_path / "environment.yml"
    path.write_text(manifest_text(overrides={"croniter": None}))

    exit_code, report = gate_runner(path, evidence)

    assert exit_code == 1
    assert "manifest pin mismatch for croniter" in " ".join(report["failures"])
    assert "None" in " ".join(report["failures"])


@pytest.mark.parametrize(
    ("name", "channels", "expected_fragment"),
    [
        ("other", ("conda-forge", "nodefaults"), "environment name must be mlsc"),
        ("mlsc", ("nodefaults", "conda-forge"), "channels must be conda-forge then nodefaults"),
        ("mlsc", ("conda-forge", "defaults"), "channels must be conda-forge then nodefaults"),
    ],
)
def test_environment_identity_and_channel_order_are_enforced(
    tmp_path: Path,
    evidence: Path,
    matching_runtime,
    gate_runner,
    name: str,
    channels: tuple[str, ...],
    expected_fragment: str,
):
    path = tmp_path / "environment.yml"
    path.write_text(manifest_text(name=name, channels=channels))

    exit_code, report = gate_runner(path, evidence)

    assert exit_code == 1
    assert any(expected_fragment in failure for failure in report["failures"])


def test_python_build_mismatch_fails_closed(
    manifest: Path,
    evidence: Path,
    matching_runtime,
    monkeypatch: pytest.MonkeyPatch,
    gate_runner,
):
    monkeypatch.setattr(gate.platform, "python_version", lambda: "3.11.0")

    exit_code, report = gate_runner(manifest, evidence)

    assert exit_code == 1
    assert report["python"] == "3.11.0"
    assert any(
        failure.startswith("runtime pin mismatch for python:") and "3.11.0" in failure
        for failure in report["failures"]
    )


def test_unavailable_runtime_package_is_reported_as_a_failure(
    manifest: Path,
    evidence: Path,
    matching_runtime: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    gate_runner,
):
    def missing_asyncpg(distribution: str) -> str:
        if distribution == "asyncpg":
            raise PackageNotFoundError(distribution)
        return matching_runtime[distribution]

    monkeypatch.setattr(gate, "version", missing_asyncpg)

    exit_code, report = gate_runner(manifest, evidence)

    assert exit_code == 1
    assert report["failures"] == ["runtime package is unavailable: asyncpg"]


def test_runtime_drift_is_detected_through_the_mapped_distribution_name(
    manifest: Path,
    evidence: Path,
    matching_runtime: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    gate_runner,
):
    redis_distribution = gate.DISTRIBUTIONS["redis-py"]
    drifted = dict(matching_runtime, **{redis_distribution: "0.0.1"})
    monkeypatch.setattr(gate, "version", lambda distribution: drifted[distribution])

    exit_code, report = gate_runner(manifest, evidence)

    assert exit_code == 1
    assert any(
        failure.startswith("runtime pin mismatch for redis-py:") and "0.0.1" in failure
        for failure in report["failures"]
    )


def test_missing_provider_evidence_file_is_a_failure(tmp_path: Path):
    absent = tmp_path / "provider-compatibility.json"

    failures = gate.validate_provider_evidence(absent)

    assert failures == [f"missing managed-provider evidence: {absent}"]


def test_indeterminate_provider_evidence_names_every_unproven_field_and_check(tmp_path: Path):
    path = tmp_path / "provider-compatibility.json"
    path.write_text(json.dumps(INDETERMINATE_EVIDENCE))

    failures = gate.validate_provider_evidence(path)

    for field in ("provider", "offering", "timescaledb_release", "pgvector_release"):
        assert f"provider evidence field is indeterminate: {field}" in failures
    for check in gate.REQUIRED_PROVIDER_CHECKS:
        assert f"provider capability is not proven: {check}" in failures


@pytest.mark.parametrize(
    "check_result",
    [
        {"status": "fail", "source": "https://example.invalid/evidence"},
        {"status": "indeterminate", "source": "https://example.invalid/evidence"},
        {"status": "pass"},
        {"status": "pass", "source": ""},
        {},
    ],
)
def test_provider_capability_requires_a_passing_result_with_a_source(
    tmp_path: Path, check_result: dict
):
    path = tmp_path / "provider-compatibility.json"
    document = passing_evidence()
    document["checks"]["transactional_create_extension"] = check_result
    path.write_text(json.dumps(document))

    failures = gate.validate_provider_evidence(path)

    assert failures == ["provider capability is not proven: transactional_create_extension"]


def test_provider_evidence_must_affirm_postgresql_major_16(tmp_path: Path):
    path = tmp_path / "provider-compatibility.json"
    document = passing_evidence()
    document["postgresql_major"] = 15
    path.write_text(json.dumps(document))

    failures = gate.validate_provider_evidence(path)

    assert failures == ["provider evidence does not affirm PostgreSQL major 16"]


def test_complete_provider_evidence_produces_no_failures(evidence: Path):
    assert gate.validate_provider_evidence(evidence) == []


def test_gate_approves_only_when_manifest_runtime_and_provider_all_pass(
    manifest: Path, evidence: Path, matching_runtime, gate_runner
):
    exit_code, report = gate_runner(manifest, evidence)

    assert exit_code == 0
    assert report["status"] == "passed"
    assert report["failures"] == []
    assert report["manifest"] == str(manifest)
    assert report["provider_evidence"] == str(evidence)


def test_failed_gate_never_rewrites_the_manifest_or_the_provider_evidence(
    tmp_path: Path, matching_runtime, gate_runner
):
    manifest_path = tmp_path / "environment.yml"
    manifest_path.write_text(manifest_text(overrides={"fastapi": "0.0.1"}))
    evidence_path = tmp_path / "provider-compatibility.json"
    evidence_path.write_text(json.dumps(INDETERMINATE_EVIDENCE))
    manifest_before = manifest_path.read_text()
    evidence_before = evidence_path.read_text()

    exit_code, report = gate_runner(manifest_path, evidence_path)

    assert exit_code == 1
    assert report["status"] == "failed"
    assert manifest_path.read_text() == manifest_before
    assert evidence_path.read_text() == evidence_before


def test_gate_reports_every_independent_failure_for_requirements_escalation(
    tmp_path: Path, matching_runtime, monkeypatch: pytest.MonkeyPatch, gate_runner
):
    manifest_path = tmp_path / "environment.yml"
    manifest_path.write_text(manifest_text(name="other", overrides={"celery": "0.0.1"}))
    evidence_path = tmp_path / "provider-compatibility.json"
    evidence_path.write_text(json.dumps({"postgresql_major": 15, "checks": {}}))
    monkeypatch.setattr(gate.platform, "python_version", lambda: "3.11.0")

    exit_code, report = gate_runner(manifest_path, evidence_path)

    assert exit_code == 1
    joined = " ".join(report["failures"])
    assert "environment name must be mlsc" in joined
    assert "manifest pin mismatch for celery" in joined
    assert "runtime pin mismatch for python" in joined
    assert "provider evidence does not affirm PostgreSQL major 16" in joined
    assert "provider capability is not proven" in joined


def test_gate_rejects_an_incomplete_invocation(capsys: pytest.CaptureFixture[str]):
    original_argv = sys.argv
    sys.argv = ["verify_compatibility.py", "environment.yml"]
    try:
        exit_code = gate.main()
    finally:
        sys.argv = original_argv

    assert exit_code == 2
    assert "usage: verify_compatibility.py" in capsys.readouterr().err
