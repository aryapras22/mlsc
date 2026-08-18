from __future__ import annotations

import json
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

EXPECTED = {
    "python": "3.14.6",
    "fastapi": "0.141.1",
    "uvicorn": "0.52.3",
    "sqlalchemy": "2.0.52",
    "alembic": "1.19.1",
    "asyncpg": "0.31.0",
    "pydantic": "2.13.4",
    "pydantic-settings": "2.15.0",
    "celery": "5.6.3",
    "redis-py": "8.1.0",
    "pgvector-python": "0.5.0",
    "croniter": "6.2.4",
    "pytest": "9.1.1",
}
DISTRIBUTIONS = {"redis-py": "redis", "pgvector-python": "pgvector"}
REQUIRED_PROVIDER_CHECKS = {
    "provider_supports_exact_releases",
    "provider_side_extension_provisioning",
    "transactional_create_extension",
    "generated_columns_on_hypertables",
    "transactional_downgrade_reversion",
}


def parse_environment(path: Path) -> tuple[str | None, list[str], dict[str, str]]:
    name = None
    channels: list[str] = []
    dependencies: dict[str, str] = {}
    section = None
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not raw_line.startswith(" ") and line.endswith(":"):
            section = line[:-1]
            continue
        if line.startswith("name:"):
            name = line.partition(":")[2].strip()
            continue
        if not line.startswith("-"):
            continue
        value = line[1:].strip()
        if section == "channels":
            channels.append(value)
        elif section == "dependencies" and "=" in value:
            package, pinned_version = value.split("=", 1)
            dependencies[package] = pinned_version
    return name, channels, dependencies


def validate_provider_evidence(path: Path) -> list[str]:
    if not path.exists():
        return [f"missing managed-provider evidence: {path}"]
    evidence = json.loads(path.read_text())
    failures = []
    for field in ("provider", "offering", "timescaledb_release", "pgvector_release"):
        if not evidence.get(field):
            failures.append(f"provider evidence field is indeterminate: {field}")
    if evidence.get("postgresql_major") != 16:
        failures.append("provider evidence does not affirm PostgreSQL major 16")
    checks = evidence.get("checks", {})
    for check in sorted(REQUIRED_PROVIDER_CHECKS):
        result = checks.get(check, {})
        if result.get("status") != "pass" or not result.get("source"):
            failures.append(f"provider capability is not proven: {check}")
    return failures


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify_compatibility.py ENVIRONMENT_YML PROVIDER_EVIDENCE", file=sys.stderr)
        return 2

    environment_path = Path(sys.argv[1])
    provider_path = Path(sys.argv[2])
    failures: list[str] = []
    name, channels, dependencies = parse_environment(environment_path)

    if name != "mlsc":
        failures.append(f"environment name must be mlsc, detected {name!r}")
    if channels != ["conda-forge", "nodefaults"]:
        failures.append(f"channels must be conda-forge then nodefaults, detected {channels!r}")
    for package, expected_version in EXPECTED.items():
        declared_version = dependencies.get(package)
        if declared_version != expected_version:
            failures.append(
                f"manifest pin mismatch for {package}: expected {expected_version}, "
                f"detected {declared_version!r}"
            )

    detected_python = platform.python_version()
    if detected_python != EXPECTED["python"]:
        failures.append(
            f"runtime pin mismatch for python: expected {EXPECTED['python']}, "
            f"detected {detected_python}"
        )
    for package, expected_version in EXPECTED.items():
        if package == "python":
            continue
        distribution = DISTRIBUTIONS.get(package, package)
        try:
            detected_version = version(distribution)
        except PackageNotFoundError:
            failures.append(f"runtime package is unavailable: {package}")
            continue
        if detected_version != expected_version:
            failures.append(
                f"runtime pin mismatch for {package}: expected {expected_version}, "
                f"detected {detected_version}"
            )

    failures.extend(validate_provider_evidence(provider_path))
    result = {
        "status": "failed" if failures else "passed",
        "python": detected_python,
        "manifest": str(environment_path),
        "provider_evidence": str(provider_path),
        "failures": failures,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
