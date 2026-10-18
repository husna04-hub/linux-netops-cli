"""Run result model and Markdown/JSON report rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import __version__
from .config import Config
from .diagnostics import Collection, collection_status
from .errors import OperationalError
from .health import HealthSummary, ServiceHealth, summarize


@dataclass(frozen=True)
class RunResult:
    generated_at: str
    environment: str
    config_path: str
    title: str
    status: str
    duration_ms: float
    health: HealthSummary
    services: tuple[ServiceHealth, ...]
    collections: tuple[Collection, ...]
    config_summary: Mapping[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    netops_version: str = __version__

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "netops_version": self.netops_version,
            "generated_at": self.generated_at,
            "environment": self.environment,
            "config_path": self.config_path,
            "title": self.title,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "dry_run": self.dry_run,
            "health": self.health.to_dict(),
            "services": [item.to_dict() for item in self.services],
            "collections": [item.to_dict() for item in self.collections],
            "config": dict(self.config_summary),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunResult":
        try:
            health = HealthSummary(**payload["health"])
            services = tuple(
                ServiceHealth(**item) for item in payload.get("services", [])
            )
            collections = tuple(
                Collection(**item) for item in payload.get("collections", [])
            )
            return cls(
                generated_at=str(payload["generated_at"]),
                environment=str(payload["environment"]),
                config_path=str(payload["config_path"]),
                title=str(payload.get("title", "Network Operations Report")),
                status=str(payload["status"]),
                duration_ms=float(payload.get("duration_ms", 0.0)),
                health=health,
                services=services,
                collections=collections,
                config_summary=dict(payload.get("config", {})),
                dry_run=bool(payload.get("dry_run", False)),
                netops_version=str(payload.get("netops_version", __version__)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationalError(
                f"run file is not a valid netops result: {exc}",
                hint="Pass the run.json written by 'netops run'.",
            ) from exc


def build_result(
    config: Config,
    services: Iterable[ServiceHealth],
    collections: Iterable[Collection],
    *,
    duration_ms: float,
    dry_run: bool = False,
) -> RunResult:
    service_list = tuple(services)
    collection_list = tuple(collections)
    health = summarize(service_list)
    status = combine_status(health.status, collection_status(collection_list))
    return RunResult(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        environment=config.environment,
        config_path=str(config.path),
        title=config.report_title,
        status=status,
        duration_ms=round(duration_ms, 1),
        health=health,
        services=service_list,
        collections=collection_list,
        config_summary=config.summary(),
        dry_run=dry_run,
    )


def combine_status(health_status: str, collection: str) -> str:
    if health_status == "planned":
        return "planned"
    if health_status == "unhealthy" or collection == "unhealthy":
        return "unhealthy"
    if health_status == "degraded" or collection == "degraded":
        return "degraded"
    return "healthy"


def status_exit_code(status: str) -> int:
    if status in {"healthy", "planned"}:
        return 0
    if status == "degraded":
        return 1
    return 3


def render_markdown(result: RunResult) -> str:
    lines: list[str] = []
    lines.append(f"# {result.title}")
    lines.append("")
    lines.append(
        f"**Environment:** `{result.environment}` &nbsp;|&nbsp; "
        f"**Generated:** {result.generated_at} &nbsp;|&nbsp; "
        f"**Overall status:** `{result.status.upper()}`"
    )
    lines.append("")
    if result.dry_run:
        lines.append(
            "> Dry run: no network calls were made and no diagnostics were written."
        )
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Health status | `{result.health.status}` |")
    lines.append(
        f"| Services | {result.health.healthy} healthy / "
        f"{result.health.failed} failed / {result.health.total} total |"
    )
    lines.append(
        f"| Required services | {result.health.required - result.health.required_failures}"
        f" / {result.health.required} passing |"
    )
    lines.append(
        f"| Diagnostic sources | {sum(1 for item in result.collections if item.ok)}"
        f" / {len(result.collections)} collected |"
    )
    lines.append(f"| Run duration | {result.duration_ms:g} ms |")
    lines.append(f"| Configuration | `{result.config_path}` |")
    lines.append("")

    lines.append("## Health checks")
    lines.append("")
    if not result.services:
        lines.append("_No services were checked._")
    else:
        lines.append(
            "| Service | Type | Target | Required | Status | Attempts | Latency | Detail |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for service in result.services:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(service.name),
                        _cell(service.type),
                        _cell(service.target),
                        "yes" if service.required else "no",
                        f"`{service.status}`",
                        str(service.attempts),
                        f"{service.latency_ms:g} ms",
                        _cell(service.detail or "-"),
                    ]
                )
                + " |"
            )
    lines.append("")

    lines.append("## Diagnostics")
    lines.append("")
    if not result.collections:
        lines.append("_No diagnostic sources were configured for this run._")
    else:
        lines.append(
            "| Source | Type | Target | Required | Status | Lines | Artifact |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for collection in result.collections:
            artifact = (
                f"[`diagnostics/{collection.artifact}`]"
                f"(diagnostics/{collection.artifact})"
                if collection.artifact
                else "-"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(collection.name),
                        _cell(collection.type),
                        _cell(collection.target),
                        "yes" if collection.required else "no",
                        f"`{collection.status}`",
                        str(collection.line_count),
                        artifact,
                    ]
                )
                + " |"
            )
    lines.append("")

    findings = _findings(result)
    lines.append("## Findings and next actions")
    lines.append("")
    if not findings:
        lines.append("No action required: all required checks passed.")
    else:
        for finding in findings:
            lines.append(f"- {finding}")
    lines.append("")

    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"- netops version: `{result.netops_version}`")
    lines.append(f"- Result schema: `1`")
    lines.append(f"- Environment: `{result.environment}`")
    lines.append(f"- Config: `{result.config_path}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Generated by netops. Review diagnostic artifacts before sharing._")
    lines.append("")
    return "\n".join(lines)


def write_report(result: RunResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(result), encoding="utf-8")
    return path


def write_run_result(result: RunResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def load_run_result(path: Path) -> RunResult:
    if not path.exists():
        raise OperationalError(
            f"run file not found: {path}",
            hint="Pass the run.json written by 'netops run'.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationalError(
            f"could not read run file {path}: {exc}",
            hint="Check that the file is valid UTF-8 JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise OperationalError(
            f"run file must contain a JSON object: {path}",
            hint="Pass the run.json written by 'netops run'.",
        )
    return RunResult.from_dict(payload)


def _findings(result: RunResult) -> list[str]:
    findings: list[str] = []
    required_health = [
        item for item in result.services if item.required and not item.ok
    ]
    optional_health = [
        item for item in result.services if not item.required and not item.ok
    ]
    required_collections = [
        item for item in result.collections if item.required and not item.ok
    ]
    optional_collections = [
        item for item in result.collections if not item.required and not item.ok
    ]

    for service in required_health:
        findings.append(
            f"**Required service `{service.name}` failed** after "
            f"{service.attempts} attempt(s): {service.detail}."
            + (f" _Hint: {service.hint}_" if service.hint else "")
        )
    for collection in required_collections:
        findings.append(
            f"**Required diagnostic source `{collection.name}` failed**: "
            f"{collection.error}."
            + (f" _Hint: {collection.hint}_" if collection.hint else "")
        )
    for service in optional_health:
        findings.append(
            f"Optional service `{service.name}` failed: {service.detail}."
            + (f" _Hint: {service.hint}_" if service.hint else "")
        )
    for collection in optional_collections:
        findings.append(
            f"Optional diagnostic source `{collection.name}` failed: "
            f"{collection.error}."
            + (f" _Hint: {collection.hint}_" if collection.hint else "")
        )
    return findings


def _cell(value: object, max_length: int = 120) -> str:
    text = " ".join(str(value).split())
    if len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return text.replace("|", "\\|")
