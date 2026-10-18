"""Bounded diagnostic collection with redaction and per-source attribution."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import LogSourceConfig
from .errors import OperationalError

MAX_CAPTURE_BYTES = 512 * 1024
_READ_CHUNK = 8192
_TAIL_READ_LIMIT = 4 * 1024 * 1024

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)([^\s]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)((?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*)"
            r"([^\s,;\"']+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)(https?://)([^/\s:@]+):([^@\s/]+)@"),
        r"\1[REDACTED]@",
    ),
)


@dataclass(frozen=True)
class Collection:
    name: str
    type: str
    target: str
    required: bool
    ok: bool
    status: str
    duration_ms: float
    line_count: int
    bytes: int
    truncated: bool
    artifact: str | None = None
    content: str = ""
    error: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "type": self.type,
            "target": self.target,
            "required": self.required,
            "ok": self.ok,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "line_count": self.line_count,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "artifact": self.artifact,
            "error": self.error,
            "hint": self.hint,
        }


def target_for(source: LogSourceConfig) -> str:
    if source.type == "file":
        return str(source.resolved_path or source.path or "<missing-path>")
    if source.type == "journalctl":
        return f"journalctl -u {source.unit}"
    if source.command:
        return " ".join(source.command)
    return "<missing-command>"


def collect_source(source: LogSourceConfig) -> Collection:
    """Collect one source. Expected failures become data, not exceptions."""
    started = time.perf_counter()
    target = target_for(source)
    try:
        if source.type == "file":
            raw, truncated = _collect_file(source)
        elif source.type == "journalctl":
            raw, truncated = _collect_journalctl(source)
        elif source.type == "command":
            raw, truncated = _collect_command(source)
        else:
            raise OperationalError(
                f"unsupported log source type: {source.type}",
                hint="Use file, journalctl, or command.",
            )
        content = redact_text(raw)
        content, limited = _limit_output(content, source.max_lines)
        truncated = truncated or limited
        return Collection(
            name=source.name,
            type=source.type,
            target=target,
            required=source.required,
            ok=True,
            status="collected",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            line_count=len(content.splitlines()),
            bytes=len(content.encode("utf-8")),
            truncated=truncated,
            content=content,
        )
    except OperationalError as exc:
        return Collection(
            name=source.name,
            type=source.type,
            target=target,
            required=source.required,
            ok=False,
            status="failed",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            line_count=0,
            bytes=0,
            truncated=False,
            error=exc.message,
            hint=exc.hint,
        )
    except Exception as exc:  # pragma: no cover - defensive collection boundary
        return Collection(
            name=source.name,
            type=source.type,
            target=target,
            required=source.required,
            ok=False,
            status="failed",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            line_count=0,
            bytes=0,
            truncated=False,
            error=f"unexpected collection error: {exc}",
            hint="Re-run with --log-level DEBUG and inspect the logs.",
        )


def collect_sources(
    sources: Iterable[LogSourceConfig],
    output_dir: Path | None = None,
    *,
    dry_run: bool = False,
) -> list[Collection]:
    source_list = list(sources)
    if dry_run:
        return [
            Collection(
                name=source.name,
                type=source.type,
                target=target_for(source),
                required=source.required,
                ok=False,
                status="planned",
                duration_ms=0.0,
                line_count=0,
                bytes=0,
                truncated=False,
                artifact=f"{sanitize_filename(source.name)}.log",
            )
            for source in source_list
        ]

    if output_dir is None:
        raise ValueError("output_dir is required when not in dry-run mode")
    output_dir.mkdir(parents=True, exist_ok=True)

    collections: list[Collection] = []
    for source in source_list:
        result = collect_source(source)
        artifact_name = f"{sanitize_filename(source.name)}.log"
        if result.content:
            artifact_path = output_dir / artifact_name
            try:
                artifact_path.write_text(result.content, encoding="utf-8")
            except OSError as exc:
                result = Collection(
                    name=result.name,
                    type=result.type,
                    target=result.target,
                    required=result.required,
                    ok=False,
                    status="failed",
                    duration_ms=result.duration_ms,
                    line_count=0,
                    bytes=0,
                    truncated=result.truncated,
                    error=f"could not write artifact {artifact_path}: {exc}",
                    hint="Check the output directory permissions.",
                )
                artifact_name = ""
            else:
                result = Collection(
                    name=result.name,
                    type=result.type,
                    target=result.target,
                    required=result.required,
                    ok=result.ok,
                    status=result.status,
                    duration_ms=result.duration_ms,
                    line_count=result.line_count,
                    bytes=result.bytes,
                    truncated=result.truncated,
                    artifact=artifact_name,
                    content=result.content,
                    error=result.error,
                    hint=result.hint,
                )
        collections.append(result)
    return collections


def write_manifest(output_dir: Path, collections: Iterable[Collection]) -> Path:
    entries = [item.to_dict() for item in collections]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(entries),
        "collections": entries,
    }
    path = output_dir / "manifest.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def collection_status(collections: Iterable[Collection]) -> str:
    items = list(collections)
    if not items or all(item.ok for item in items):
        return "healthy"
    if any(item.required and not item.ok for item in items):
        return "unhealthy"
    return "degraded"


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:80] or "source"


def redact_text(text: str) -> str:
    redacted = text
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _collect_file(source: LogSourceConfig) -> tuple[str, bool]:
    path = source.resolved_path
    if path is None:
        raise OperationalError(
            "file source has no resolved path",
            hint="Set log_sources[].path.",
        )
    if not path.exists():
        raise OperationalError(
            f"log file not found: {path}",
            hint="Check the path or mark this source required: false.",
        )
    if not path.is_file():
        raise OperationalError(
            f"log path is not a regular file: {path}",
            hint="Point the source at a file, not a directory.",
        )
    try:
        content, truncated = _tail(path, source.max_lines)
    except OSError as exc:
        raise OperationalError(
            f"could not read log file {path}: {exc}",
            hint="Check file permissions (the CLI must be able to read it).",
        ) from exc
    return content, truncated


def _collect_journalctl(source: LogSourceConfig) -> tuple[str, bool]:
    if not source.unit:
        raise OperationalError(
            "journalctl source has no unit",
            hint="Set log_sources[].unit to a systemd unit name.",
        )
    command = [
        "journalctl",
        "--no-pager",
        "--output=short-iso",
        "--unit",
        source.unit,
        "--lines",
        str(source.max_lines),
    ]
    if source.since:
        command.extend(["--since", source.since])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=source.timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperationalError(
            "journalctl is not installed on this host",
            hint="Journal sources require systemd; mark this source required: false on non-systemd hosts.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OperationalError(
            f"journalctl timed out after {source.timeout_seconds:g}s",
            hint="Lower max_lines, narrow --since, or raise timeout_seconds.",
        ) from exc
    except OSError as exc:
        raise OperationalError(
            f"could not run journalctl: {exc}",
            hint="Check that journalctl is executable.",
        ) from exc

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise OperationalError(
            f"journalctl exited {completed.returncode}: {message[:300]}",
            hint="Check the unit name and journal permissions (sudo may be required).",
        )
    return completed.stdout, False


def _collect_command(source: LogSourceConfig) -> tuple[str, bool]:
    if not source.command:
        raise OperationalError(
            "command source has no command",
            hint="Set log_sources[].command as an argv list.",
        )
    try:
        completed = subprocess.run(
            list(source.command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=source.timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperationalError(
            f"diagnostic command not found: {source.command[0]}",
            hint="Install the command or use an absolute path.",
        ) from exc
    except PermissionError as exc:
        raise OperationalError(
            f"diagnostic command is not executable: {source.command[0]}",
            hint="Check file permissions (chmod +x).",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OperationalError(
            f"diagnostic command timed out after {source.timeout_seconds:g}s",
            hint="Raise timeout_seconds or simplify the command.",
        ) from exc
    except OSError as exc:
        raise OperationalError(
            f"could not run diagnostic command: {exc}",
            hint="Check the executable path and permissions.",
        ) from exc

    output = completed.stdout
    if completed.stderr:
        output = f"{output}\n[stderr]\n{completed.stderr}" if output else completed.stderr
    if completed.returncode != 0:
        raise OperationalError(
            f"diagnostic command exited {completed.returncode}: "
            f"{' '.join(output.split())[:300]}",
            hint="The command ran but reported failure; inspect its output.",
        )
    return output, False


def _tail(path: Path, lines: int) -> tuple[str, bool]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        total_read = 0
        chunks: list[bytes] = []
        newlines = 0
        while position > 0 and newlines <= lines:
            size = min(_READ_CHUNK, position)
            position -= size
            handle.seek(position)
            chunk = handle.read(size)
            chunks.append(chunk)
            total_read += size
            newlines += chunk.count(b"\n")
            if total_read >= _TAIL_READ_LIMIT:
                break
    data = b"".join(reversed(chunks))
    text = data.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    truncated = len(all_lines) > lines or position > 0
    return "\n".join(all_lines[-lines:]), truncated


def _limit_output(text: str, max_lines: int) -> tuple[str, bool]:
    if len(text.encode("utf-8")) > MAX_CAPTURE_BYTES:
        encoded = text.encode("utf-8")[-MAX_CAPTURE_BYTES:]
        text = encoded.decode("utf-8", errors="replace")
        truncated = True
    else:
        truncated = False
    lines = text.splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[-max_lines:]), True
    return text, truncated
