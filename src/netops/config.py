"""Configuration loading and strict validation.

Configuration is intentionally validated before any network call or file read.
Errors point at the exact YAML path and include a hint, which makes a failed
pipeline run actionable instead of a stack trace.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml
from yaml import YAMLError

from .errors import ConfigError

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

ROOT_KEYS = {
    "version",
    "environment",
    "description",
    "defaults",
    "services",
    "log_sources",
    "report",
}
DEFAULTS_KEYS = {
    "timeout_seconds",
    "retries",
    "backoff_seconds",
    "max_backoff_seconds",
}
SERVICE_KEYS = {
    "name",
    "type",
    "description",
    "required",
    "tags",
    "timeout_seconds",
    "retries",
    "backoff_seconds",
    "max_backoff_seconds",
    "url",
    "expected_status",
    "headers",
    "host",
    "port",
    "command",
    "expected_exit_code",
}
LOG_SOURCE_KEYS = {
    "name",
    "type",
    "required",
    "path",
    "unit",
    "since",
    "command",
    "max_lines",
    "timeout_seconds",
}
REPORT_KEYS = {"title"}


@dataclass(frozen=True)
class RetrySettings:
    retries: int = 2
    backoff_seconds: float = 0.25
    max_backoff_seconds: float = 2.0

    def delay_for(self, attempt: int) -> float:
        """Delay after a failed 1-based attempt, capped to stay bounded."""
        if self.backoff_seconds <= 0:
            return 0.0
        return min(
            self.backoff_seconds * (2 ** max(attempt - 1, 0)),
            self.max_backoff_seconds,
        )


@dataclass(frozen=True)
class Defaults:
    timeout_seconds: float = 2.0
    retries: int = 2
    backoff_seconds: float = 0.25
    max_backoff_seconds: float = 2.0

    def retry_settings(
        self,
        retries: int | None = None,
        backoff_seconds: float | None = None,
        max_backoff_seconds: float | None = None,
    ) -> RetrySettings:
        return RetrySettings(
            retries=self.retries if retries is None else retries,
            backoff_seconds=(
                self.backoff_seconds if backoff_seconds is None else backoff_seconds
            ),
            max_backoff_seconds=(
                self.max_backoff_seconds
                if max_backoff_seconds is None
                else max_backoff_seconds
            ),
        )


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    type: str
    required: bool = True
    description: str = ""
    tags: tuple[str, ...] = ()
    timeout_seconds: float = 2.0
    retry: RetrySettings = field(default_factory=RetrySettings)
    url: str | None = None
    expected_status: tuple[int, ...] = (200,)
    headers: Mapping[str, str] = field(default_factory=dict)
    host: str | None = None
    port: int | None = None
    command: tuple[str, ...] | None = None
    expected_exit_code: int = 0


@dataclass(frozen=True)
class LogSourceConfig:
    name: str
    type: str
    required: bool = True
    path: str | None = None
    resolved_path: Path | None = None
    unit: str | None = None
    since: str | None = None
    command: tuple[str, ...] | None = None
    max_lines: int = 200
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class Config:
    path: Path
    version: int
    environment: str
    description: str
    defaults: Defaults
    services: tuple[ServiceConfig, ...]
    log_sources: tuple[LogSourceConfig, ...]
    report_title: str = "Network Operations Report"
    warnings: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def required_services(self) -> tuple[ServiceConfig, ...]:
        return tuple(service for service in self.services if service.required)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "version": self.version,
            "environment": self.environment,
            "description": self.description,
            "services": len(self.services),
            "required_services": len(self.required_services),
            "log_sources": len(self.log_sources),
            "service_names": [service.name for service in self.services],
            "warnings": list(self.warnings),
        }


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML configuration file."""
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError(
            f"configuration file not found: {config_path}",
            hint="Pass --config with a path to an existing YAML file.",
        )
    if not config_path.is_file():
        raise ConfigError(
            f"configuration path is not a file: {config_path}",
            hint="Point --config at a YAML file, not a directory.",
        )
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"could not read configuration {config_path}: {exc}",
            hint="Check file permissions and that the file is valid UTF-8.",
        ) from exc

    try:
        data = yaml.safe_load(text)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        )
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(
            f"invalid YAML{location}: {problem}",
            hint="Fix the YAML syntax (indentation and quoting are common causes).",
        ) from exc

    return parse_config(data, config_path.resolve())


def parse_config(data: Any, source_path: Path) -> Config:
    """Validate an already-decoded configuration mapping."""
    root = _mapping(data, "configuration")
    _check_keys(root, ROOT_KEYS, "configuration")

    version = _integer(root.get("version"), "version", minimum=1, maximum=1)
    environment = _string(
        root.get("environment"), "environment", pattern=_NAME_RE, pattern_hint="letters, digits, dot, dash, underscore"
    )
    description = _string(
        root.get("description", ""), "description", allow_empty=True, max_length=500
    )

    raw_defaults = _mapping(root.get("defaults", {}), "defaults")
    _check_keys(raw_defaults, DEFAULTS_KEYS, "defaults")
    defaults = Defaults(
        timeout_seconds=_number(
            raw_defaults.get("timeout_seconds", 2.0),
            "defaults.timeout_seconds",
            minimum=0.05,
            maximum=300.0,
        ),
        retries=_integer(
            raw_defaults.get("retries", 2),
            "defaults.retries",
            minimum=0,
            maximum=10,
        ),
        backoff_seconds=_number(
            raw_defaults.get("backoff_seconds", 0.25),
            "defaults.backoff_seconds",
            minimum=0.0,
            maximum=60.0,
        ),
        max_backoff_seconds=_number(
            raw_defaults.get("max_backoff_seconds", 2.0),
            "defaults.max_backoff_seconds",
            minimum=0.0,
            maximum=300.0,
        ),
    )
    if defaults.max_backoff_seconds < defaults.backoff_seconds:
        raise ConfigError(
            "defaults.max_backoff_seconds must be greater than or equal to "
            "defaults.backoff_seconds",
            hint="Set a cap that is at least the initial backoff.",
        )

    raw_services = root.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise ConfigError(
            "services must be a non-empty list",
            hint="Add at least one HTTP, TCP, or command health check.",
        )

    services: list[ServiceConfig] = []
    seen_services: set[str] = set()
    for index, item in enumerate(raw_services):
        service = _parse_service(item, index, defaults)
        if service.name in seen_services:
            raise ConfigError(
                f"duplicate service name: {service.name}",
                hint="Service names must be unique; rename one of the entries.",
            )
        seen_services.add(service.name)
        services.append(service)

    raw_log_sources = root.get("log_sources", [])
    if raw_log_sources is None:
        raw_log_sources = []
    if not isinstance(raw_log_sources, list):
        raise ConfigError(
            "log_sources must be a list",
            hint="Use a YAML list, or remove the key if nothing should be collected.",
        )

    log_sources: list[LogSourceConfig] = []
    seen_sources: set[str] = set()
    for index, item in enumerate(raw_log_sources):
        source = _parse_log_source(item, index, source_path)
        if source.name in seen_sources:
            raise ConfigError(
                f"duplicate log source name: {source.name}",
                hint="Log source names must be unique; rename one of the entries.",
            )
        seen_sources.add(source.name)
        log_sources.append(source)

    raw_report = _mapping(root.get("report", {}), "report")
    _check_keys(raw_report, REPORT_KEYS, "report")
    report_title = _string(
        raw_report.get("title", "Network Operations Report"),
        "report.title",
        max_length=200,
    )

    warnings: list[str] = []
    if not log_sources:
        warnings.append(
            "no log_sources configured; diagnostic collection will be empty"
        )
    if not any(service.required for service in services):
        warnings.append(
            "all services are optional; the run can never report 'unhealthy'"
        )

    return Config(
        path=source_path,
        version=version,
        environment=environment,
        description=description,
        defaults=defaults,
        services=tuple(services),
        log_sources=tuple(log_sources),
        report_title=report_title,
        warnings=tuple(warnings),
        raw=root,
    )


def _parse_service(
    item: Any, index: int, defaults: Defaults
) -> ServiceConfig:
    where = f"services[{index}]"
    data = _mapping(item, where)
    _check_keys(data, SERVICE_KEYS, where)

    name = _string(
        data.get("name"),
        f"{where}.name",
        pattern=_NAME_RE,
        pattern_hint="letters, digits, dot, dash, underscore",
    )
    service_type = _string(data.get("type"), f"{where}.type").lower()
    if service_type not in {"http", "tcp", "command"}:
        raise ConfigError(
            f"{where}.type must be one of: http, tcp, command",
            hint=f"Got {service_type!r}; choose a supported check type.",
        )

    timeout_seconds = _number(
        data.get("timeout_seconds", defaults.timeout_seconds),
        f"{where}.timeout_seconds",
        minimum=0.05,
        maximum=300.0,
    )
    retries = _integer(
        data.get("retries", defaults.retries),
        f"{where}.retries",
        minimum=0,
        maximum=10,
    )
    backoff = _number(
        data.get("backoff_seconds", defaults.backoff_seconds),
        f"{where}.backoff_seconds",
        minimum=0.0,
        maximum=60.0,
    )
    max_backoff = _number(
        data.get("max_backoff_seconds", defaults.max_backoff_seconds),
        f"{where}.max_backoff_seconds",
        minimum=0.0,
        maximum=300.0,
    )
    if max_backoff < backoff:
        raise ConfigError(
            f"{where}.max_backoff_seconds must be >= {where}.backoff_seconds",
            hint="Lower the initial backoff or raise the cap.",
        )

    required = _boolean(data.get("required", True), f"{where}.required")
    description = _string(
        data.get("description", ""),
        f"{where}.description",
        allow_empty=True,
        max_length=500,
    )
    tags = _string_list(data.get("tags", []), f"{where}.tags")

    common = dict(
        name=name,
        type=service_type,
        required=required,
        description=description,
        tags=tags,
        timeout_seconds=timeout_seconds,
        retry=defaults.retry_settings(retries, backoff, max_backoff),
    )

    if service_type == "http":
        _reject_keys(
            data,
            where,
            ["host", "port", "command", "expected_exit_code"],
            service_type,
        )
        url = _string(data.get("url"), f"{where}.url", max_length=2048)
        _validate_http_url(url, f"{where}.url")
        expected_status = _expected_status(
            data.get("expected_status", 200), f"{where}.expected_status"
        )
        headers = _headers(data.get("headers", {}), f"{where}.headers")
        return ServiceConfig(
            **common,
            url=url,
            expected_status=expected_status,
            headers=headers,
        )

    if service_type == "tcp":
        _reject_keys(
            data,
            where,
            ["url", "expected_status", "headers", "command", "expected_exit_code"],
            service_type,
        )
        host = _string(data.get("host"), f"{where}.host", max_length=253)
        port = _integer(data.get("port"), f"{where}.port", minimum=1, maximum=65535)
        return ServiceConfig(**common, host=host, port=port)

    _reject_keys(
        data,
        where,
        ["url", "expected_status", "headers", "host", "port"],
        service_type,
    )
    command = _command(data.get("command"), f"{where}.command")
    expected_exit = _integer(
        data.get("expected_exit_code", 0),
        f"{where}.expected_exit_code",
        minimum=-255,
        maximum=255,
    )
    return ServiceConfig(
        **common, command=command, expected_exit_code=expected_exit
    )


def _parse_log_source(
    item: Any, index: int, source_path: Path
) -> LogSourceConfig:
    where = f"log_sources[{index}]"
    data = _mapping(item, where)
    _check_keys(data, LOG_SOURCE_KEYS, where)

    name = _string(
        data.get("name"),
        f"{where}.name",
        pattern=_NAME_RE,
        pattern_hint="letters, digits, dot, dash, underscore",
    )
    source_type = _string(data.get("type"), f"{where}.type").lower()
    if source_type not in {"file", "journalctl", "command"}:
        raise ConfigError(
            f"{where}.type must be one of: file, journalctl, command",
            hint=f"Got {source_type!r}; choose a supported log source type.",
        )

    required = _boolean(data.get("required", True), f"{where}.required")
    max_lines = _integer(
        data.get("max_lines", 200), f"{where}.max_lines", minimum=1, maximum=10000
    )
    timeout_seconds = _number(
        data.get("timeout_seconds", 10.0),
        f"{where}.timeout_seconds",
        minimum=0.05,
        maximum=300.0,
    )

    common = dict(
        name=name,
        type=source_type,
        required=required,
        max_lines=max_lines,
        timeout_seconds=timeout_seconds,
    )

    if source_type == "file":
        _reject_keys(data, where, ["unit", "since", "command"], source_type)
        raw_path = _string(data.get("path"), f"{where}.path", max_length=4096)
        if "\x00" in raw_path:
            raise ConfigError(
                f"{where}.path contains a NUL byte",
                hint="Use a normal filesystem path.",
            )
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = source_path.parent / candidate
        return LogSourceConfig(
            **common,
            path=raw_path,
            resolved_path=candidate.resolve(),
        )

    if source_type == "journalctl":
        _reject_keys(data, where, ["path", "command"], source_type)
        unit = _string(data.get("unit"), f"{where}.unit", max_length=256)
        since = data.get("since")
        if since is not None:
            since = _string(since, f"{where}.since", max_length=128)
        return LogSourceConfig(**common, unit=unit, since=since)

    _reject_keys(data, where, ["path", "unit", "since"], source_type)
    command = _command(data.get("command"), f"{where}.command")
    return LogSourceConfig(**common, command=command)


def _validate_http_url(url: str, where: str) -> None:
    if any(char in url for char in "\r\n"):
        raise ConfigError(
            f"{where} contains a newline",
            hint="URLs must be a single line.",
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ConfigError(
            f"{where} is not a valid URL: {exc}",
            hint="Use a URL such as http://127.0.0.1:8080/healthz.",
        ) from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise ConfigError(
            f"{where} must use http or https",
            hint="Example: http://127.0.0.1:8080/healthz.",
        )
    if not parts.hostname:
        raise ConfigError(
            f"{where} must include a hostname",
            hint="Example: http://127.0.0.1:8080/healthz.",
        )
    if parts.username or parts.password:
        raise ConfigError(
            f"{where} must not embed credentials",
            hint="Remove user:password from the URL; use an authenticated endpoint without logging secrets.",
        )
    if parts.fragment:
        raise ConfigError(
            f"{where} must not contain a URL fragment",
            hint="Remove the #... suffix; fragments are not sent to servers.",
        )
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(
            f"{where} has an invalid port",
            hint="Use a port between 1 and 65535.",
        )


def _expected_status(value: Any, where: str) -> tuple[int, ...]:
    if isinstance(value, list):
        if not value:
            raise ConfigError(
                f"{where} must not be an empty list",
                hint="Provide at least one expected HTTP status code.",
            )
        values = tuple(
            _integer(item, f"{where}[{index}]", minimum=100, maximum=599)
            for index, item in enumerate(value)
        )
    else:
        values = (_integer(value, where, minimum=100, maximum=599),)
    return values


def _headers(value: Any, where: str) -> dict[str, str]:
    data = _mapping(value, where)
    headers: dict[str, str] = {}
    for key, raw_value in data.items():
        if not isinstance(key, str) or not _HEADER_NAME_RE.match(key):
            raise ConfigError(
                f"{where} has an invalid header name: {key!r}",
                hint="Header names may contain letters, digits, and RFC 7230 token characters.",
            )
        if not isinstance(raw_value, str):
            raise ConfigError(
                f"{where}.{key} must be a string",
                hint="Quote header values in YAML.",
            )
        if any(char in raw_value for char in "\r\n\x00"):
            raise ConfigError(
                f"{where}.{key} contains a control character",
                hint="Header values must be a single line without NUL bytes.",
            )
        headers[key] = raw_value
    return headers


def _command(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(
            f"{where} must be a non-empty list of arguments",
            hint='Use an argv list such as ["systemctl", "is-active", "nginx"].',
        )
    args: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(
                f"{where}[{index}] must be a string",
                hint="Every command argument must be a scalar string.",
            )
        if "\x00" in item:
            raise ConfigError(
                f"{where}[{index}] contains a NUL byte",
                hint="Remove control characters from the command.",
            )
        args.append(item)
    return tuple(args)


def _reject_keys(
    data: Mapping[str, Any], where: str, keys: list[str], service_type: str
) -> None:
    present = [key for key in keys if key in data]
    if present:
        raise ConfigError(
            f"{where} has key(s) not valid for type {service_type!r}: "
            + ", ".join(present),
            hint="Remove the incompatible keys or change the check type.",
        )


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"{where} must be a mapping",
            hint=f"Use YAML key/value pairs for {where}.",
        )
    return value


def _check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) at {where}: " + ", ".join(str(key) for key in unknown),
            hint="Remove the key(s) or check the configuration reference in README.md.",
        )


def _string(
    value: Any,
    where: str,
    *,
    allow_empty: bool = False,
    max_length: int = 500,
    pattern: re.Pattern[str] | None = None,
    pattern_hint: str = "",
) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            f"{where} must be a string",
            hint="Quote values that contain YAML-special characters.",
        )
    if not allow_empty and not value.strip():
        raise ConfigError(
            f"{where} must not be empty",
            hint="Provide a non-empty string.",
        )
    if len(value) > max_length:
        raise ConfigError(
            f"{where} is too long ({len(value)} > {max_length} characters)",
            hint="Shorten the value.",
        )
    if pattern is not None and not pattern.match(value):
        raise ConfigError(
            f"{where} has invalid characters: {value!r}",
            hint=f"Use only {pattern_hint}.",
        )
    return value


def _string_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            f"{where} must be a list of strings",
            hint="Example: tags: [edge, api].",
        )
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, f"{where}[{index}]", max_length=64))
    return tuple(result)


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"{where} must be true or false",
            hint="YAML booleans are unquoted true/false.",
        )
    return value


def _integer(
    value: Any, where: str, *, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{where} must be an integer",
            hint=f"Use a whole number between {minimum} and {maximum}.",
        )
    if not minimum <= value <= maximum:
        raise ConfigError(
            f"{where} must be between {minimum} and {maximum}",
            hint=f"Got {value}.",
        )
    return value


def _number(
    value: Any, where: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{where} must be a number",
            hint=f"Use a value between {minimum} and {maximum}.",
        )
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(
            f"{where} must be finite",
            hint="Use a normal decimal value.",
        )
    if not minimum <= number <= maximum:
        raise ConfigError(
            f"{where} must be between {minimum} and {maximum}",
            hint=f"Got {number}.",
        )
    return number
