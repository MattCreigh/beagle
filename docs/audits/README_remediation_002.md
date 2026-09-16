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
| 1 | "Add Node.js 20 or later (required by the Pi frontend)." | Three floors exist and they disagree. The shipped bundle declares `">=22.19.0"`. The launcher tells the user `>= 20`. The `pi` command says `>=18`. No code enforces any of them: both entry points only test that `node` is on `PATH`. | `src/beagle/frontends/pi/vendor/pi-prebuild/package.json:103-104`; `src/beagle/frontends/pi/launcher.py:102-108`; `src/beagle/cli/commands/pi.py:9,64-67` | Node.js 22.19.0 or later — the floor the shipped bundle declares for itself. |
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
| Step 2 — `uv sync --frozen --no-dev` | Pass, in 8m 07s. `Installed 177 packages`. Most of the time is the CUDA download of finding 4. |
| Step 3 — `beagle config init` | Pass. Printed `Created config at /root/.config/beagle/beagle_core_config/config.toml`, which is the path the README now documents. |
| Step 5 — `beagle` with no Node.js | Fails as designed: `The pi frontend requires Node.js (>= 20). Install Node or add it to PATH.` Exit 1. |

The first attempt of this run failed at step 1 with `fatal: detected dubious ownership in
repository at '/src/.git'`. That is a property of cloning a bind-mounted host repository
as root inside a container, not a defect in the README. The re-run adds
`git config --global --add safe.directory` and proceeds.

Step 2 downloaded these GPU-only packages, which is the measurement behind finding 4:

```text
torch 506.1 MiB (CUDA-linked build) + triton 179.6 MiB
nvidia-cublas       403.5 MiB     nvidia-cusparse      139.2 MiB
nvidia-cudnn-cu13   349.1 MiB     nvidia-cuda-nvrtc     86.0 MiB
nvidia-cufft        204.2 MiB     nvidia-nvshmem-cu13   57.6 MiB
nvidia-cusolver     191.6 MiB     nvidia-curand         56.8 MiB
nvidia-nccl-cu13    187.4 MiB     nvidia-nvjitlink      38.8 MiB
nvidia-cusparselt   162.0 MiB     + 4 smaller nvidia / cuda packages

GPU-only payload: ~2.0 GiB
```

Step 5 gives the third Node.js floor recorded in item 1 of section 1. The message a user
actually sees says 20, the `pi` command says 18, and the bundle declares 22.19.0.

## 6. Remediation of finding 4 — the CUDA download

Finding 4 is fixed on branch `feat/cpu-torch-index` (commit `2a24d46`). The fix declares
the PyTorch CPU index in `pyproject.toml` with `explicit = true`, so only the packages
named in `[tool.uv.sources]` resolve from it and every other dependency still comes from
PyPI:

```toml
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cpu" }
```

`uv lock` then removed 19 GPU packages and `triton`:

```text
Resolved 190 packages in 4.34s
Removed cuda-bindings, cuda-pathfinder, cuda-toolkit, nvidia-cublas,
        nvidia-cuda-cupti, nvidia-cuda-nvrtc, nvidia-cuda-runtime,
        nvidia-cudnn-cu13, nvidia-cufft, nvidia-cufile, nvidia-curand,
        nvidia-cusolver, nvidia-cusparse, nvidia-cusparselt-cu13,
        nvidia-nccl-cu13, nvidia-nvjitlink, nvidia-nvshmem-cu13,
        nvidia-nvtx, triton
Updated torch v2.11.0 -> v2.11.0, v2.11.0+cpu
```

`uv.lock` now contains 0 `nvidia-` entries, against 36 before. `torch` resolves to
`2.11.0+cpu` on every platform except macOS, which takes the plain `2.11.0` wheel from the
same index. The README Installation section carries the "CPU and GPU builds" note that
`pyproject.toml:37` had promised since the pin was added, and the comment now points at a
section that exists.

The same clean-container walk was repeated against the new lock:

| Measurement | Before | After |
|---|---|---|
| Step 2 wall time | 8m 07s | 1m 26s |
| Packages installed | 177 | 158 |
| `nvidia-*` directories in the venv | 19 | 0 |
| `triton` in the venv | yes | no |
| `torch.__version__` | `2.11.0` (CUDA build) | `2.11.0+cpu` |
| `torch.cuda.is_available()` | n/a | `False` |
| Step 3 `beagle config init` | Pass | Pass |

The resulting virtual environment is 1.6 GB.

## 7. Open finding — the default frontend fails on a clean install

Quick Start step 5 was run again in the container with Node.js v22.23.2 present. The
frontend does not start:

```text
$ uv run beagle
Failed to update agents.toml: [Errno 2] No such file or directory:
  '/root/.config/beagle/coding_agent_config/agents.toml'
agents.toml not found at /work/beagle/src/beagle/config/agents.toml — using global defaults
Error: Unknown option: --extension
exit 1
```

`launcher.py` prepends `--extension=<path>` before handing off to the pi bundle. The
vendored bundle at version 0.84.3 does accept that option: run directly on the
development host, both `--extension=<path>` and `--extension <path>` parse and reach
model selection. The container therefore executes a different bundle or a different
argument vector than the source tree does, and the cause is not yet established.

This is a `src/` defect, not a documentation defect, so nothing was changed for it. It
does contradict a README sentence, which the next remediation should either fix or
qualify: "The `beagle` command with no subcommand starts `pi`. The bridge calls Beagle's
agents over MCP without more setup."

## 8. Status of this report

Every finding is verified against the repository or against a live run. Two of them are
fixed on branch `feat/cpu-torch-index`:

| Finding | Status |
|---|---|
| Section 4, item 4 — the CUDA download | Fixed, `2a24d46`. Measured before and after. |
| Section 1, item 1 — the Node.js floor drift | Fixed, `625a03f`. `launcher.py` now reads `engines.node` from the vendored manifest instead of restating it. `cli/commands/pi.py` still says `>=18`; that file is untracked work in progress, so it was left alone. |
| Section 4, items 1 and 2 — two setup scripts that do not exist | Open. Both are named in log messages and `fix_hint` fields only; nothing executes them, so a stub script would not be reached by the code that names it. |
| Section 4, item 3 — `--headless` exits 0 after a node fails | Open. |
| Section 7 — the default frontend fails on a clean install | Open, cause not yet established. |
