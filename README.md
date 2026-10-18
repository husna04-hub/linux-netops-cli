# Linux Network Operations Automation CLI

`netops` is a Linux-first operations CLI that validates YAML service
configuration, probes HTTP/TCP/command health with bounded retries, collects
redacted diagnostics, and produces machine-readable run records plus a
Markdown operations report.

It is designed for the workflow an on-call engineer actually needs:

```text
validate -> probe -> collect -> report
             |          |
             |          +-- file tails, journalctl, diagnostic commands
             +-- HTTP status, TCP connect, local command, bounded retries
```

## Highlights

- **Strict config validation** before any network call: unknown keys, duplicate
  names, invalid ports/URLs, unsafe timeouts, and type-specific mistakes fail
  with a YAML path and an actionable hint.
- **Three health check types**: HTTP status, TCP connect, and local command
  exit code. HTTP redirects, TLS, expected status sets, and per-service headers
  are supported.
- **Bounded exponential retries** with a hard attempt cap and capped backoff.
  Every result records the number of attempts and total latency.
- **Diagnostics with guardrails**: tail files without loading them whole, read
  `journalctl` by unit, run argv-based commands, cap captured bytes/lines, and
  redact bearer/basic tokens, passwords, API keys, and URL credentials.
- **Structured JSON logs** on stderr, clean table/JSON output on stdout, and
  optional log files.
- **Dry-run everywhere**: health, collection, and the full run can print the
  plan without network calls, log reads, or writes.
- **Repeatable artifacts**: `run.json`, `report.md`, `diagnostics/manifest.json`,
  and per-source log files with stable names.
- **Actionable exit codes** for cron jobs and CI: `0` healthy, `1` degraded,
  `2` config/usage error, `3` required failure.

## Quickstart

Linux/macOS:

```bash
cd linux-netops-cli
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

netops validate -c configs/lab.yaml
pytest
```

Windows PowerShell:

```powershell
cd linux-netops-cli
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
netops validate -c configs/lab.yaml
pytest
```

The fastest end-to-end demonstration starts two local HTTP services (one
healthy, one intentionally failing), generates a config, and runs the whole
pipeline:

```bash
python scripts/demo.py
```

Expected outcome: `DEGRADED` because the optional metrics service is failing;
the required API and both diagnostic sources succeed. Open
`artifacts/demo/run/report.md` to inspect the generated report.

An illustrative report is checked in at
[examples/report.example.md](examples/report.example.md).

## Commands

### Validate

```bash
netops validate -c configs/lab.yaml
netops validate -c configs/lab.yaml --json
netops validate -c configs/lab.yaml --strict
```

```text
Configuration valid: /srv/netops/configs/lab.yaml
Environment: lab
Services: 3 (1 required)
Log sources: 2
```

Validation is offline. `--strict` turns non-fatal configuration warnings (for
example, no log sources) into exit code `2`.

### Health

```bash
netops health -c configs/lab.yaml
netops health -c configs/lab.yaml --json --output artifacts/health.json
netops health -c configs/lab.yaml --dry-run
```

The table includes service, type, target, required flag, status, attempts,
latency, and the final error/status detail. Optional failures produce
`DEGRADED` and exit code `1`; required failures produce `UNHEALTHY` and exit
code `3`.

### Collect diagnostics

```bash
netops collect -c configs/lab.yaml -o artifacts/diagnostics
netops collect -c configs/lab.yaml -o artifacts/diagnostics \
  --only edge-api --only system-journal
```

Artifacts are written with sanitized names and a `manifest.json` containing
source type, target, required flag, line count, byte count, truncation flag,
and error metadata. Secrets are redacted before writing.

### Full run

```bash
netops run -c configs/lab.yaml
netops run -c configs/lab.yaml -o artifacts/incident-42
netops run -c configs/lab.yaml --skip-collect
```

Default output is
`artifacts/<UTC timestamp>-<environment>/` containing:

```text
run.json
report.md
diagnostics/
  manifest.json
  edge-api.log
  system-journal.log
```

### Re-render a saved report

```bash
netops report --input artifacts/incident-42/run.json
netops report --input artifacts/incident-42/run.json \
  --output report-copy.md
netops report --input artifacts/incident-42/run.json --format json
```

The saved `run.json` is the source of truth; the Markdown report can be
regenerated without re-probing services.

## Configuration

See [configs/lab.yaml](configs/lab.yaml) and
[configs/production.example.yaml](configs/production.example.yaml). Relative
file paths resolve against the configuration file's directory.

```yaml
version: 1
environment: lab
description: Local integration lab for the edge network stack.

defaults:
  timeout_seconds: 2.0
  retries: 2                 # total attempts = 3
  backoff_seconds: 0.2       # delay after attempt 1
  max_backoff_seconds: 1.0

services:
  - name: edge-api
    type: http
    url: http://127.0.0.1:8080/healthz
    expected_status: [200, 204]
    headers:
      User-Agent: netops-probe
    retries: 3
    tags: [edge]

  - name: cache
    type: tcp
    host: 127.0.0.1
    port: 6379
    required: false

  - name: nginx
    type: command
    command: ["systemctl", "is-active", "nginx.service"]
    expected_exit_code: 0
    required: true

log_sources:
  - name: edge-api
    type: file
    path: ../examples/logs/edge-api.log
    max_lines: 100

  - name: nginx-journal
    type: journalctl
    unit: nginx.service
    since: "-15 min"
    max_lines: 200
    required: false

report:
  title: Edge Network Operations Report
```

### Schema reference

| Section | Keys |
| --- | --- |
| Root | `version`, `environment`, `description`, `defaults`, `services`, `log_sources`, `report` |
| `defaults` | `timeout_seconds`, `retries`, `backoff_seconds`, `max_backoff_seconds` |
| `services[]` | `name`, `type`, `required`, `description`, `tags`, `timeout_seconds`, `retries`, `backoff_seconds`, `max_backoff_seconds`, `url`, `expected_status`, `headers`, `host`, `port`, `command`, `expected_exit_code` |
| `log_sources[]` | `name`, `type`, `required`, `path`, `unit`, `since`, `command`, `max_lines`, `timeout_seconds` |
| `report` | `title` |

Service types:

| Type | Required keys | Semantics |
| --- | --- | --- |
| `http` | `url` | GET; success when the final status is in `expected_status` (default `[200]`) |
| `tcp` | `host`, `port` | TCP connect within `timeout_seconds` |
| `command` | `command` | argv execution; success when exit code equals `expected_exit_code` |

Log source types:

| Type | Required keys | Semantics |
| --- | --- | --- |
| `file` | `path` | tail the last `max_lines` lines without loading the whole file |
| `journalctl` | `unit` | `journalctl --unit ... --lines ...`, optional `--since` |
| `command` | `command` | argv execution; stdout + stderr captured, bounded and redacted |

`retries` means *additional* attempts after the first. With `retries: 2`, a
failing check runs at most three times; the delay after attempt *n* is
`min(backoff_seconds * 2^(n-1), max_backoff_seconds)`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Healthy, planned, or all required checks/collections succeeded |
| `1` | Degraded: only optional checks or optional sources failed |
| `2` | Configuration or usage error (validation, unknown source, unsafe output dir) |
| `3` | Required health check or required diagnostic source failed |
| `130` | Interrupted with Ctrl-C |

## Safety model

- Commands are executed as argv lists with `shell=False`; no value is
  interpolated into a shell string.
- The configuration file is trusted input. A diagnostic/command service can
  execute arbitrary local programs by design, so protect the config with normal
  file permissions and code review.
- Every network operation has a timeout. Retries have a hard cap and a capped
  exponential backoff.
- Log capture is limited by lines and bytes; file tails read backwards in
  chunks instead of loading large logs into memory.
- Diagnostics are redacted before being written. Redaction is best-effort; see
  "Production notes".
- `--dry-run` performs no network calls, no log reads, and no artifact writes.
- `run` refuses to write into a non-empty output directory unless `--force` is
  passed.

## Testing

The suite has **58 tests** covering:

- config schema, defaults, relative paths, duplicate names, invalid URLs/ports,
  incompatible keys, YAML error locations;
- HTTP success/failure/expected status/retry/timeout, TCP, command checks, and
  optional-vs-required summary semantics;
- file tailing, truncation, redaction, command failures, manifests, and
  dry-run behavior;
- Markdown/JSON report rendering and saved-run round trips;
- CLI exit codes, `--json`, `--dry-run`, output-directory protection, full runs,
  and report re-rendering.

```bash
pytest -q
```

## Project layout

```text
linux-netops-cli/
  configs/                 valid lab and production-shaped examples
  examples/logs/           sample diagnostic log
  scripts/demo.py          one-command local demonstration
  scripts/netops           bash launcher for a source checkout
  src/netops/
    cli.py                 argparse commands and exit codes
    config.py              schema and strict validation
    health.py              HTTP/TCP/command probes, retries, summary
    diagnostics.py         bounded collection, redaction, manifests
    report.py              run model and Markdown/JSON rendering
    log.py                 structured logging
  tests/                   58 pytest tests
```

## Resume bullet mapping

| Claim | Implementation |
| --- | --- |
| Built an automation CLI that validates config, inspects service health, collects diagnostic logs, and generates operational reports | `validate`, `health`, `collect`, `run`, `report` commands; `src/netops/` |
| Input validation, dry-run execution, bounded retries, actionable error messages, structured logs | `config.py` schema validation; `--dry-run`; `health.py` retry policy; `NetopsError` hints; `log.py` JSON events |
| Unit and integration tests for invalid configs, command failures, and recovery scenarios | 58 tests in `tests/`, including retry-then-success, missing logs, failing commands, and end-to-end CLI runs |

## Production notes

- `journalctl` may require membership in `systemd-journal` or elevated
  permissions. Mark journal sources `required: false` on hosts without
  systemd.
- Redaction covers common token/password patterns, but no regex can guarantee
  every secret is removed. Review artifacts before sharing and add
  environment-specific patterns if needed.
- For scheduled execution, run from a service manager/cron and retain
  `run.json` plus the report as incident evidence. The CLI returns non-zero on
  required failures so the scheduler can page.
- The tool intentionally has no remote execution or SSH layer; it runs on the
  host or in a container with the required read access.
