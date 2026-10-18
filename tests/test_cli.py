from __future__ import annotations

import json
from pathlib import Path

import yaml

from netops.cli import main
from tests.conftest import respond


def write_config(
    path: Path,
    url: str,
    *,
    log_path: Path | None = None,
    optional_url: str | None = None,
) -> Path:
    services = [
        {
            "name": "api",
            "type": "http",
            "url": url,
            "timeout_seconds": 0.5,
            "retries": 0,
        }
    ]
    if optional_url:
        services.append(
            {
                "name": "optional-api",
                "type": "http",
                "url": optional_url,
                "timeout_seconds": 0.5,
                "retries": 0,
                "required": False,
            }
        )
    data = {
        "version": 1,
        "environment": "test",
        "services": services,
        "log_sources": (
            [{"name": "app-log", "type": "file", "path": str(log_path)}]
            if log_path
            else []
        ),
        "report": {"title": "CLI Test Report"},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_validate_json(capsys, tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.yaml", "http://127.0.0.1:1/healthz")
    code = main(
        ["validate", "--config", str(config), "--json", "--log-level", "ERROR"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["config"]["environment"] == "test"


def test_validate_invalid_returns_2(capsys, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("version: 1\nservices: []\n", encoding="utf-8")
    code = main(["validate", "-c", str(config), "--log-level", "ERROR"])
    captured = capsys.readouterr()
    assert code == 2
    assert "error:" in captured.err
    assert "hint:" in captured.err


def test_health_json(capsys, start_http_server, tmp_path: Path) -> None:
    _, base = start_http_server(lambda handler: respond(handler))
    config = write_config(tmp_path / "config.yaml", f"{base}/healthz")
    code = main(
        ["health", "-c", str(config), "--json", "--log-level", "ERROR"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["status"] == "healthy"
    assert payload["services"][0]["ok"] is True


def test_health_unhealthy_returns_3(capsys, start_http_server, tmp_path: Path) -> None:
    _, base = start_http_server(lambda handler: respond(handler, 503))
    config = write_config(tmp_path / "config.yaml", f"{base}/healthz")
    code = main(["health", "-c", str(config), "--log-level", "ERROR"])
    captured = capsys.readouterr()
    assert code == 3
    assert "UNHEALTHY" in captured.out


def test_health_degraded_returns_1(capsys, start_http_server, tmp_path: Path) -> None:
    _, ok_base = start_http_server(lambda handler: respond(handler))
    _, bad_base = start_http_server(lambda handler: respond(handler, 503))
    config = write_config(
        tmp_path / "config.yaml",
        f"{ok_base}/healthz",
        optional_url=f"{bad_base}/healthz",
    )
    code = main(["health", "-c", str(config), "--log-level", "ERROR"])
    assert code == 1
    assert "DEGRADED" in capsys.readouterr().out


def test_health_dry_run_skips_network(capsys, tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.yaml", "http://127.0.0.1:1/healthz")
    code = main(
        ["health", "-c", str(config), "--dry-run", "--log-level", "ERROR"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "planned" in out
    assert "dry-run" in out.lower()


def test_collect_writes_redacted_artifact(capsys, tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("ok\nAuthorization: Bearer topsecret\n", encoding="utf-8")
    config = write_config(
        tmp_path / "config.yaml", "http://127.0.0.1:1/healthz", log_path=log
    )
    out = tmp_path / "collected"
    code = main(
        [
            "collect",
            "-c",
            str(config),
            "-o",
            str(out),
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 0
    artifact = (out / "app-log.log").read_text(encoding="utf-8")
    assert "topsecret" not in artifact
    assert "[REDACTED]" in artifact
    assert (out / "manifest.json").exists()


def test_collect_unknown_source_returns_2(capsys, tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.yaml", "http://127.0.0.1:1/healthz")
    code = main(
        [
            "collect",
            "-c",
            str(config),
            "-o",
            str(tmp_path / "out"),
            "--only",
            "missing",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 2
    assert "unknown log source" in capsys.readouterr().err


def test_run_end_to_end(capsys, start_http_server, tmp_path: Path) -> None:
    _, base = start_http_server(lambda handler: respond(handler))
    log = tmp_path / "app.log"
    log.write_text("service healthy\n", encoding="utf-8")
    config = write_config(
        tmp_path / "config.yaml", f"{base}/healthz", log_path=log
    )
    run_dir = tmp_path / "run"
    code = main(
        [
            "run",
            "-c",
            str(config),
            "-o",
            str(run_dir),
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 0
    assert (run_dir / "report.md").exists()
    assert (run_dir / "run.json").exists()
    assert (run_dir / "diagnostics" / "app-log.log").exists()
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "CLI Test Report" in report
    assert "HEALTHY" in report
    out = capsys.readouterr().out
    assert "Run complete: HEALTHY" in out


def test_run_degraded_returns_1(capsys, start_http_server, tmp_path: Path) -> None:
    _, ok_base = start_http_server(lambda handler: respond(handler))
    _, bad_base = start_http_server(lambda handler: respond(handler, 503))
    config = write_config(
        tmp_path / "config.yaml",
        f"{ok_base}/healthz",
        optional_url=f"{bad_base}/healthz",
    )
    run_dir = tmp_path / "run"
    code = main(
        [
            "run",
            "-c",
            str(config),
            "-o",
            str(run_dir),
            "--skip-collect",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 1
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "DEGRADED" in report
    assert "optional-api" in report


def test_run_dry_run_writes_nothing(capsys, tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.yaml", "http://127.0.0.1:1/healthz")
    run_dir = tmp_path / "run"
    code = main(
        [
            "run",
            "-c",
            str(config),
            "-o",
            str(run_dir),
            "--dry-run",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 0
    assert not run_dir.exists()
    assert "Dry run" in capsys.readouterr().out


def test_run_refuses_nonempty_directory(tmp_path: Path) -> None:
    config = write_config(tmp_path / "config.yaml", "http://127.0.0.1:1/healthz")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "keep.txt").write_text("keep", encoding="utf-8")
    code = main(
        [
            "run",
            "-c",
            str(config),
            "-o",
            str(run_dir),
            "--skip-collect",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 2


def test_report_rerenders_saved_run(capsys, start_http_server, tmp_path: Path) -> None:
    _, base = start_http_server(lambda handler: respond(handler))
    config = write_config(tmp_path / "config.yaml", f"{base}/healthz")
    run_dir = tmp_path / "run"
    assert (
        main(
            [
                "run",
                "-c",
                str(config),
                "-o",
                str(run_dir),
                "--skip-collect",
                "--log-level",
                "ERROR",
            ]
        )
        == 0
    )
    output = tmp_path / "report-copy.md"
    code = main(
        [
            "report",
            "--input",
            str(run_dir / "run.json"),
            "--output",
            str(output),
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 0
    assert "CLI Test Report" in output.read_text(encoding="utf-8")
    assert "Report written" in capsys.readouterr().out
