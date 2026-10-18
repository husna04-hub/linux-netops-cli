from __future__ import annotations

import sys
import time

from netops.config import RetrySettings, ServiceConfig
from netops.health import (
    ServiceHealth,
    check_service,
    check_services,
    summarize,
    target_for,
)
from tests.conftest import respond


def service(**overrides) -> ServiceConfig:
    values = dict(
        name="api",
        type="http",
        required=True,
        timeout_seconds=1.0,
        retry=RetrySettings(retries=0, backoff_seconds=0.0, max_backoff_seconds=0.0),
        url="http://127.0.0.1:1/healthz",
        expected_status=(200,),
    )
    values.update(overrides)
    return ServiceConfig(**values)


def test_http_health_success(start_http_server) -> None:
    _, base = start_http_server(lambda handler: respond(handler))
    result = check_service(service(url=f"{base}/healthz"))
    assert result.ok is True
    assert result.status == "healthy"
    assert result.status_code == 200
    assert result.attempts == 1
    assert "HTTP 200" in result.detail


def test_http_unexpected_status_fails(start_http_server) -> None:
    _, base = start_http_server(lambda handler: respond(handler, 503))
    result = check_service(service(url=f"{base}/healthz"))
    assert result.ok is False
    assert result.status == "failed"
    assert result.status_code == 503
    assert "HTTP 503" in result.detail
    assert result.hint


def test_expected_non_200_status_succeeds(start_http_server) -> None:
    _, base = start_http_server(lambda handler: respond(handler, 503))
    result = check_service(
        service(url=f"{base}/healthz", expected_status=(503,))
    )
    assert result.ok is True
    assert "expected" in result.detail


def test_http_retries_then_succeeds(start_http_server) -> None:
    state = {"count": 0}

    def flaky(handler) -> None:
        state["count"] += 1
        respond(handler, 503 if state["count"] == 1 else 200)

    _, base = start_http_server(flaky)
    result = check_service(
        service(
            url=f"{base}/healthz",
            retry=RetrySettings(
                retries=2, backoff_seconds=0.0, max_backoff_seconds=0.0
            ),
        ),
        sleep=lambda _: None,
    )
    assert result.ok is True
    assert result.attempts == 2
    assert state["count"] == 2


def test_http_timeout_is_bounded(start_http_server) -> None:
    def slow(handler) -> None:
        time.sleep(0.3)
        try:
            respond(handler)
        except (BrokenPipeError, ConnectionResetError):
            pass

    _, base = start_http_server(slow)
    started = time.perf_counter()
    result = check_service(
        service(url=f"{base}/healthz", timeout_seconds=0.05)
    )
    elapsed = time.perf_counter() - started
    assert result.ok is False
    assert "timed out" in result.detail
    assert elapsed < 1.0


def test_tcp_health_success(start_http_server) -> None:
    server, _ = start_http_server(lambda handler: respond(handler))
    result = check_service(
        service(
            type="tcp",
            url=None,
            host="127.0.0.1",
            port=server.server_port,
        )
    )
    assert result.ok is True
    assert "TCP connect" in result.detail


def test_command_health_success() -> None:
    result = check_service(
        service(
            type="command",
            url=None,
            command=(sys.executable, "-c", "print('ok')"),
        )
    )
    assert result.ok is True


def test_command_health_failure() -> None:
    result = check_service(
        service(
            type="command",
            url=None,
            command=(sys.executable, "-c", "import sys; sys.exit(7)"),
            expected_exit_code=0,
        )
    )
    assert result.ok is False
    assert "exit code 7" in result.detail


def test_dry_run_does_not_contact_target() -> None:
    result = check_service(service(url="http://127.0.0.1:1/healthz"), dry_run=True)
    assert result.status == "planned"
    assert result.attempts == 0
    assert result.latency_ms == 0


def test_target_for_all_types() -> None:
    assert target_for(service()) == "http://127.0.0.1:1/healthz"
    assert target_for(service(type="tcp", url=None, host="h", port=1)) == "h:1"
    assert target_for(
        service(type="command", url=None, command=("true",))
    ) == "true"


def test_check_services_dry_run_preserves_order() -> None:
    services = [
        service(name="one"),
        service(name="two", type="tcp", url=None, host="h", port=1),
    ]
    results = check_services(services, dry_run=True)
    assert [item.name for item in results] == ["one", "two"]
    assert all(item.status == "planned" for item in results)


def _result(name: str, ok: bool, required: bool) -> ServiceHealth:
    return ServiceHealth(
        name=name,
        type="http",
        target="http://x",
        required=required,
        ok=ok,
        status="healthy" if ok else "failed",
        attempts=1,
        latency_ms=1.0,
        detail="detail",
    )


def test_summary_healthy() -> None:
    summary = summarize([_result("a", True, True), _result("b", True, False)])
    assert summary.status == "healthy"
    assert summary.healthy == 2
    assert summary.required_failures == 0


def test_summary_degraded_for_optional_failure() -> None:
    summary = summarize([_result("a", True, True), _result("b", False, False)])
    assert summary.status == "degraded"
    assert summary.optional_failures == 1


def test_summary_unhealthy_for_required_failure() -> None:
    summary = summarize([_result("a", False, True), _result("b", True, False)])
    assert summary.status == "unhealthy"
    assert summary.required_failures == 1
