from __future__ import annotations

import json
import sys
from pathlib import Path

from netops.config import LogSourceConfig
from netops.diagnostics import (
    collect_source,
    collect_sources,
    collection_status,
    redact_text,
    sanitize_filename,
    write_manifest,
)


def file_source(path: Path, **overrides) -> LogSourceConfig:
    values = dict(
        name="app",
        type="file",
        required=True,
        path=str(path),
        resolved_path=path,
        max_lines=200,
    )
    values.update(overrides)
    return LogSourceConfig(**values)


def test_file_collection_tails_and_redacts(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text(
        "one\n"
        "two\n"
        "Authorization: Bearer super-secret\n"
        "password=hunter2\n"
        "five\n",
        encoding="utf-8",
    )
    result = collect_source(file_source(log, max_lines=4))
    assert result.ok is True
    assert "one" not in result.content
    assert "five" in result.content
    assert "super-secret" not in result.content
    assert "[REDACTED]" in result.content
    assert "hunter2" not in result.content


def test_missing_file_is_actionable(tmp_path: Path) -> None:
    result = collect_source(file_source(tmp_path / "missing.log"))
    assert result.ok is False
    assert result.status == "failed"
    assert "not found" in (result.error or "")
    assert result.hint


def test_command_collection_success(tmp_path: Path) -> None:
    result = collect_source(
        LogSourceConfig(
            name="host",
            type="command",
            command=(sys.executable, "-c", "print('kernel: ok')"),
            max_lines=10,
        )
    )
    assert result.ok is True
    assert "kernel: ok" in result.content


def test_command_collection_nonzero_exit_fails() -> None:
    result = collect_source(
        LogSourceConfig(
            name="host",
            type="command",
            command=(sys.executable, "-c", "import sys; print('bad'); sys.exit(2)"),
            max_lines=10,
        )
    )
    assert result.ok is False
    assert "exited 2" in (result.error or "")
    assert result.hint


def test_collect_sources_writes_artifacts_and_manifest(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("alpha\nbeta\n", encoding="utf-8")
    out = tmp_path / "out"
    results = collect_sources([file_source(log)], output_dir=out)
    assert results[0].artifact == "app.log"
    assert (out / "app.log").read_text(encoding="utf-8") == "alpha\nbeta"
    manifest = write_manifest(out, results)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["collections"][0]["name"] == "app"
    assert payload["collections"][0]["artifact"] == "app.log"


def test_collect_sources_dry_run_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    results = collect_sources(
        [file_source(tmp_path / "missing.log")], output_dir=out, dry_run=True
    )
    assert results[0].status == "planned"
    assert results[0].artifact == "app.log"
    assert not out.exists()


def test_collection_status() -> None:
    assert collection_status([]) == "healthy"
    ok = collect_source(
        LogSourceConfig(
            name="x",
            type="command",
            command=(sys.executable, "-c", "print('ok')"),
        )
    )
    failed_required = collect_source(file_source(Path("missing-file-xyz.log")))
    failed_optional = collect_source(
        file_source(Path("missing-file-xyz.log"), required=False)
    )
    assert collection_status([ok]) == "healthy"
    assert collection_status([ok, failed_optional]) == "degraded"
    assert collection_status([ok, failed_required]) == "unhealthy"


def test_redaction_patterns() -> None:
    text = (
        "Authorization: Basic dXNlcjpwYXNz\n"
        "api_key=abc123\n"
        "token: xyz\n"
        "https://user:pass@example.test/path\n"
    )
    redacted = redact_text(text)
    assert "dXNlcjpwYXNz" not in redacted
    assert "abc123" not in redacted
    assert "xyz" not in redacted
    assert "user:pass" not in redacted


def test_sanitize_filename() -> None:
    assert sanitize_filename("edge api / prod") == "edge_api_prod"
    assert sanitize_filename("....") == "source"
