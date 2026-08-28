# Release Readiness Code Audit — Beagle 1.4.0 (main @ 2b68900)

Date: 2026-08-28 · Mode: READ-ONLY (no source changed) · Auditor: claude/opus-5
Key question: software errors, bad structures, differences vs Enterprise Linux rules;
release readiness; error list + correction procedure with test/approval/real-world gates.

Readable version: [published audit artifact](https://claude.ai/code/artifact/378de3e4-b526-48b1-9031-9ae16ef75aec)

## PART 1 — Planning & Evidence Log

Plan: inventory and packaging → run every project gate and capture real output → broad-ruleset
lint sweep to find what the project's narrow rule selection hides → correctness-invariant sweep
→ structural analysis (import cycles, duplicate implementations, dead code, complexity) → full
test suite → separate source defects from environment drift by re-running failures against
`src/` → CI and container review against the EL baseline → severity-ranked register →
correction procedure.

Examined: `pyproject.toml`, `Makefile`, `qup.toml`, `.gitignore`, all five
`.github/workflows/*.yml`, `docker/Dockerfile`, `docker/docker-compose.yml`,
`scripts/prepare_public_release.sh`, `scripts/check_host_paths.py`, `src/beagle/code_mode.py`,
`src/beagle/webhooks.py`, `src/beagle/utils/atomic.py`, `src/beagle/permission_context.py`,
`src/beagle/config/paths.py`, `src/beagle/config/defaults.py`,
`src/beagle/config/_config_path.py`, `src/beagle/core/workflow_loader.py`,
`src/beagle/context/context_window.py`, `src/beagle/context/rag_staleness.py`,
`src/beagle/security/binary_validator.py`, `src/beagle/security/deserialization_guard.py`,
`src/beagle/core/transports.py`, `src/beagle/core/a2a_protocol.py`,
`src/beagle/infrastructure/health_check.py`, `src/beagle/startup/health_check.py`,
`tests/test_spotless_sp4_no_sys_path_hacks.py`, plus AST sweeps over all 378 source modules.

Queries run: `ruff check src/beagle/ tests/`; `ruff check --select` (43 rule families,
`--no-cache`); `mypy src`; `bandit -r src/beagle`; `vulture --min-confidence 90` and `60`;
`make banned`; `pytest --timeout=120` (full); `PYTHONPATH=src pytest <failing set>`;
`scripts/check_host_paths.py` (working tree and detached `HEAD` worktree); AST + Tarjan SCC
import-cycle analysis; AST function-length and singleton-lock analysis; `zipfile` inspection of
`dist/beagle-1.4.0-py3-none-any.whl`; `aiohttp` exception-hierarchy introspection in the project
interpreter; `git ls-files`, `git worktree`, and `git status` in `~/.config/beagle`.

Initial answer to key question: **no-go** — static hygiene is excellent, but the defects cluster
in packaging, gate integrity, and supply chain rather than in the code itself.

## PART 2 — Final Audit Report

### 0. What Beagle is — architecture correction

This audit initially read `bridges/goose_launcher.py` as the spine and described Beagle as an
orchestrator that *drives* Goose. That is wrong, and the error came from entering the codebase
through the defect list rather than the wiring. The maintainer corrected it; the code confirms
the correction.

Beagle is the **capability and context plane**. The agent front end is external and swappable —
Goose CLI (Block) is a *thin agent terminal*, one of several interchangeable consumers.
Integration runs on two axes:

1. **Capability, over MCP.** `docker/MCP_WIRING.md` states it directly — "beagle as the primary
   agentic inference plane". `beagle-rag:8421` and the utility/coord servers are consumed
   identically by goose, code-server, open-webui and openclaw, all under mandatory bearer auth
   (fail-closed). `goose mcp list` is the integration test; `goose_launcher.py` is a
   convenience, not the architecture.
2. **Prompt substrate, via render targets.** `style_guides/targets/base.py` defines a
   `RenderTarget` Protocol — frozen `EmitOptions`, a pure/offline contract, lazy registration to
   avoid a cycle with `render.py`. Four targets ship: `goosehints`, `claude_md`,
   `top_of_mind_xml`, `mcp_resource`. Beagle renders doctrine from the TOML SSOT into whatever
   shape the host harness reads, and manages that harness's context (folding, compaction,
   rehydration).

The intended relationship is symbiotic: Beagle supplies capability, context and doctrine; the
front end supplies the terminal and the model loop. Adding a harness (Claude Code, Codex, pi)
is one `FileRenderTarget(name=..., filename=...)` line for delivery — `EmitOptions.layers`
(global → directory → task) already models the hard part. The remaining work is content shape,
since `.goosehints` and `CLAUDE.md` are XML pointers while Codex and Cursor expect different
conventions (`AGENTS.md`, `.cursorrules`).

**This reframing raised the weight of two components the original scoring under-rated** and
adds **D-35**, the structural obstacle to the swappable-front-end thesis. Both were then audited
on a second pass: `style_guides/render.py` is now assessed (D-37, D-38, D-39), and the render
targets turned out to be **properly tested** after all — see the D-14 methodology note.

### Executive Summary

Beagle 1.4.0 is **not ready for release**, but the failure is concentrated in the last mile
rather than in the engineering. Static hygiene is genuinely strong: `ruff` under the project
config reports one finding, `mypy` exits 0 across 377 files, `bandit` reports 0 High and 0
Medium, `make banned` passes, and a Tarjan SCC over the AST import graph finds **zero import
cycles across 377 modules**. Every outbound HTTP call site carries an explicit timeout, path
containment uses `Path.is_relative_to` with the reasoning documented inline, and there is no
`shell=True`, `eval`, `pickle`, unsafe `yaml.load`, `utcnow`, or MD5/SHA-1 anywhere in source.

The blocking defect is an unbounded busy-wait in `code_mode.py` that hangs the tool-call
executor permanently on two reachable paths, in a 473-line module with no test file. Behind it
sit four instrument failures: the doctrine-gates workflow is red on `main` (34 host-path
violations at `HEAD`, and the allowlist it names does not exist); the security-audit workflow
emits an empty SARIF by construction, so the GitHub Security tab is structurally blind; the
clean-room CI job installs the committed proprietary wheel it exists to exclude, and never runs
a workflow despite its comment claiming it does; and `beagle config init` seeds a config file
but no workflows, returning an empty list indistinguishable from a working one. Verdict:
**NO-GO** until Phase 0–3 below are complete and the full gate battery is re-run green.

### 1.1 Component Assessment

| Component | Score /10 | Evidence |
|---|---|---|
| `config/paths.py` (containment) | 10 | `is_relative_to` on resolved paths; `<invariant>` names the `startswith` bypass; XDG runtime dir at 0700 |
| `utils/atomic.py` (durability) | 8 | Single implementation, mode applied before rename; **missing parent-dir fsync** (D-12) |
| `core/transports.py` (HTTP) | 9 | `timeout` defaulted at every constructor; per-call clients honour per-call timeouts |
| `core/a2a_protocol.py` | 9 | Session timeout env-tunable; the prior no-timeout regression documented at the fix site |
| Doctrine gate layer | 7 | Unusually strong concept; three gates have scope holes (D-04, D-21, SP-4) |
| `style_guides/targets/` (harness abstraction) | 8 | Clean `RenderTarget` Protocol, frozen options, lazy registration; all four targets covered by `tests/test_render_targets.py` |
| `style_guides/render.py` | 5 | Audited on the second pass. Well tested (102 tests across 11 files) and the doctrine logic is careful; but one 2,123-line class, five write paths of which four bypass the project's atomic-write util, and import-time `Path.home()` (D-37, D-38, D-39) |
| `runtime/loader.py` + plugin layer | 4 | Entry-point dispatch and `[runtime].plugin` exist and are correct; goose bypasses them at every layer (D-35) |
| `security/` package | 6 | No injection or unsafe-deserialization primitives; CVE mitigations **untested** (D-14) |
| `permission_context.py` | 3 | Read-only context is denylist-based (fail-open) and has zero consumers (D-15) |
| `code_mode.py` | 2 | Unbounded busy-wait, no timeout, no tests (D-02) |
| `config/loader.py` | 4 | Correct but 623 lines at cyclomatic complexity 55 — the SSOT nobody can safely edit |
| CI workflows | 3 | Empty SARIF by construction; 47/47 actions unpinned; 2 of 5 lack `permissions:` |
| Container image | 7 | Non-root UID 1000, no baked credentials, loopback-only; no `cap_drop`, health check inert |
| Test suite | 6 | 3,481 passing; 12 source-level failures; 35 modules / 6,033 LOC unreferenced |

### 1.2 Security Highlights

Bandit: 0 High, 0 Medium, 56 Low (all `subprocess`/`B404` import notices on argument-list calls).
Verified absent from `src/beagle/`: `shell=True`, `os.system`, `os.popen`, `eval`, `exec`,
`pickle`, `yaml.load` without a safe loader, `datetime.utcnow()`, MD5, SHA-1, hardcoded
credentials, and secret values in log statements. Zero bare `except:` across 270 handlers; the
two `except BaseException` sites are correct cleanup-and-reraise.

Negative findings that matter more than the clean sweep:

- The SARIF conversion step in `beagle-security-audit.yml` loads Bandit and Safety JSON, then
  discards both — the conversion bodies are literal `# ... conversion logic ...` placeholders.
  It uploads `{'version':'2.1.0','runs':[]}` with `continue-on-error: true`.
- `trufflesecurity/trufflehog@main` executes a mutable branch at CI run time; all 47 action
  references across five workflows are unpinned.
- `security/deserialization_guard.py` (the CVE-2025-68664 and CVE-2026-34070 mitigations) and
  `security/binary_validator.py` have **no test coverage whatsoever** — neither `safe_loads`,
  `safe_load_prompt`, nor `validate_goose_binary` appears anywhere in `tests/`.
- `binary_validator.validate_goose_binary` accepts a root-owned binary without checking whether
  it is world-writable, or whether any parent directory is.

### Software Error List (severity = likelihood × impact)

| ID | Sev | Risk | Defect | Evidence |
|---|---|---|---|---|
| D-02 | Critical | Possible × Major | Unbounded busy-wait hangs the executor permanently; `execute_chain` is sequential so no producer can satisfy the wait; two reachable triggers (unsatisfiable dep graph, chain truncation) | `code_mode.py:226-234,203-224,182-197` |
| D-35 | High | Certain × Major | Goose bypasses the documented replaceability model: 1,031 references across 118 of 378 modules, incl. layering inversions where `config/schema.py` and `security/firewall.py` import the runtime plugin, `GoosePool` is the generic subprocess pool, and `GooseExecutionError` sits in core types | `config/schema.py:13,102`, `security/firewall.py:19`, `utils/subprocess_pool.py:1-7,536`, `runtime/loader.py:25,45` |
| D-40 | High | Certain × Major | The "Deep Forks" differentiator is **hardcoded off** and its enabling code is unreachable: `PYRSISTENT_AVAILABLE = False` is a literal, `freeze`/`pmap`/`thaw` are literal `None`, `pyrsistent` is undeclared and uninstalled. Worse, the branch was never O(1) — `freeze()` is an O(n) deep conversion and `thaw()` is another, so the "structural sharing" is destroyed by converting back for LangGraph, making it strictly worse than the `deepcopy` it replaces | `core/graph.py:62-68,176-186`; README:32-34,271 |
| D-01 | High | Certain × Major | `beagle config init` seeds only `config.toml`; never creates `coding_agent_config/metaprompts/`; `list_workflows()` returns `[]` for absent and empty alike — served to **every** front end via four `mcp_utility_server.py` call sites, not just the CLI | `config/defaults.py:152-166`, `core/workflow_loader.py:875-878`, `mcp_utility_server.py:413,522,1516,1778` |
| D-34 | High | Likely × Major | Seven workflows untracked in the config repo — no commit, no remote copy, no reversion path | `~/.config/beagle` git status |
| D-03 | High | Certain × Major | Security-audit SARIF is empty by construction; Security tab structurally blind | `beagle-security-audit.yml:66-101` |
| D-04 | High | Certain × Major | Doctrine-gates workflow red on `main`: 34 host-path violations at `HEAD`; named allowlist absent | `check_host_paths.py:273-280` |
| D-05 | High | Certain × Major | 16 test failures (12 source-level after isolating deployment drift) | full suite, 8m45s |
| D-06 | High | Certain × Moderate | Six tests import `beagle.infrastructure.mcp_openclaw_server`, which exists nowhere | `test_mcp_openclaw_concurrency.py:35`, `test_mcp_e2e.py` |
| D-07 | High | Likely × Major | Proprietary compiled wheel committed to git against the repo's own stated `.gitignore` policy | `git ls-files dist/`, `.gitignore:6-9` |
| D-08 | High | Likely × Major | `prepare_public_release.sh` `git add -A` publishes it; `--force` to public `main`; no cleanup trap | `prepare_public_release.sh:37-52` |
| D-09 | High | Possible × Major | Clean-room CI `pip install dist/*.whl` installs the committed cp313 proprietary wheel; never runs a workflow | `beagle-test.yml:107-140` |
| D-10 | High | Possible × Major | 47/47 actions unpinned incl. `trufflehog@main`; 2 of 5 workflows lack `permissions:` | all workflow files |
| D-11 | Medium | Likely × Moderate | Webhook retry catches `ConnectionError`; `aiohttp.ClientConnectorError` is **not** a subclass — connection failures escape the loop | `webhooks.py:287-289`, verified via MRO |
| D-12 | Medium | Unlikely × Major | `atomic_write_*` never fsyncs the parent dir; rename can be lost on power failure — used for Ed25519 seeds | `utils/atomic.py:40-53,78-91` |
| D-13 | Medium | Certain × Moderate | "Zero-error" mypy gate has no strict flags; untyped function bodies unchecked; 462 `type: ignore` | `pyproject.toml:139-143`, `Makefile:33` |
| D-14 | Medium | Likely × Major | CVE mitigations ship untested: `safe_loads`, `safe_load_prompt` and `validate_goose_binary` appear in **zero** test files by name or by any indirect route. Wider claim requalified — see methodology note | `security/deserialization_guard.py`, `security/binary_validator.py` |
| D-15 | Medium | Possible × Major | `READ_ONLY_PERMISSION_CONTEXT` is denylist-based (fail-open) and has zero consumers | `permission_context.py:20-33,55-59` |
| D-16 | Medium | Likely × Moderate | Health check is `import beagle`; the purpose-built 206-line module is unwired; no `--start-period` | `Dockerfile:76-77`, `infrastructure/health_check.py:169` |
| D-17 | Medium | Possible × Moderate | 33 lazy global singletons with no lock in the module — double-init under the project's own thread pools | `context_window.py:198` +32 |
| D-18 | Medium | Certain × Moderate | `load_config` 623 lines / complexity 55; `run` 426/54; `_run_inner` 454/48; 40 functions >15, 28 >150 lines | `config/loader.py:152` |
| D-19 | Medium | Likely × Moderate | Five artefacts disagree on the Python version; Dockerfile hardcodes `python3.12` site-packages path | `.python-version`, `pyproject.toml:11,17,124,141` |
| D-20 | Medium | Certain × Moderate | `Makefile` tests against `/opt/beagle/beagle_venv`, which holds **1.3.0** while source is 1.4.0 | `Makefile:8-9` |
| D-21 | Medium | Certain × Moderate | `test_no_shell_true.py` scans `.venv/` — all 13 "offenders" are vendor code; source is clean | `test_no_shell_true.py:86` |
| D-22 | Medium | Certain × Minor | `test_requirements_parity.py` asserts against `requirements.txt`, removed in favour of `uv.lock` | 3 × `FileNotFoundError` |
| D-23 | Medium | Possible × Moderate | CVE scanners run `uv lock` first, auditing a re-resolved graph, not the tracked SSOT | `beagle-security-audit.yml:178-207` |
| D-24 | Medium | Possible × Moderate | Compose lacks `cap_drop`, `no-new-privileges`, read-only rootfs, and memory/CPU ceilings | `docker-compose.yml:1-46` |
| D-25 | Low | Certain × Minor | Incremental-ingest cache never written — every ingest is a full re-ingest | `test_hardware_optimization.py:84` |
| D-26 | Low | Certain × Minor | One silent exception handler, flagged by the project's own doctrine gate | `context/rag_staleness.py:538` |
| D-27 | Low | Certain × Minor | Two unawaited coroutines silenced with `type: ignore[unused-coroutine]`; two logging-arity bugs | `context_window.py:228,231,238,239` |
| D-28 | Low | Possible × Minor | 12 `subprocess.run` calls omit `check=`, several in write/validation paths | `utils/file_writer.py:90,135` +10 |
| D-29 | Low | Unlikely × Minor | 8 blocking filesystem calls inside `async def`, two in `firewall.py` | `security/firewall.py:303,330` |
| D-30 | Low | Possible × Minor | `src/beagle/checkpointer.py` (301 LOC) has test-only consumers; duplicate `health_check` modules | `core/graph.py:804` |
| D-31 | Low | Certain × Insignificant | Declared coverage default (28%) is weaker than the enforced one (55%) | `beagle-test.yml:8-11,87` |
| D-32 | Low | Certain × Insignificant | SBOM uploaded to `upload-sarif`; the step's own comment concedes it is not SARIF | `beagle-sbom.yml:99-108` |
| D-33 | Low | Certain × Insignificant | One `F401` unused import; two production `assert`s stripped under `python -O` | `test_disk_full.py:10` |
| D-36 | Low | Certain × Minor | Rendered harness artefacts (`.goosehints`, `CLAUDE.md`, `src/beagle/CLAUDE.md`, `MEMORY_INDEX.md`) are untracked **and** absent from `.gitignore` — permanent `??` noise, and a fresh clone has no pointer files until someone runs `render-prompts` | `git status --porcelain`, `.gitignore` |
| D-37 | Medium | Likely × Major | `render.py` has five write paths; **one** uses the project's atomic-write util. Three hand-roll `mkstemp`+`os.replace` with **no `fsync`**, and `.goosehints` is written with a plain non-atomic `write_text` — goose reads that file at session start, so a concurrent render yields a truncated or empty doctrine | `render.py:28,484-491,742-746,2039,2123,2151-2155` |
| D-38 | Medium | Certain × Moderate | Three canonical paths resolve `Path.home()` at **import time**, so a later `HOME` change (tests, containers, systemd `User=`) never applies. The test suite works around it by monkeypatching module internals at six sites; `test_render_canonical_staleness.py:22` documents the workaround in its own docstring | `render.py:43,1890,1893` |
| D-39 | Medium | Certain × Moderate | `GooseTopOfMindRenderer` is a 2,123-line, 45-method class named for one harness while serving all of them, with goose paths hardcoded and `render_claude_md` switching on a `variant` string literal instead of dispatching through a target — D-35 at file level | `render.py:46,554-565,1827-1854` |
| D-41 | Medium | Certain × Major | TurboQuant's pack/unpack are **pure-Python nested bit loops** — `values × bits` interpreted iterations per call, so a 1000×384 embedding matrix costs ~1.15M inner iterations to compress. The module whose entire purpose is efficiency is the slowest path in it; the codebase's own comment records "~1s for a TurboQuant build". Vectorisable with `np.unpackbits` + reshape | `core/turboquant.py:98-172`; `context/token_counter_subscriber.py:148` |
| D-42 | Medium | Likely × Moderate | **`numpy` is not a declared dependency.** TurboQuant hard-requires it and `compressed_store.py` calls it "OPTIONAL"; it arrives only transitively via `torch`/`faiss-cpu`. Any change to those pins silently disables context folding, degrading to a warning rather than an error | `pyproject.toml` (absent); `core/turboquant.py:38-53`; `context/compressed_store.py:35-44` |
| D-44 | Medium | Possible × Major | **No multiprocessing start method is set anywhere in the tree**, so Linux inherits `fork`. Beacon tests fork while the journal's `beacon-journal-fsync` and the server's `beacon-ring-poller` daemon threads are alive — undefined behaviour that Python 3.13 warns about explicitly ("may lead to deadlocks in the child") and that 3.14 changes out from under the project | `beacon/journal.py:294`, `beacon/server.py:407`; `tests/test_beacon_contact.py`, `tests/test_beacon_store.py` |
| D-45 | Medium | Certain × Moderate | **The test suite segfaults under coverage** — "dumped core" at ~76%, no `cov.json` produced — while the same suite completes cleanly without `--cov`. Coverage is therefore unmeasurable locally, the CI `--cov-fail-under` gate cannot be validated against a local baseline, and D-14's scope stays unsettled | `pytest --cov=beagle` run, 2026-08-28 |
| D-43 | Low | Possible × Moderate | Beacon's `_publish_status` performs `mkdir` + write + `os.replace` **while holding the journal lock**, so the observability path can block the durability path it reports on; the status write is also non-atomic in the D-37 sense (no `fsync`) | `beacon/journal.py:223-240,240-273` |

#### D-14 methodology note — a false positive, and what it costs the claim

The original D-14 figure ("35 modules / 6,033 LOC with no public symbol referenced in `tests/`")
came from an AST cross-reference: collect each module's public symbols, then check whether any
appears as text anywhere under `tests/`. That heuristic **understates coverage for anything
reached through indirection**, and it produced at least one confirmed false positive.

`style_guides/targets/file_targets.py` and `targets/mcp_target.py` were both flagged. They are in
fact covered by `tests/test_render_targets.py` — six tests exercising all four targets through
`TargetRegistry.get(name)` and `renderer.emit(target=...)`. The symbols `goosehints_target` and
`mcp_resource_target` never appear in the test file because the registry resolves them from a
*string*, so the heuristic could not see them. Registry, factory and entry-point dispatch are
exactly the patterns this codebase uses everywhere, so the same blind spot plausibly affects
other modules in the set.

What survives without qualification is the narrower, directly verified claim now in the table:
`safe_loads`, `safe_load_prompt` and `validate_goose_binary` were each grepped for individually
across `tests/` and return nothing — no name reference and no registry indirection, because
nothing dispatches to them dynamically. A coverage run is the only instrument that settles the
wider set, and it must run with `PYTHONPATH=src`: a first attempt against the deployed venv would
have measured 1.3.0, which is D-20 turned on the audit itself.

**Correct the number before acting on it.** Do not treat "35 modules" as a work queue.

### Enterprise Linux baseline differences

Pass: FHS/XDG path discipline, path containment, no command injection, no unsafe
deserialization, cryptographic hygiene, secret management, non-root container execution, SBOM
generation (CycloneDX 1.5, release-attached).

Partial: reproducible build and CVE surveillance — `uv.lock` is tracked as the SSOT, but CI
regenerates it before scanning (D-23).

Fail: least-privilege CI tokens (D-10), supply-chain action pinning (D-10), artefact provenance
(D-07), container hardening (D-24), health/liveness signal (D-16), and the security findings
pipeline (D-03).

### Correction Procedure (with test, approval, real-world gates)

Six phases, ordered so each phase's verification is trustworthy before the next begins.
Phase 0 is mandatory: until the gates are honest, no later phase can be validated.

#### Phase 0 — Restore gate integrity (D-21, D-22, D-06, D-20)

1. Exclude `.venv/`, `node_modules/`, `site-packages/` from the `test_no_shell_true.py` walk;
   scope it to `src/` and `tests/`.
2. Rewrite `test_requirements_parity.py` against `uv.lock` via `uv export`, preserving the
   CVE-floor and CPU-index assertions. Do not delete them.
3. Delete `test_mcp_openclaw_concurrency.py` and the two `mcp_openclaw_server` tests in
   `test_mcp_e2e.py`, or restore the module. Record which, and why, in `CHANGELOG.md`.
4. `make build && make install` so `make test` exercises 1.4.0, not 1.3.0.
5. Create `scripts/host_path_allowlist.txt` with a reason per entry, or fix each of the 34
   literals. Do not widen the gate's scope exclusions to make it pass.
6. **D-45 — root-cause the coverage segfault.** The suite passes without `--cov` and cores at
   ~76% with it, so this is not a test defect but an interaction between the coverage tracer and
   a C extension or a fork. Bisect with `--cov` held constant, halving the test set each pass;
   `faiss`, `kuzu`, `lancedb`, `torch` and `google-re2` are the candidate extensions, and D-44's
   fork-with-threads is the other prime suspect since the tracer adds threads. Fix or isolate the
   offending module with a marker, then re-run to produce `cov.json` — every coverage claim in
   this audit stays unsettled until it exists. Do **not** lower `--cov-fail-under` to route
   around it.
7. **D-44 — set the start method explicitly.** Add `multiprocessing.set_start_method("spawn")`
   (or `forkserver`) at the beacon test entry point and anywhere the package spawns processes.
   Forking a process that holds journal and ring-poller threads is undefined today and an error
   on 3.14. Fixing this may also resolve D-45.

```bash
/opt/beagle/beagle_venv/bin/python -c "import beagle; print(beagle.__version__)"  # 1.4.0
python scripts/check_host_paths.py tests/ src/beagle/config src/beagle/style_guides; echo $?
pytest tests/test_no_shell_true.py tests/test_requirements_parity.py -v
pytest -q 2>&1 | tail -3
# D-45: coverage must complete and emit a report, not core-dump
PYTHONPATH=src pytest -q --cov=beagle --cov-report=json:cov.json; test -s cov.json
# D-44: no fork-with-threads warning survives
PYTHONPATH=src pytest -W error::DeprecationWarning tests/test_beacon_contact.py -q
```

*Approval gate 0:* maintainer confirms `check_host_paths.py` exits 0, the full suite reports
0 failed, and the deployed venv reports 1.4.0. Any test deleted rather than fixed is named
explicitly in the sign-off with its justification.

*Real-world validation:* open a throwaway PR with a one-line no-op change. Confirm all five
workflows run and report green — not skipped, not amber, not `continue-on-error` masked.

#### Phase 1 — Clear the release blockers (D-34, D-02, D-01)

1. **D-34 first — data-loss risk, one minute.** In `~/.config/beagle`:
   `git add coding_agent_config/metaprompts/*.yaml`, commit, **push** to `beagle_config`.
   Seven workflows currently have no copy anywhere else.
2. **D-02.** Wrap the wait in the timeout the class already owns —
   `await asyncio.wait_for(self._wait_for_dependencies(...), timeout=self.timeout_seconds)` —
   and return a failed `ChainResult` on expiry. Make `_resolve_dependencies` mark unsatisfiable
   calls as failed rather than appending them, and make `max_chain_length` truncation reject
   dependents of dropped calls instead of silently orphaning them.
3. Write `tests/test_code_mode.py` from zero: a cyclic dependency graph, a missing `dep_id`, and
   a chain truncated mid-dependency — each asserting bounded completion under `pytest-timeout`.
4. **D-01.** The config-SSOT design stays as it is. Extend seeding rather than moving content
   into the package: have `beagle config init` also create and populate
   `coding_agent_config/metaprompts/`, and make `list_workflows()` distinguish *directory
   absent* from *directory empty*. Delete the stale `workflows_builtin/*.yaml` package-data
   glob. **Do not create `src/beagle/workflows_builtin/`.**

```bash
git -C ~/.config/beagle status --porcelain | grep '^??.*\.yaml'   # must return nothing
git -C ~/.config/beagle ls-files | grep -cE 'secret|\.key|\.pem'  # must print 0
pytest tests/test_code_mode.py -v --timeout=30                    # must PASS, not time out
grep -n "workflows_builtin" pyproject.toml                        # must return nothing
```

*Approval gate 1:* maintainer confirms the seven workflows are **pushed**, not merely committed
— a local commit is not a reversion path. Confirms `test_code_mode.py` exercises all three hang
triggers and completes in bounded time.

*Real-world validation:* on a machine with no `~/.config/beagle` (a fresh container suffices):
install the wheel, run `beagle config init`, then `beagle list-workflows`, then execute one
workflow end to end. It must either run, or fail with an actionable message naming the directory
to seed and the command that seeds it. Silently returning an empty list is a failed validation.

#### Phase 2 — Make the security pipeline honest (D-03, D-10, D-23, D-32)

1. Replace the stub SARIF heredoc with `bandit -f sarif` (native since 1.7.6); delete the
   hand-rolled converter. Remove `continue-on-error` from the upload.
2. Pin all 47 action references to full commit SHAs with a version comment. Replace
   `returntocorp/semgrep-action@v1` with the current `semgrep/semgrep` action. Enable Dependabot
   for `github-actions`.
3. Add `permissions: contents: read` to `beagle-security-audit.yml` and `beagle-test.yml`,
   elevating per-job only where required (`security-events: write`, `issues: write`).
4. Delete `uv lock` from `osv-scan` and `pip-audit-lockfile`; scan the tracked `uv.lock`.
   Correct the false comment claiming it is untracked.
5. Delete the CycloneDX-to-`upload-sarif` step. Replace `safety check` with `safety scan` or
   remove it — `pip-audit` and `osv-scanner` already cover the ground.

```bash
grep -rn "uses:" .github/workflows/ | grep -vcE "@[0-9a-f]{40}"   # must print 0
for f in .github/workflows/*.yml; do grep -q "^permissions:" "$f" || echo "MISSING: $f"; done
bandit -r src/beagle -f sarif -o /tmp/b.sarif
python -c "import json; r=json.load(open('/tmp/b.sarif'))['runs']; assert r and r[0]['results']"
```

*Approval gate 2:* security owner confirms the GitHub Security tab shows **real Bandit
findings** after a run — a non-empty result set, cross-checked against the local count of 56.
An empty tab is now a failure, not a pass.

*Real-world validation:* introduce a deliberate `subprocess.run(cmd, shell=True)` on a scratch
branch. Confirm it is caught by Bandit **and** surfaces in the Security tab **and** fails the
doctrine gate. Revert without merging.

#### Phase 3 — Close the publication path (D-07, D-08, D-09, D-24, D-16)

1. `git rm --cached dist/*.whl` and commit. **Preserve both files on disk** until the maintainer
   confirms the Orpheus wheel exists in another retrievable location — deletion is permitted
   only once a reversion path is verified in a pushed commit.
2. Add a hard guard to `prepare_public_release.sh`: after `git add -A`, assert
   `git ls-files dist/` is empty and abort otherwise. Replace `--force` with
   `--force-with-lease`. Add a `trap` returning to `$BRANCH` and deleting the orphan branch on
   any exit path.
3. Change `minimal-install` to build into a clean directory and install that exact path —
   `python -m build --wheel --outdir /tmp/wheelhouse` then
   `pip install /tmp/wheelhouse/beagle-*.whl` — never a glob over the checked-out `dist/`.
   Extend it to actually run a workflow, as its comment already claims.
4. Add `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `read_only: true` with a
   `tmpfs` for `/tmp`, and explicit `mem_limit`/`cpus` to `docker-compose.yml`.
5. Point both health checks at `python -m beagle.infrastructure.health_check` and add
   `--start-period=60s` to absorb model load.

```bash
git ls-files dist/ | wc -l                                 # must print 0
git ls-files | grep -cE '\.(whl|tar\.gz|so)$'              # must print 0
make image-build
docker run --rm beagle:1.4.0 python -m beagle.infrastructure.health_check; echo $?
docker compose -f docker/docker-compose.yml config | grep -E 'cap_drop|no-new-priv|read_only'
```

*Approval gate 3:* maintainer performs a **dry run** of `prepare_public_release.sh` against a
private throwaway remote and personally inspects `git ls-tree -r --name-only public-release` for
any `.whl`, `.so`, or Orpheus-related path. Legal/licensing sign-off is required before the real
public push — history rewriting after publication is not a remedy.

*Real-world validation:* start the hardened container with a deliberately absent Goose binary.
The health check must report **unhealthy** within three intervals. Under the previous
`import beagle` check it reported healthy; that delta is the proof the fix works.

#### Phase 4 — Correctness and coverage (D-11, D-12, D-14, D-15, D-17, D-25 to D-28)

1. **D-11.** Catch `aiohttp.ClientError` and `OSError` in the webhook retry loop. Regression
   test: patch `session.post` to raise `ClientConnectorError`, assert all retries are consumed
   and a `WebhookDelivery(success=False)` is returned.
2. **D-12.** After `os.replace`, `fd = os.open(path.parent, os.O_RDONLY); os.fsync(fd);
   os.close(fd)`, guarded for platforms without directory fsync. Test with a fault-injecting
   `fsync` mock asserting call order.
3. **D-14.** Write tests for `deserialization_guard` (a blob naming a disallowed object must
   raise; `secrets_from_env=False` must hold) and `binary_validator` (non-executable,
   foreign-owned, and world-writable binaries must all be rejected — the last is a genuine gap
   in the current implementation).
4. **D-15.** Rebuild `READ_ONLY_PERMISSION_CONTEXT` on `allow_names` so it fails closed, then
   wire it to a real caller or remove it from `__all__`.
5. **D-17.** Replace the 33 lazy singletons with a shared `threading.Lock` double-checked
   helper, or `functools.lru_cache(maxsize=1)` where the constructor is side-effect free.
   Prioritise `get_audit_logger` and `get_recorder`.
6. **D-25.** Diagnose the incremental-ingest cache before touching it — a cache that never
   writes means a measurable performance regression is hiding behind this test.
7. **D-26 to D-28.** Log the `RuntimeError`; `await` the two coroutines and delete both
   `type: ignore[unused-coroutine]` comments; fix the two logging-arity bugs; add explicit
   `check=` to all twelve `subprocess.run` sites.
8. **D-37 — do the `.goosehints` write first; it is the only one with a live race.** Replace
   `hints_path.write_text(pointer, encoding="utf-8")` at `render.py:2039` with
   `atomic_write_text(hints_path, pointer, mode=0o644)`. Goose reads this file at session start,
   and the "Don't-Stop Gate" directive is built inline into that same string — a truncated read
   silently drops a behavioural contract with no error anywhere. Then replace the three
   hand-rolled `mkstemp`/`os.replace` blocks (`render.py:484-491`, `742-746`, `2151-2155`) with
   the same util, so all five paths agree and all five `fsync`. `_atomic_write` at `:2123` is
   already correct and is the model. Note this compounds with D-12: fixing `utils/atomic.py`
   alone does not help the four callers that never reach it.
9. **D-38.** Convert the three import-time constants (`render.py:43`, `:1890`, `:1893`) to
   call-time resolvers, following `config/paths.py`, which already does XDG resolution correctly
   at call time. Then delete the six module-internal monkeypatches in
   `test_f5_hydration_ttl.py`, `test_render_prompts.py` and `test_render_canonical_staleness.py`
   and let the tests set `HOME` — a test that has to patch a module's private constant is
   reporting a design defect, not exercising the code.

```bash
pytest tests/test_webhooks.py tests/test_atomic.py tests/test_deserialization_guard.py \
       tests/test_binary_validator.py tests/test_permission_context.py -v
ruff check src/beagle/ --select PLE1205,PLW1510,ASYNC110 --no-cache   # must be clean
grep -rn "unused-coroutine" src/beagle/                               # must return nothing
python -W error::RuntimeWarning -m beagle.context.context_window      # must not warn
# D-37: every write path routes through the util; no hand-rolled temp-rename remains
grep -cn "atomic_write_text" src/beagle/style_guides/render.py        # must be 5 (1 import + 4)
grep -n "mkstemp\|\.write_text(" src/beagle/style_guides/render.py    # must return nothing
# D-38: no import-time home resolution
grep -n "Path.home()" src/beagle/style_guides/render.py               # must return nothing
HOME=/tmp/fakehome pytest tests/test_render_canonical_staleness.py -v # must pass unpatched
```

*Approval gate 4:* reviewer confirms each new test **fails against the unfixed code** before
passing against the fix. A test that passes both ways proves nothing. Coverage of
`src/beagle/security/` must be reported and must exceed the repository average.

*Real-world validation:* point a webhook at a hostname that does not resolve and one at a port
with nothing listening. Both must consume the full retry schedule and return a delivery record.
Run an ingest twice; confirm the second reads the cache — a wall-clock reduction, not a log line.
For D-37, run `beagle render-prompts` in a tight loop while a second process reads `.goosehints`
continuously; the reader must never observe a short or empty file. That race is reproducible
today and is the acceptance test for the fix.

#### Phase 5 — Structural debt, after release (D-13, D-18, D-19, D-29 to D-33)

1. **D-13.** Enable `check_untyped_defs` globally first, then `disallow_untyped_defs` per package
   via `[[tool.mypy.overrides]]`, starting with `beagle.security` and `beagle.config`. Ratchet
   the `type: ignore` count downward with a test that fails if it rises.
2. **D-18.** Decompose `load_config` first — highest leverage. Extract per-section loaders behind
   the existing signature so callers are unaffected. Add `C901` at `max-complexity=15` with a
   documented, shrinking per-file ignore list.
3. **D-19.** Pick one Python floor. Add the 3.13 trove classifier, align `ruff target-version`,
   `mypy python_version`, and the Dockerfile base, and replace the hardcoded `python3.12`
   site-packages path with a `sysconfig` lookup. Extend the version-consistency test to cover the
   interpreter matrix.
4. **D-29 to D-33.** Move blocking filesystem calls in `firewall.py` and `cvcp.py` to
   `asyncio.to_thread`. Decide the fate of the orphaned `checkpointer.py` and the duplicated
   `health_check` modules. Remove the unused import; replace the two production asserts with
   `cast()`.
5. Widen the SP-4 `sys.path` gate to cover `tests/`, picking up the 38 files that hard-wire one
   developer's home directory.

*Approval gate 5:* these must not gate the release. Each merges independently with its own
review. The only binding requirement is that the ratchet tests are added **now**, so the debt
cannot grow while it is being paid down.

*Real-world validation:* clone to a machine that is not the development host, into a path other
than `/home/server/Projects/beagle`, and run the full suite. Every host-path and `sys.path`
defect surfaces here and nowhere else.

#### Phase 5b — The differentiator claims (D-40, D-41, D-42, D-43)

These three are grouped because they share a failure mode: the features the README leads with
under "What makes Beagle different" are, in the shipped tree, either disabled, slower than the
thing they replace, or resting on an undeclared dependency. None is a correctness bug. All three
are credibility bugs, and this project is about to be published.

1. **D-40 — decide, then make the code and the README agree.** `PYRSISTENT_AVAILABLE = False`,
   `freeze = pmap = thaw = None` are literal constants (`graph.py:62-68`), so the entire
   pyrsistent branch at `:176-186` is unreachable. Two honest options, and *only* two:
   (a) **Delete it.** Remove the dead branch, keep `copy.deepcopy`, and rewrite README:32-34 and
   the `:271` table row to describe deep-copy forks without the O(1) claim. This is the
   recommended path — the code already says "removed as a phantom dependency".
   (b) **Make it real.** Declare `pyrsistent`, and keep state *persistent end-to-end* rather than
   `freeze`→`pmap`→`thaw`. The current sequence cannot be O(1) at any point: `freeze` is an O(n)
   deep conversion in, `thaw` is an O(n) deep conversion out, so it does two full traversals plus
   intermediate allocation where `deepcopy` does one. Benchmark before believing it is faster.
   What is not acceptable is leaving unreachable code behind a README claim of O(1) forks.
2. **D-41 — vectorise the bit packing.** `_pack_bit_indices` and `_unpack_bit_indices` loop in
   Python over every value *and* every bit. Replace with `np.unpackbits`/`np.packbits` on a
   reshaped view, or a strided `np.bitwise_or.reduce`. Benchmark a realistic embedding matrix
   (1000×384) before and after and record both numbers — a compression routine that costs ~1s per
   fold is a latency source in the context path, not an optimisation.
3. **D-42 — declare `numpy` in `pyproject.toml`.** It is a hard requirement of TurboQuant and
   `compressed_store`, currently satisfied only by accident through `torch`/`faiss-cpu`. Then
   decide whether context folding is genuinely optional: if it is, the degradation must be an
   operator-visible warning at startup, not a debug log at first use.
4. **D-43 — move the status write out of the lock.** `_publish_status` does `mkdir`, write and
   `os.replace` while holding `self._lock` in `_flush_locked`. Publish outside the critical
   section (or hand it to the fsync thread), and route the write through `atomic_write_text` so
   the durability subsystem's own status file is written as durably as the thing it reports on.

```bash
grep -n "PYRSISTENT_AVAILABLE" src/beagle/core/graph.py   # one definition, or none
grep -n "pyrsistent\|numpy" pyproject.toml                # numpy declared; pyrsistent per decision
pytest tests/test_turboquant_integration.py tests/test_turboquant_sidecar.py -v
python -m timeit -s "import numpy as np; from beagle.core.turboquant import _pack_bit_indices; \
  a=np.random.randint(0,8,384000,dtype=np.uint8)" "_pack_bit_indices(a,3)"
```

*Approval gate 5b:* maintainer confirms **every remaining README claim under "What makes Beagle
different" has been checked against the code**, not just these three. The two found here were
found by reading two of them; the base rate matters. Any claim that cannot be demonstrated gets
reworded or removed before the repository is published.

*Real-world validation:* run a workflow that crosses the fold threshold and confirm from the
sidecar that a TurboQuant fold was actually built and is retrievable — the feature is claimed as
live in the README's architecture table, and no test in the suite exercises the full
fold-then-rehydrate round trip end to end.

#### Phase 6 — Extract Goose to a plugin (D-35, D-36)

Owner direction, 2026-08-28: goose-specific behaviour belongs in a comprehensive plugin
following the pattern the codebase already documents — **core Python, TOML config, automatic
detection and integration, standardised**. That pattern is not new work: `runtime/loader.py`
calls it "axis 2 of the replaceability model", `mcp_plugins.py` states the contract as
"auto-detect, never auto-activate — each plugin owns its console script and config gate", and
`beagle.transports` already carries the proprietary Orpheus transport this way. The defect is
that goose predates the model and was never migrated to it.

Sequence matters: the layering inversions must be cut before the package moves, or the
extraction drags core with it.

1. **Cut the inverted imports first.** `config/schema.py:13` imports
   `runtime.goose_cli.default_goose_binary` — the config schema depending on a runtime plugin.
   `security/firewall.py:19` imports `GooseCliRuntime` to spawn a subprocess for the semantic
   check. Replace both with resolution through the `AgentRuntime` Protocol.
2. **Generalise the pool.** `utils/subprocess_pool.py` is `GoosePool`, its docstring mandates
   that all goose executions route through it, and it reads `config.goose.fallback_chain` and
   `config.goose.binary_path` directly. Rename to a runtime-agnostic `AgentSubprocessPool` and
   move binary/fallback resolution behind the runtime plugin. Move `GooseExecutionError` out of
   `core/orchestrator_types` into the plugin, leaving a generic `RuntimeExecutionError` in core.
3. **Move the config section.** `GooseConfig` (`config/schema.py:102`) becomes the plugin's own
   TOML, read by the plugin, discovered by detection — matching how Orpheus and the MCP plugins
   already gate themselves. `RuntimeConfig.plugin` stays in core as the selector.
4. **Break the hardcoded default.** `runtime/loader.py:25` pins `_DEFAULT_PLUGIN = "goose_cli"`
   and `_discover_plugins()` seeds the map with a hard-imported `GooseCliRuntime()` *before*
   scanning entry points, with goose as the failure fallback. Entry-point discovery must be the
   only path; absence of any runtime plugin is a loud, actionable error, not a silent default.
5. **De-goose the router.** `core/router.py:230-263` carries goose string literals in the
   workflow-routing regexes. Those belong in the plugin's own routing contribution or in TOML.
6. **De-goose the render engine (D-39).** `GooseTopOfMindRenderer` is the render engine for
   *every* harness, named for one, with goose output paths baked into a module constant and two
   `ClassVar`s. The abstraction already exists beside it: `emit()` at `render.py:554` documents
   itself as "the axis-1 interface: a front end (goose, claude, pi, OpenClaw-over-MCP) picks a
   target". Rename the class to `TopOfMindRenderer`, keeping a deprecated alias for one release;
   move the three goose paths into the goose plugin's TOML (they are the plugin's output
   locations, not the engine's); and route `render_claude_md` through the `claude_md` target
   instead of switching on a `variant` string literal and hardcoding `repo/"CLAUDE.md"`. Split
   the 2,123-line class along the seams the method names already suggest — guide selection,
   section rendering, the forbidden-fold cache, and per-target emission.
7. **Add the regression gate.** A doctrine test asserting the identifier `goose` (case-insensitive)
   appears nowhere under `src/beagle/` outside the plugin package and an explicit allowlist.
   Without it the coupling regrows — 1,031 references is what happens when nothing watches.
8. **D-36.** Decide whether rendered artefacts are tracked or generated, then make `.gitignore`
   say so. Either is defensible; the current state is neither.

```bash
grep -rniE '\bgoose\b' src/beagle/ --include='*.py' | wc -l   # baseline 1031, target ~0 outside plugin
grep -rniEl '\bgoose\b' src/beagle/ --include='*.py' | wc -l  # baseline 118 of 378 modules
pytest tests/test_runtime_plugin_isolation.py -v               # the new regression gate
python -c "from beagle.runtime.loader import _discover_plugins; print(_discover_plugins())"
beagle system doctor                                           # must detect the plugin, not activate it
```

*Approval gate 6:* maintainer confirms Beagle still runs end-to-end **with the goose plugin
uninstalled** — a clean failure naming the missing runtime, not an import error or a silent
fallback. This is the acceptance test for the whole replaceability model: if removing goose
breaks core, the extraction is incomplete.

*Real-world validation:* drive the same workflow from two front ends — goose CLI and one other
MCP consumer (code-server or open-webui) — against the same Beagle instance. Both must reach the
same tool surface and produce equivalent results. That is the symbiosis thesis under test, and
it is the thing no current CI job checks.

### 5. Actionable Roadmap (Quick Wins)

| Action | Effort | Expected result | Risk if skipped |
|---|---|---|---|
| Push the 7 untracked workflows to `beagle_config` | S | Reversion path restored | High — total loss on `git clean` |
| Apply `asyncio.wait_for` to `_wait_for_dependencies` | S | Removes the only Critical | Critical — permanent hang |
| Scope `test_no_shell_true.py` off `.venv/` | S | Gate stops crying wolf | Medium — gate ignored |
| `git rm --cached dist/*.whl` | S | Proprietary binary out of the publish path | High — IP leak on release |
| Pin all actions to SHAs; add `permissions:` blocks | M | Supply chain closed | High — CI RCE surface |
| Replace stub SARIF with `bandit -f sarif` | S | Security tab reports reality | High — blind pipeline |
| Rebuild `/opt` venv to 1.4.0 | S | `make test` measures the right build | Medium — gate is misreporting |
| Seed metaprompts in `beagle config init` | M | Fresh installs are functional | High — unusable install |
| Add the `goose`-outside-plugin regression gate (before extracting) | S | Coupling stops growing while Phase 6 runs | Medium — 1,031 refs is the cost of no gate |
| Cut the two inverted imports (`config/schema.py`, `security/firewall.py`) | M | Core stops depending on a runtime plugin | High — blocks the whole extraction |
| Test the two render targets | S | The multi-harness differentiator is exercised | Medium — silent breakage on new harnesses |

### Closing Remarks

The engineering underneath is better than the release state suggests. Zero import cycles across
377 modules, universal HTTP timeouts verified call site by call site, correct path containment
with the reasoning written down, no injection or unsafe-deserialization primitives anywhere, no
bare exception handlers in 270, and a self-enforcing doctrine layer most teams never build.
That is the profile of a codebase written by someone who knows what goes wrong.

What failed is the instruments watching the last mile. The onboarding command seeds a config
file but no workflows, and the empty result is indistinguishable from a working one. The
clean-room CI job written to catch exactly that never runs a workflow. The security pipeline
emits an empty report by construction. The hard gate that must fail a PR is failing on `main`
and being carried anyway. Each is an instrument that reads green — or that nobody reads — while
the thing it measures is unverified.

The cause is not carelessness: the gates were built faster than they were audited. A project
this instrumented accumulates trust in its own dashboards, and once a gate is trusted nobody
re-derives whether it still measures anything. The `|| true` the doctrine forbids never
appeared — but a stub SARIF converter, a `.venv`-scanning linter, and a health check that only
tests `import` achieve the same outcome by other means. Hence Phase 0.

One thing to resist: making a red gate green by narrowing it. Three findings here (D-04, D-21,
and the SP-4 gate's `src/`-only scope) are already gates that pass because they look at less
than they should. The project's own doctrine has the right instinct — a lint finding is a defect
of the input, not of the rule. D-27 is the counter-example living in the tree right now: two
unawaited coroutines silenced with `type: ignore` instead of two added `await` keywords. Small,
and the whole failure mode in miniature.

D-35 deserves a separate closing note, because it is the one finding that is not a defect
against the project's own standard — it *is* the project's own standard, unenforced. The
replaceability model is designed, documented in three places, and correct. `beagle.transports`
proves it works: Orpheus is genuinely optional, genuinely detected, genuinely gated. Goose is
simply older than the model, and 1,031 references across 118 modules is the compound interest
on that. The extraction is unglamorous and it is the highest-leverage architectural work
available, because the swappable-front-end thesis is the product thesis — and today core,
config, and security all know the name of one front end. Note the ordering constraint in Phase
6: nothing about this is hard, but doing it in the wrong order drags core into the plugin.

### Template Self-Audit

The generic audit template pulled hard toward market-positioning and arXiv sections that the key
question excluded; those were omitted rather than filled with speculation, since no market data
is derivable from a repository. The "minimum 5 features" and fixed section list would have
diluted a defect report into a survey. The template would improve by letting the key question
suppress sections outright rather than requiring each to be filled or waived, and by requiring
the **exit code** of every gate cited — that is what separates a measured finding from a read
one.

Four corrections were made during this audit, recorded so a reader can tell which findings were
re-derived:

1. An early reading recorded `check_host_paths.py` as exiting 0 despite reporting violations.
   That was a measurement error — `$?` after a pipeline returns the status of `tail`, not the
   script. Re-measured without the pipe, the gate correctly exits 1. The finding stands but
   inverts: the gate is sound and CI is red, which is worse than a broken gate.
2. D-01 was originally logged as Critical ("the wheel ships no workflows") on the assumption
   that workflows were product content. The maintainer identified this as a misreading:
   `~/.config/beagle` is a deliberate, documented config SSOT with its own versioned repository.
   Re-verified against `CHANGELOG.md:35`, `README.md:730`, and `config/_config_path.py:9-15`.
   Downgraded to High and the Phase 1 remediation rewritten — the original framing would have
   caused the wrong fix, moving operator content into the package.
3. A second finding under D-25 (`missing develop` in workflow discovery) resolved mid-audit when
   the maintainer created the file. Re-run: 2 passed. Source-level failures 13 to 12.
4. The architecture was misread. This audit described Beagle as an orchestrator that drives
   Goose, with `goose_launcher.py` as the spine. The maintainer corrected it: Goose is a thin
   agent terminal that connects to Beagle over MCP, and Beagle is the capability and context
   plane serving any front end. Re-verified against `docker/MCP_WIRING.md`,
   `docker/examples/goose-extensions.yaml`, and `style_guides/targets/base.py`. This added
   section 0 and D-35, promoted the render targets within D-14, widened D-01's blast radius to
   all MCP consumers, and exposed a coverage gap: `style_guides/render.py` (2,241 LOC, the
   second-largest module and the engine behind the primary product surface) was never read and
   is recorded as unassessed rather than scored. The cause is structural — entering a codebase
   through its defect list produces a map of where the bugs are, not of what the system is. A
   wiring document should be read first; `MCP_WIRING.md` would have prevented all of it.
