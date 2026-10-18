from __future__ import annotations

import json
from pathlib import Path

import pytest

from netops.diagnostics import Collection
from netops.errors import OperationalError
from netops.health import HealthSummary, ServiceHealth
from netops.report import (
    RunResult,
    load_run_result,
    render_markdown,
    status_exit_code,
)


def make_result(status: str = "degraded") -> RunResult:
    service = ServiceHealth(
        name="edge-api",
        type="http",
        target="http://127.0.0.1:8080/healthz",
        required=True,
        ok=True,
        status="healthy",
        attempts=2,
        latency_ms=18.4,
        detail="HTTP 200",
        status_code=200,
        checked_at="2026-08-10T10:00:00+00:00",
    )
    optional = ServiceHealth(
        name="cache",
        type="tcp",
        target="127.0.0.1:6379",
        required=False,
        ok=False,
        status="failed",
        attempts=1,
        latency_ms=12.0,
        detail="connection refused on 127.0.0.1:6379",
        hint="Confirm the service is listening on that host and port.",
        checked_at="2026-08-10T10:00:00+00:00",
    )
    collection = Collection(
        name="edge-api-log",
        type="file",
        target="/var/log/edge-api.log",
        required=True,
        ok=True,
        status="collected",
        duration_ms=2.1,
        line_count=10,
        bytes=512,
        truncated=False,
        artifact="edge-api-log.log",
    )
    return RunResult(
        generated_at="2026-08-10T10:00:01+00:00",
        environment="lab",
        config_path="/srv/netops/config.yaml",
        title="Edge Network Operations Report",
        status=status,
        duration_ms=42.5,
        health=HealthSummary(
            status="degraded" if status == "degraded" else "healthy",
            total=2,
            healthy=1,
            failed=1,
            planned=0,
            required=1,
            required_failures=0,
            optional_failures=1,
        ),
        services=(service, optional),
        collections=(collection,),
        config_summary={"environment": "lab"},
    )


def test_markdown_contains_operational_sections() -> None:
    report = render_markdown(make_result())
    assert "# Edge Network Operations Report" in report
    assert "**Overall status:** `DEGRADED`" in report
    assert "## Health checks" in report
    assert "| edge-api |" in report
    assert "diagnostics/edge-api-log.log" in report
    assert "Optional service `cache` failed" in report
    assert "Confirm the service is listening" in report
    assert "## Run metadata" in report


def test_result_json_round_trip() -> None:
    result = make_result()
    restored = RunResult.from_dict(result.to_dict())
    assert restored.environment == result.environment
    assert restored.health == result.health
    assert restored.services[1].hint == result.services[1].hint
    assert restored.collections[0].artifact == "edge-api-log.log"


def test_load_run_result(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(make_result().to_dict()), encoding="utf-8")
    result = load_run_result(path)
    assert result.title == "Edge Network Operations Report"


def test_load_invalid_run_result(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(OperationalError, match="could not read"):
        load_run_result(path)


def test_status_exit_codes() -> None:
    assert status_exit_code("healthy") == 0
    assert status_exit_code("planned") == 0
    assert status_exit_code("degraded") == 1
    assert status_exit_code("unhealthy") == 3
