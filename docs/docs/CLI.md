# Beagle CLI Reference

`beagle` (also exposed as `goose-workflow`) is the command-line
interface to the Beagle orchestrator. This document covers every
command and its options.

## Global options

```text
$ beagle --version
beagle <version>
```

The version string is resolved at runtime from the package SSOT
(`beagle.constants.PACKAGE_VERSION`, sourced from `pyproject.toml`), so it
always matches the installed release.

| Flag        | Description                                          |
|-------------|------------------------------------------------------|
| `--version` | Print the package version (from `__version__`) and exit. |

## `beagle run` — Run a workflow

```text
beagle run <workflow> <query> [options]
```

| Argument    | Description |
|-------------|-------------|
| `workflow`  | Workflow name: `research`, `deep-planning`, `develop`, `self-improvement`, `devops`, `db-migration`, `audit`, `security`, `incident`, `verify`. |
| `query`     | The query to process. |

| Option                    | Default | Description |
|---------------------------|---------|-------------|
| `--budget`, `-b`          | 10.0    | Maximum budget in USD. The orchestrator stops when the cost tracker hits this number. |
| `--resume`                | —       | Resume from a checkpoint ID (printed by previous interrupted runs). |
| `--estimate`, `-e`        | False   | Show cost estimate without executing. |
| `--auto-approve`          | False   | Auto-approve all approval gates (use with care). |
| `--approve-all`           | False   | Approve all human-in-the-loop gates (bypass `require_approval`). |
| `--steering`, `-s`        | —       | Global steering prompt injected into all agents as a high-priority directive. |
| `--mode`, `-m`            | —       | Workflow mode: `audit` (read-only), `develop` (read-write), `research` (read-only). Overrides the YAML default. |
| `--tui`                   | False   | Launch the reactive TUI dashboard. |
| `--headless`              | False   | Run without any interactive output (CI/CD mode). |
| `--skip-preflight`        | False   | Bypass the cost and time estimation confirmation. |
| `--output-format`, `-f`   | markdown | Output format: `markdown`, `json`, `sarif`, `github-issues`. |
| `--output`, `-o`          | —       | Custom path to save the output report. |
| `--dry-run`               | False   | Plan the workflow without executing it. Prints the plan (graph, estimated cost, agents) and exits. For `github-issues` output, `--dry-run` still previews the issues. |

**Example:**

```bash
# Plan a research workflow, see cost & agents, then run it for real
beagle run research "What are the security implications of prompt caching?" --dry-run
beagle run research "What are the security implications of prompt caching?" --budget 5.0

# Audit-only mode (read-only)
beagle run audit "Find all hardcoded secrets in this repo" --mode audit

# CI-friendly: JSON output, no prompts
beagle run security "scan the codebase" --headless --output-format json -o scan.json
```

## `beagle doctor` — Diagnose the installation

```text
beagle doctor [--json]
```

Reports:

- Package version (the `__version__` SSOT)
- Python version and platform
- Critical third-party packages (`typer`, `rich`, `pydantic`, `mcp`,
  `langgraph`, `langchain`, `lancedb`, `kuzu`, `opentelemetry-api`)
- The `google-re2` secret-scrubber dependency (mandatory; the system fails closed without it)
- The feature-flag state from `constants.FEATURE_FLAGS`
- The list of supported workflows
- The standard startup health checks

Exit code is 0 if everything is healthy, 1 if any required check fails.

## `beagle config` — Show or validate configuration

```text
beagle config show            # show resolved config (with secrets redacted)
beagle config show --json     # machine-readable JSON
beagle config validate        # validate the configuration
beagle config init            # seed ~/.config/beagle from code defaults
```

## `beagle health` — Run startup health checks

```text
beagle health [--json] [--required-only]
```

Lighter than `doctor` — runs only the startup checks, not the
version/dependency report.

## `beagle replay` — Replay a previous run

```text
beagle replay <run_id> [--dry-run]
```

Replays a recorded run from the reproducibility store. With
`--dry-run`, prints the description of the replay without
executing it.

## `beagle config init` — Initialize Beagle configuration

```text
beagle config init [--force]
```

Seeds `~/.config/beagle` from programmatic defaults (creates
`beagle_core_config/config.toml`). `--force` overwrites an existing file.

## `beagle stats` — Show runtime statistics

```text
beagle stats [--json]
```

Prints aggregated statistics: total runs, average cost, p95
duration, error rate, etc. `--json` makes the output
machine-readable.

## `beagle visualize` — Render a workflow graph

```text
beagle visualize <workflow_name>
```

Renders the DAG of a workflow as an ASCII diagram.

## `beagle checkpoint` — Manage checkpoints

```text
beagle checkpoint list
beagle checkpoint show <id>
beagle checkpoint delete <id>
```

## `beagle daemon` — Run Beagle as a long-lived daemon

```text
beagle daemon start
beagle daemon stop
beagle daemon status
```

The daemon is the recommended way to run Beagle in production. It
manages the Orpheus IPC, the RAG connections, and the cost
tracker across multiple CLI invocations.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Generic error / required health check failed |
| 2    | Misuse (unknown command, invalid option) |
| 130  | SIGINT (Ctrl-C) |

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `BEAGLE_PROMETHEUS_PORT` | If set to a non-zero int, start a Prometheus exporter on that port. | not set |
| `BEAGLE_MULTI_TENANT` | Enable per-tenant rate limiting on MCP endpoints. | not set |
| `BEAGLE_CONFIG_PATH` | Override the path to `config.toml`. | repo root |
| `BEAGLE_LOG_LEVEL` | Set the log level (DEBUG, INFO, WARNING, ERROR). | INFO |
| `BEAGLE_LICENSE_KEY` | Optional license key (currently no-op; reserved for future use). | not set |

See [`docs/SECURITY.md`](SECURITY.md) for additional security-relevant
variables.
