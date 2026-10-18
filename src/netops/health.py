"""Health checks with strict timeouts and bounded exponential retries."""

from __future__ import annotations

import logging
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import ServiceConfig
from .log import event, get_logger

logger = get_logger("health")

USER_AGENT = "netops-cli/0.1 (+health-check)"


@dataclass(frozen=True)
class ServiceHealth:
    name: str
    type: str
    target: str
    required: bool
    ok: bool
    status: str
    attempts: int
    latency_ms: float
    detail: str
    status_code: int | None = None
    hint: str | None = None
    checked_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.type,
            "target": self.target,
            "required": self.required,
            "ok": self.ok,
            "status": self.status,
            "attempts": self.attempts,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "status_code": self.status_code,
            "hint": self.hint,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True)
class HealthSummary:
    status: str
    total: int
    healthy: int
    failed: int
    planned: int
    required: int
    required_failures: int
    optional_failures: int

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "total": self.total,
            "healthy": self.healthy,
            "failed": self.failed,
            "planned": self.planned,
            "required": self.required,
            "required_failures": self.required_failures,
            "optional_failures": self.optional_failures,
        }


class _CheckError(Exception):
    def __init__(
        self,
        detail: str,
        hint: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.hint = hint
        self.status_code = status_code


def target_for(service: ServiceConfig) -> str:
    if service.type == "http":
        return service.url or "<missing-url>"
    if service.type == "tcp":
        return f"{service.host}:{service.port}"
    if service.command:
        return " ".join(service.command)
    return "<missing-command>"


def check_service(
    service: ServiceConfig,
    *,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> ServiceHealth:
    target = target_for(service)
    if dry_run:
        return ServiceHealth(
            name=service.name,
            type=service.type,
            target=target,
            required=service.required,
            ok=False,
            status="planned",
            attempts=0,
            latency_ms=0.0,
            detail="dry-run: no network call made",
            checked_at=_now(),
        )

    started = clock()
    total_attempts = service.retry.retries + 1
    last_error: _CheckError | None = None

    for attempt in range(1, total_attempts + 1):
        try:
            detail, status_code = _check_once(service)
            return ServiceHealth(
                name=service.name,
                type=service.type,
                target=target,
                required=service.required,
                ok=True,
                status="healthy",
                attempts=attempt,
                latency_ms=round((clock() - started) * 1000, 1),
                detail=detail,
                status_code=status_code,
                checked_at=_now(),
            )
        except _CheckError as exc:
            last_error = exc
            if attempt < total_attempts:
                delay = service.retry.delay_for(attempt)
                event(
                    logger,
                    logging.WARNING,
                    "health_retry",
                    "health check attempt failed; retrying",
                    service=service.name,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    delay_seconds=round(delay, 3),
                    detail=exc.detail,
                )
                if delay > 0:
                    sleep(delay)
        except Exception as exc:  # pragma: no cover - defensive CLI boundary
            last_error = _CheckError(
                f"unexpected check error: {exc}",
                hint="Re-run with --log-level DEBUG and inspect the logs.",
            )
            logger.exception("unexpected health check error for %s", service.name)
            break

    assert last_error is not None
    return ServiceHealth(
        name=service.name,
        type=service.type,
        target=target,
        required=service.required,
        ok=False,
        status="failed",
        attempts=total_attempts if last_error else 0,
        latency_ms=round((clock() - started) * 1000, 1),
        detail=last_error.detail,
        status_code=last_error.status_code,
        hint=last_error.hint,
        checked_at=_now(),
    )


def check_services(
    services: Iterable[ServiceConfig],
    *,
    workers: int = 4,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ServiceHealth]:
    service_list = list(services)
    if not service_list:
        return []
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if dry_run or len(service_list) == 1 or workers == 1:
        return [
            check_service(service, dry_run=dry_run, sleep=sleep)
            for service in service_list
        ]
    with ThreadPoolExecutor(max_workers=min(workers, len(service_list))) as pool:
        return list(
            pool.map(
                lambda service: check_service(service, dry_run=dry_run, sleep=sleep),
                service_list,
            )
        )


def summarize(results: Iterable[ServiceHealth]) -> HealthSummary:
    items = list(results)
    planned = sum(1 for item in items if item.status == "planned")
    if planned:
        status = "planned"
    else:
        required_failures = sum(
            1 for item in items if item.required and not item.ok
        )
        optional_failures = sum(
            1 for item in items if not item.required and not item.ok
        )
        if required_failures:
            status = "unhealthy"
        elif optional_failures:
            status = "degraded"
        else:
            status = "healthy"

    return HealthSummary(
        status=status,
        total=len(items),
        healthy=sum(1 for item in items if item.ok),
        failed=sum(1 for item in items if item.status == "failed"),
        planned=planned,
        required=sum(1 for item in items if item.required),
        required_failures=sum(
            1 for item in items if item.required and not item.ok
        ),
        optional_failures=sum(
            1 for item in items if not item.required and not item.ok
        ),
    )


def _check_once(service: ServiceConfig) -> tuple[str, int | None]:
    if service.type == "http":
        return _check_http(service)
    if service.type == "tcp":
        return _check_tcp(service)
    if service.type == "command":
        return _check_command(service)
    raise _CheckError(f"unsupported service type: {service.type}")


def _check_http(service: ServiceConfig) -> tuple[str, int | None]:
    assert service.url is not None
    headers = {"User-Agent": USER_AGENT}
    headers.update(dict(service.headers))
    request = Request(service.url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=service.timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            response.read(256)
    except HTTPError as exc:
        status = int(exc.code)
        try:
            exc.read(256)
        except OSError:
            pass
        if status in service.expected_status:
            return f"HTTP {status} (expected)", status
        raise _CheckError(
            f"HTTP {status} {exc.reason}".strip(),
            hint=_http_status_hint(status),
            status_code=status,
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise _CheckError(
            f"timed out after {service.timeout_seconds:g}s",
            hint="Raise timeout_seconds or check network reachability.",
        ) from exc
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise _CheckError(
                f"timed out after {service.timeout_seconds:g}s",
                hint="Raise timeout_seconds or check network reachability.",
            ) from exc
        if isinstance(reason, socket.gaierror):
            raise _CheckError(
                f"DNS resolution failed: {reason}",
                hint="Check the hostname and DNS resolver configuration.",
            ) from exc
        if isinstance(reason, ConnectionRefusedError):
            raise _CheckError(
                f"connection refused: {reason}",
                hint="Confirm the service is listening on the configured host and port.",
            ) from exc
        raise _CheckError(
            f"connection error: {reason}",
            hint="Confirm the target is reachable from this host.",
        ) from exc
    except OSError as exc:
        raise _CheckError(
            f"connection error: {exc}",
            hint="Confirm the target is reachable from this host.",
        ) from exc

    if status not in service.expected_status:
        raise _CheckError(
            f"HTTP {status} (expected {_status_list(service.expected_status)})",
            hint=_http_status_hint(status),
            status_code=status,
        )
    return f"HTTP {status}", status


def _check_tcp(service: ServiceConfig) -> tuple[str, int | None]:
    assert service.host is not None and service.port is not None
    try:
        with socket.create_connection(
            (service.host, service.port), timeout=service.timeout_seconds
        ) as sock:
            sock.settimeout(service.timeout_seconds)
        return f"TCP connect to {service.host}:{service.port}", None
    except (TimeoutError, socket.timeout) as exc:
        raise _CheckError(
            f"TCP connect timed out after {service.timeout_seconds:g}s",
            hint="Check routing/firewall rules or raise timeout_seconds.",
        ) from exc
    except socket.gaierror as exc:
        raise _CheckError(
            f"DNS resolution failed: {exc}",
            hint="Check the hostname and DNS resolver configuration.",
        ) from exc
    except ConnectionRefusedError as exc:
        raise _CheckError(
            f"connection refused on {service.host}:{service.port}",
            hint="Confirm the service is listening on that host and port.",
        ) from exc
    except OSError as exc:
        raise _CheckError(
            f"TCP connection failed: {exc}",
            hint="Confirm the target is reachable from this host.",
        ) from exc


def _check_command(service: ServiceConfig) -> tuple[str, int | None]:
    assert service.command is not None
    try:
        completed = subprocess.run(
            list(service.command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=service.timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _CheckError(
            f"command not found: {service.command[0]}",
            hint="Install the command or use an absolute path.",
        ) from exc
    except PermissionError as exc:
        raise _CheckError(
            f"command is not executable: {service.command[0]}",
            hint="Check the file mode (chmod +x).",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise _CheckError(
            f"command timed out after {service.timeout_seconds:g}s",
            hint="Raise timeout_seconds or check why the command is hanging.",
        ) from exc
    except OSError as exc:
        raise _CheckError(
            f"could not run command: {exc}",
            hint="Check the executable path and permissions.",
        ) from exc

    if completed.returncode != service.expected_exit_code:
        output = (completed.stderr or completed.stdout or "").strip()
        output = " ".join(output.split())[:240]
        detail = f"exit code {completed.returncode} (expected {service.expected_exit_code})"
        if output:
            detail = f"{detail}: {output}"
        raise _CheckError(
            detail,
            hint="Inspect the service state or adjust expected_exit_code.",
        )
    return f"exit code {completed.returncode}", None


def _http_status_hint(status: int) -> str:
    if status in {401, 403}:
        return "Add credentials or use an unauthenticated health endpoint."
    if status == 404:
        return "Verify the health endpoint path in the service URL."
    if status == 429:
        return "The target is rate limiting probes; reduce probe frequency."
    if 500 <= status <= 599:
        return "The endpoint is reachable but the service is unhealthy; inspect its logs."
    if 300 <= status <= 399:
        return "The endpoint redirects; update the URL to the final health endpoint."
    return "Verify the expected_status values and the endpoint URL."


def _status_list(statuses: tuple[int, ...]) -> str:
    return ", ".join(str(status) for status in statuses)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
