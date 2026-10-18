"""Argument parsing and command implementations for the netops CLI."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import Config, LogSourceConfig, load_config
from .diagnostics import (
    Collection,
    collection_status,
    collect_sources,
    write_manifest,
)
from .errors import ConfigError, NetopsError, OperationalError
from .health import (
    ServiceHealth,
    check_services,
    summarize,
)
from .log import configure_logging, event, get_logger
from .report import (
    RunResult,
    build_result,
    load_run_result,
    render_markdown,
    status_exit_code,
    write_report,
    write_run_result,
)
from .ui import print_table

logger = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netops",
        description=(
            "Validate network service configuration, probe health with bounded "
            "retries, collect diagnostics, and generate operational reports."
        ),
        epilog=(
            "Exit codes: 0 healthy, 1 degraded, 2 config/usage error, "
            "3 required failure."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    logging_parent = argparse.ArgumentParser(add_help=False)
    logging_parent.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Structured log level (default: INFO).",
    )
    logging_parent.add_argument(
        "--log-format",
        choices=["json", "text"],
        default="json",
        help="Structured log format written to stderr (default: json).",
    )
    logging_parent.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Also append structured logs to this file.",
    )

    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        help="Path to the YAML configuration file.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        parents=[config_parent, logging_parent],
        help="Validate configuration without touching the network.",
        description="Validate schema, references, and safety bounds.",
    )
    validate.add_argument(
        "--json", action="store_true", help="Print the validation result as JSON."
    )
    validate.add_argument(
        "--strict",
        action="store_true",
        help="Treat configuration warnings as validation errors.",
    )
    validate.set_defaults(handler=cmd_validate)

    health = subparsers.add_parser(
        "health",
        parents=[config_parent, logging_parent],
        help="Probe HTTP, TCP, and command health checks.",
        description="Run health checks with retries and bounded timeouts.",
    )
    health.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    health.add_argument(
        "--output",
        type=Path,
        help="Write the health result as JSON to this path.",
    )
    health.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show the probe plan without network calls.",
    )
    health.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Maximum concurrent checks (default: 4).",
    )
    health.set_defaults(handler=cmd_health)

    collect = subparsers.add_parser(
        "collect",
        parents=[config_parent, logging_parent],
        help="Collect bounded, redacted diagnostic logs.",
        description="Collect file, journalctl, and command diagnostics.",
    )
    collect.add_argument(
        "-o",
        "--output",
        required=True,
        type=Path,
        help="Directory for artifacts and manifest.json.",
    )
    collect.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="Collect only this source; repeat for multiple sources.",
    )
    collect.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be collected without reading logs.",
    )
    collect.add_argument(
        "--force",
        action="store_true",
        help="Allow writing into a non-empty output directory.",
    )
    collect.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON."
    )
    collect.set_defaults(handler=cmd_collect)

    run = subparsers.add_parser(
        "run",
        parents=[config_parent, logging_parent],
        help="Validate, check health, collect diagnostics, and report.",
        description="Run the full validation, health, collection, and report flow.",
    )
    run.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Run directory (default: artifacts/<timestamp>-<environment>).",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the complete plan without network calls or writes.",
    )
    run.add_argument(
        "--skip-collect",
        action="store_true",
        help="Skip diagnostic collection.",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="Allow writing into a non-empty run directory.",
    )
    run.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Maximum concurrent health checks (default: 4).",
    )
    run.add_argument(
        "--json",
        action="store_true",
        help="Print the run result as JSON after writing artifacts.",
    )
    run.set_defaults(handler=cmd_run)

    report = subparsers.add_parser(
        "report",
        parents=[logging_parent],
        help="Re-render a report from a saved run.json.",
        description="Render Markdown or JSON from a previous run result.",
    )
    report.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to a run.json written by 'netops run'.",
    )
    report.add_argument(
        "--output",
        type=Path,
        help="Write the report here instead of stdout.",
    )
    report.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Output format (default: md).",
    )
    report.set_defaults(handler=cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configure_logging(
            level=args.log_level,
            fmt=args.log_format,
            log_file=str(args.log_file) if args.log_file else None,
        )
    except OSError as exc:
        print(f"error: could not open log file: {exc}", file=sys.stderr)
        return 2

    try:
        return int(args.handler(args))
    except NetopsError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.strict and config.warnings:
        if args.json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "config": config.summary(),
                        "errors": list(config.warnings),
                    },
                    indent=2,
                )
            )
        else:
            print("Configuration is valid but has warnings:", file=sys.stderr)
            for warning in config.warnings:
                print(f"  - {warning}", file=sys.stderr)
            print("error: --strict treats warnings as errors", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {"valid": True, "config": config.summary()},
                indent=2,
            )
        )
    else:
        print(f"Configuration valid: {config.path}")
        print(f"Environment: {config.environment}")
        print(
            f"Services: {len(config.services)} "
            f"({len(config.required_services)} required)"
        )
        print(f"Log sources: {len(config.log_sources)}")
        for warning in config.warnings:
            print(f"warning: {warning}", file=sys.stderr)
    event(
        logger,
        logging.INFO,
        "config_validated",
        "configuration validated",
        config=str(config.path),
        environment=config.environment,
        services=len(config.services),
    )
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    _validate_workers(args.workers)
    config = load_config(args.config)
    started = time.perf_counter()
    results = check_services(
        config.services, workers=args.workers, dry_run=args.dry_run
    )
    summary = summarize(results)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    payload = {
        "environment": config.environment,
        "config_path": str(config.path),
        "dry_run": args.dry_run,
        "duration_ms": duration_ms,
        "summary": summary.to_dict(),
        "services": [item.to_dict() for item in results],
    }
    if args.output:
        _write_json(args.output, payload)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        if args.dry_run:
            print("Dry run: no network calls were made.")
        print(
            f"Health: {summary.status.upper()} — "
            f"{summary.healthy}/{summary.total} passed, "
            f"{summary.required_failures} required failure(s)"
        )
        print_table(
            [
                "Service",
                "Type",
                "Target",
                "Req",
                "Status",
                "Att",
                "Latency",
                "Detail",
            ],
            [
                [
                    item.name,
                    item.type,
                    item.target,
                    "yes" if item.required else "no",
                    item.status,
                    item.attempts,
                    f"{item.latency_ms:g} ms",
                    item.detail,
                ]
                for item in results
            ],
            max_widths=[24, 8, 42, 5, 10, 5, 10, 46],
        )
        if args.output:
            print(f"Health JSON: {args.output}")
    event(
        logger,
        logging.INFO if summary.status in {"healthy", "planned"} else logging.WARNING,
        "health_checked",
        "health checks completed",
        environment=config.environment,
        status=summary.status,
        services=len(results),
        failures=summary.required_failures + summary.optional_failures,
        dry_run=args.dry_run,
    )
    return 0 if args.dry_run else status_exit_code(summary.status)


def cmd_collect(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    sources = _select_sources(config, args.only)
    if args.dry_run:
        results = collect_sources(sources, dry_run=True)
        payload = {
            "environment": config.environment,
            "dry_run": True,
            "collections": [item.to_dict() for item in results],
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("Dry run: no logs were read and no files were written.")
            _print_collection_table(results)
        return 0

    output = _prepare_output_dir(args.output, force=args.force)
    results = collect_sources(sources, output_dir=output)
    manifest = write_manifest(output, results)
    status = collection_status(results)
    payload = {
        "environment": config.environment,
        "dry_run": False,
        "output": str(output),
        "manifest": str(manifest),
        "status": status,
        "collections": [item.to_dict() for item in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_collection_table(results)
        print(f"Artifacts: {output}")
        print(f"Manifest: {manifest}")
    event(
        logger,
        logging.INFO if status == "healthy" else logging.WARNING,
        "diagnostics_collected",
        "diagnostic collection completed",
        environment=config.environment,
        sources=len(results),
        status=status,
        output=str(output),
    )
    return status_exit_code(status)


def cmd_run(args: argparse.Namespace) -> int:
    _validate_workers(args.workers)
    config = load_config(args.config)
    started = time.perf_counter()

    if args.dry_run:
        services = check_services(
            config.services, workers=args.workers, dry_run=True
        )
        collections = (
            []
            if args.skip_collect
            else collect_sources(config.log_sources, dry_run=True)
        )
        result = build_result(
            config,
            services,
            collections,
            duration_ms=(time.perf_counter() - started) * 1000,
            dry_run=True,
        )
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print("Dry run: no network calls, log reads, or files written.")
            print(f"Environment: {config.environment}")
            print(f"Health plan ({len(services)} checks):")
            _print_health_table(services)
            if args.skip_collect:
                print("Diagnostics: skipped (--skip-collect)")
            elif collections:
                print(f"Diagnostic plan ({len(collections)} sources):")
                _print_collection_table(collections)
            else:
                print("Diagnostics: no sources configured")
            print("Overall status: planned")
        return 0

    output = args.output or _default_run_dir(config)
    run_dir = _prepare_output_dir(output, force=args.force)
    diagnostics_dir = run_dir / "diagnostics"
    diagnostics_dir.mkdir(exist_ok=True)

    event(
        logger,
        logging.INFO,
        "run_started",
        "netops run started",
        environment=config.environment,
        config=str(config.path),
        output=str(run_dir),
    )
    services = check_services(config.services, workers=args.workers)
    if args.skip_collect:
        collections: list[Collection] = []
    else:
        collections = collect_sources(config.log_sources, output_dir=diagnostics_dir)
        if collections:
            write_manifest(diagnostics_dir, collections)

    result = build_result(
        config,
        services,
        collections,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    run_path = write_run_result(result, run_dir / "run.json")
    report_path = write_report(result, run_dir / "report.md")

    payload = result.to_dict()
    payload["artifacts"] = {
        "run": str(run_path),
        "report": str(report_path),
        "diagnostics": str(diagnostics_dir),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Run complete: {result.status.upper()}")
        print(
            f"Health: {result.health.healthy}/{result.health.total} passed; "
            f"diagnostics: "
            f"{sum(1 for item in result.collections if item.ok)}/"
            f"{len(result.collections)} collected"
        )
        print(f"Report: {report_path}")
        print(f"Run JSON: {run_path}")
        if not args.skip_collect:
            print(f"Diagnostics: {diagnostics_dir}")
    event(
        logger,
        logging.INFO if result.status == "healthy" else logging.WARNING,
        "run_completed",
        "netops run completed",
        environment=config.environment,
        status=result.status,
        duration_ms=result.duration_ms,
        report=str(report_path),
    )
    return status_exit_code(result.status)


def cmd_report(args: argparse.Namespace) -> int:
    result = load_run_result(args.input)
    if args.format == "json":
        rendered = json.dumps(result.to_dict(), indent=2) + "\n"
    else:
        rendered = render_markdown(result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Report written: {args.output}")
    else:
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    event(
        logger,
        logging.INFO,
        "report_rendered",
        "report rendered",
        input=str(args.input),
        output=str(args.output) if args.output else "stdout",
        format=args.format,
    )
    return 0


def _print_health_table(services: Sequence[ServiceHealth]) -> None:
    print_table(
        ["Service", "Type", "Target", "Req", "Status", "Att", "Latency", "Detail"],
        [
            [
                item.name,
                item.type,
                item.target,
                "yes" if item.required else "no",
                item.status,
                item.attempts,
                f"{item.latency_ms:g} ms",
                item.detail,
            ]
            for item in services
        ],
        max_widths=[24, 8, 42, 5, 10, 5, 10, 46],
    )


def _print_collection_table(collections: Sequence[Collection]) -> None:
    print_table(
        ["Source", "Type", "Target", "Req", "Status", "Lines", "Artifact"],
        [
            [
                item.name,
                item.type,
                item.target,
                "yes" if item.required else "no",
                item.status,
                item.line_count,
                item.artifact or "-",
            ]
            for item in collections
        ],
        max_widths=[24, 12, 50, 5, 10, 7, 30],
    )


def _select_sources(
    config: Config, names: Sequence[str]
) -> list[LogSourceConfig]:
    if not names:
        return list(config.log_sources)
    known = {source.name: source for source in config.log_sources}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ConfigError(
            "unknown log source(s): " + ", ".join(unknown),
            hint="Available sources: "
            + (", ".join(known) if known else "(none configured)"),
        )
    wanted = set(names)
    return [source for source in config.log_sources if source.name in wanted]


def _prepare_output_dir(path: Path, *, force: bool) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        if not resolved.is_dir():
            raise ConfigError(
                f"output path is not a directory: {resolved}",
                hint="Pass a directory path.",
            )
        if any(resolved.iterdir()) and not force:
            raise ConfigError(
                f"output directory is not empty: {resolved}",
                hint="Use a new directory or pass --force to write into it.",
            )
    else:
        try:
            resolved.mkdir(parents=True)
        except OSError as exc:
            raise ConfigError(
                f"could not create output directory {resolved}: {exc}",
                hint="Check the parent directory permissions.",
            ) from exc
    return resolved


def _default_run_dir(config: Config) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("artifacts") / f"{stamp}-{config.environment}"


def _write_json(path: Path, payload: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise OperationalError(
            f"could not write JSON output {path}: {exc}",
            hint="Check the output path permissions.",
        ) from exc


def _validate_workers(workers: int) -> None:
    if not 1 <= workers <= 64:
        raise ConfigError(
            "--workers must be between 1 and 64",
            hint="Use a small concurrency limit, for example --workers 4.",
        )
