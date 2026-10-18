from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from netops.config import load_config, parse_config
from netops.errors import ConfigError


def base_config() -> dict:
    return {
        "version": 1,
        "environment": "test",
        "defaults": {
            "timeout_seconds": 1.5,
            "retries": 1,
            "backoff_seconds": 0.1,
            "max_backoff_seconds": 0.5,
        },
        "services": [
            {
                "name": "api",
                "type": "http",
                "url": "http://127.0.0.1:8080/healthz",
                "expected_status": [200, 204],
            }
        ],
        "log_sources": [
            {"name": "app", "type": "file", "path": "logs/app.log"}
        ],
        "report": {"title": "Test Report"},
    }


def parse(data: dict, path: Path):
    return parse_config(data, path)


def test_valid_config_applies_defaults_and_resolves_paths(tmp_path: Path) -> None:
    data = base_config()
    config = parse(data, tmp_path / "config.yaml")
    assert config.environment == "test"
    assert config.report_title == "Test Report"
    assert config.services[0].retry.retries == 1
    assert config.services[0].timeout_seconds == 1.5
    assert config.services[0].expected_status == (200, 204)
    assert config.log_sources[0].resolved_path == (
        tmp_path / "logs" / "app.log"
    ).resolve()
    assert config.warnings == ()


def test_optional_service_is_not_required(tmp_path: Path) -> None:
    data = base_config()
    data["services"][0]["required"] = False
    config = parse(data, tmp_path / "config.yaml")
    assert config.services[0].required is False
    assert config.required_services == ()


def test_unknown_root_key_is_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["unexpected"] = True
    with pytest.raises(ConfigError, match="unknown key"):
        parse(data, tmp_path / "config.yaml")


def test_unknown_service_key_is_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"][0]["retry_count"] = 3
    with pytest.raises(ConfigError, match="unknown key"):
        parse(data, tmp_path / "config.yaml")


def test_duplicate_service_names_are_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"].append(copy.deepcopy(data["services"][0]))
    with pytest.raises(ConfigError, match="duplicate service"):
        parse(data, tmp_path / "config.yaml")


def test_invalid_port_is_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"] = [
        {"name": "cache", "type": "tcp", "host": "127.0.0.1", "port": 0}
    ]
    with pytest.raises(ConfigError, match="port"):
        parse(data, tmp_path / "config.yaml")


def test_type_specific_keys_are_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"] = [
        {
            "name": "cache",
            "type": "tcp",
            "host": "127.0.0.1",
            "port": 6379,
            "url": "http://127.0.0.1:6379",
        }
    ]
    with pytest.raises(ConfigError, match="not valid for type"):
        parse(data, tmp_path / "config.yaml")


def test_url_credentials_are_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"][0]["url"] = "http://user:secret@example.test/healthz"
    with pytest.raises(ConfigError, match="must not embed credentials"):
        parse(data, tmp_path / "config.yaml")


def test_invalid_expected_status_is_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"][0]["expected_status"] = 99
    with pytest.raises(ConfigError, match="expected_status"):
        parse(data, tmp_path / "config.yaml")


def test_empty_command_is_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"] = [
        {"name": "clock", "type": "command", "command": []}
    ]
    with pytest.raises(ConfigError, match="non-empty list"):
        parse(data, tmp_path / "config.yaml")


def test_unknown_log_source_type_is_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["log_sources"] = [{"name": "x", "type": "syslog"}]
    with pytest.raises(ConfigError, match="must be one of"):
        parse(data, tmp_path / "config.yaml")


def test_duplicate_log_sources_are_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["log_sources"].append(copy.deepcopy(data["log_sources"][0]))
    with pytest.raises(ConfigError, match="duplicate log source"):
        parse(data, tmp_path / "config.yaml")


def test_no_services_is_rejected(tmp_path: Path) -> None:
    data = base_config()
    data["services"] = []
    with pytest.raises(ConfigError, match="non-empty list"):
        parse(data, tmp_path / "config.yaml")


def test_no_log_sources_produces_warning(tmp_path: Path) -> None:
    data = base_config()
    data["log_sources"] = []
    config = parse(data, tmp_path / "config.yaml")
    assert "no log_sources" in config.warnings[0]


def test_missing_config_file_has_hint(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "missing.yaml")
    assert "not found" in str(excinfo.value)
    assert excinfo.value.hint


def test_invalid_yaml_reports_location(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("services:\n  - name: x\n   type: http\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_load_yaml_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base_config()), encoding="utf-8")
    config = load_config(path)
    assert config.services[0].name == "api"
    assert config.log_sources[0].type == "file"
