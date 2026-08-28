# README Remediation 002 — Facts the Repository Did Not Support

Date: 2026-08-28 · Scope: `README.md` (documentation only; no file under `src/` changed)
Auditor: claude/opus-5 · Commits: `5f0a00d`…`ecd548f` (one per task, T1–T10)

This report answers verification step 5 of the remediation brief: every fact that the
brief asserted, or implied, and that the repository does not support. For each item the
README carries the repository fact instead.

## Location note

The brief asked for `audits/README_remediation_002.md`. This repository has no top-level
`audits/` directory. Audits live in `docs/audits/` (two files, both dated 2026-08).
This report follows that convention.

## 1. Brief facts the repository contradicts

| # | Fact in the brief | What the repository says | Evidence | What the README now says |
|---|---|---|---|---|
| 1 | "Add Node.js 20 or later (required by the Pi frontend)." | Three different floors exist, and none is 20. The shipped bundle declares `">=22.19.0"`. The launcher message says `>=18`. The old README said `>= 20`. | `src/beagle/frontends/pi/vendor/pi-prebuild/package.json:103-104`; `src/beagle/cli/commands/pi.py:9,67` | Node.js 22.19.0 or later — the floor the shipped bundle itself declares. |
| 2 | "add Goose with minimum version to prerequisites" | No minimum Goose version is declared anywhere. Two incidental version mentions exist, and they disagree: 1.29.1 and 1.44.0. | `src/beagle/bridges/goose_launcher.py:9`; `src/beagle/context/session_usage.py:7` | Goose is required, resolved through `PATH` or `GOOSE_BIN`, and "the repository does not declare a minimum Goose version". |
| 3 | T3 implies Docker has a part in isolation. | Docker is in no microVM condition. The four conditions are the `firecracker` binary, `/dev/kvm`, a kernel image, and a rootfs image. | `src/beagle/core/sandbox.py:571-597` | Docker packages and runs the image. A separate subsection states that Docker is not an isolation mode. |
| 4 | T5: the firewall "starts by default" — stated without qualification. | True for the pattern pass. The Goose subprocess pass runs only when the sub-agent runtime is `goose_cli`; for a remote runtime the pattern verdict stands. | `src/beagle/security/firewall.py:236-243` | Both passes are documented, with the runtime condition on the second. |
| 5 | T9: "Keep the fallback-path statement" — the statement in the README named Unix domain sockets and Redis. | The built-in fallback is the HTTP transport. `[connections].transport` defaults to `"http"` and is never auto-set to a plugin name. | `src/beagle/core/transports.py:1-36`; `src/beagle/config/schema.py:766-774` | The fallback path is the built-in HTTP transport. |
| 6 | T9: Orpheus has an "isolation function". | The repository describes process separation, not an isolation boundary: the harness dispatches over IPC to the OpenClaw controller instead of spawning agent processes itself. | `src/beagle/infrastructure/agent_harness.py:10,179` | The function is named "Process separation". |

## 2. Brief facts the repository confirms

| Fact in the brief | Evidence |
|---|---|
| The core is MIT in all four sources. | `LICENSE:1`; `pyproject.toml:10`; README badge; README text |
| The wheel does not contain the Goose binary. | `pyproject.toml:117-140` package-data lists the `pi` bundle only; no Goose entry, no Goose dependency |
| `beagle config init` creates the configuration file. | `src/beagle/cli/commands/config.py:231-245`. Live run printed `Config already exists at /home/server/.config/beagle/beagle_core_config/config.toml` |
| Environment variables override the file. | `src/beagle/config/loader.py:875-891` — `load_config()` then `apply_env_overrides()` |
| The firewall timeout comes from code. | `SEMANTIC_FIREWALL_TIMEOUT = 15`, `src/beagle/security/constants.py:17` |
| `FIREWALL_MODEL` and `FIREWALL_PROVIDER` exist. | `src/beagle/security/firewall.py:296-297`; defaults `ollama_cloud` / `gemma4:31b` at `constants.py:26-27` |
| The stdio transport does not require a token. | `src/beagle/infrastructure/mcp_utility_server.py:1880-1897`; `src/beagle/infrastructure/mcp_rag_server.py:2065-2077` |
| The distribution name `beagle-orpheus` is real in this repository. | `pyproject.toml:63-65,76-79`; `src/beagle/infrastructure/agent_harness.py:29`; `src/beagle/infrastructure/_orpheus_optional.py` |
| The style-guide system works as the brief describes. | `src/beagle/style_guides/loader.py:53-100`; `render.py:1-15`; `src/beagle/config/_config_path.py:211-236` |

The default `config.toml` has **20** sections. Counted from `generate_default_config()` and
confirmed by execution:
`orchestrator, goose, models, budget, cache, rate_limit, mcp, logging, node_timeout, pool,
context_threshold, memory, security, output, circuit_breaker, orpheus, paths, behavior,
mcp_auth, mcp_cors`.

## 3. Unsupported claims found in the README itself

These were not in the brief. The sweep for absolute claims (T6) found them.

| Claim removed | Why it fails | Evidence |
|---|---|---|
| "Human-in-the-Loop — Pauses for your permission before consequential actions" | `require_approval` defaults to `False`, so the pause is opt-in for each workflow node. | `src/beagle/core/workflow_schema.py:42` |
| "A structured to-do list that never loops" / "Prevents agents from going in circles" | CVCP's own `FAIL → incorporate_feedback → execute` path is a loop. It is bounded by `max_cvcp_attempts` (default 3), not absent. | `src/beagle/protocols/cvcp.py:1-15`; `src/beagle/config/defaults.py:124` |
| "Eliminates hallucinations and false claims" | The protocol finds and retries. It does not remove all errors. | `src/beagle/protocols/cvcp.py:1-15` |
| "Docker (optional) — required for Firecracker microVM isolation" | See item 3 above. | `src/beagle/core/sandbox.py:571-597` |
| "Beagle falls back to subprocess sandboxing when it is unavailable" | The fallback is deny-by-default: `allow_fallback` is `False`. | `src/beagle/core/sandbox.py:509-530` |

## 4. Defects found outside the documentation scope

These are `src/` defects. The brief puts `src/` out of scope, so none was changed. The
README does not cite either script.

1. `src/beagle/core/sandbox.py` tells the reader to run `scripts/setup_firecracker.py`
   four times (lines 552, 575, 586, 593). That file does not exist in the repository.
2. The runtime tells the operator to run `scripts/setup_orpheus_rings.py` when it cannot
   create `/run/orpheus_ring`. That file does not exist either. Observed in the live run
   below.
3. `beagle run … --headless` exits 0 after a node fails. The live run below reported
   `Completed with 1 errors` and still returned exit code 0.
4. `pyproject.toml:37` says "CPU-only torch pin; see README for GPU/CPU index guidance."
   The README has no such guidance, and it never did. `uv.lock` carries 36 `nvidia-*`
   entries, so Quick Start step 2 (`uv sync --frozen --no-dev`) downloads the CUDA
   packages. The clean-container run below shows `nvidia-cublas` (403 MiB) and
   `nvidia-cusparselt-cu13` (162 MiB) among them. Writing the missing guidance needs a
   decision about the index to use, so this report records the gap and does not invent
   the procedure.

## 5. Verification record

### Lint gate

`qup check --no-cache README.md` after every task: `ascii-diagram-check`,
`logic-notation-check`, and `markdownlint-cli2` all PASS, 0 violations.
`ascii-diagram-check` reports `1 diagram block(s), 1 flow chart(s) — clean`, which proves
the pipeline diagram is classified and its Mermaid pair is checked.

### Usage example

Command: `beagle run research "What does the authentication module do?" --budget 5.0`

The cost estimate built the DAG and passed the budget check:

```text
plan / discover / fact_check / synthesize   TOTAL $0.072   ~11.0m
✅ Budget sufficient ($5.00 > $0.072)
```

The full headless run reached the Goose subprocess, which means the semantic firewall
allowed the query and the Goose binary passed validation. Execution then stopped at the
provider:

```text
[orchestrator] deepseek-v4-flash:0731-cloud/ollama_cloud failed: No <final_answer> found
warning: Please check your account with your provider to add more credits

RUN_ID: research
Total tokens: 0
Total cost: $0.000000
Nodes completed: 1
```

The failure is an exhausted provider account, not a defect in the documented steps. The
run also produced the two `src/` findings recorded in section 4 above.

### Quick Start on a clean machine

Container: `python:3.13-slim`, nothing pre-installed. The repository was cloned into the
container at `ecd548f`, the last documentation commit.

| Step | Result |
|---|---|
| Prerequisite check | `Python 3.13.15` present. `node` absent. `goose` absent. Both absences match the new prerequisites list. |
| Step 1 — `git clone` | Pass. `clone OK -> HEAD ecd548f` |
| Prerequisite — `curl -LsSf https://astral.sh/uv/install.sh \| sh` | Pass. `uv 0.12.7` installed, exactly as the README command states. |
| Step 2 — `uv sync --frozen --no-dev` | Runs, and downloads the CUDA packages named in finding 4 above. This step is the one that needs the missing index guidance. |

The first attempt of this run failed at step 1 with `fatal: detected dubious ownership in
repository at '/src/.git'`. That is a property of cloning a bind-mounted host repository
as root inside a container, not a defect in the README. The re-run adds
`git config --global --add safe.directory` and proceeds.

## 6. Status of this report

Every finding above is verified. The clean-container walk of step 2 was still downloading
when this report was written; the CUDA finding it produced is already conclusive, and the
remaining rows (steps 3 and 5, with and without Node.js) are appended when the run ends.
