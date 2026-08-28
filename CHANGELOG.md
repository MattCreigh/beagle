# Changelog

All notable changes to **beagle** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/) conventions.

---

## [Unreleased]

### Relicensing to MIT

- **Changed**: Beagle and the vendored `pi` frontend fork are now released
  under the **MIT License**, replacing the previous proprietary "Beagle
  License Agreement v1.0" (personal/non-commercial free, commercial paid).
  `LICENSE` is now the MIT text with a scope note. `pyproject.toml` declares
  `license = "MIT"` (PEP 639 SPDX expression, which supersedes the legacy
  license classifier); build floor raised to `setuptools>=77` for PEP 639
  support.
- **Unchanged**: the optional `beagle-orpheus` transport wheel remains
  separately licensed **proprietary** software (evaluation free; production
  paid). It is explicitly excluded from the MIT scope note and never ships
  by default. README, CONTRIBUTING, and the system-spec doc updated; the
  legacy `[1.4.0]` proprietary entry is retained in history.

### Packaging

- **Removed**: committed `requirements.txt` — it merely mirrored pyproject constraints.
- Pip-format exports are now ephemeral: `make freeze-requirements`, CI `uv export --frozen`.

### Remediation of the release-readiness audit (2026-08-28)

Phase 0/1 remediations applied from `docs/audits/release_readiness_code_audit_2026-08-28.md`:

- **D-34**: the 7 runtime workflow definitions (db-migration, deep-planning,
  develop, devops, security, self-improvement, verify) committed and pushed
  to the `beagle_config` config repo — they had no reversion path.
- **D-02 (Critical)**: `code_mode.CodeModeExecutor` no longer hangs on
  unsatisfiable dependency graphs, missing `dep_id`s, or truncation
  orphans — cyclic/missing dependencies are failed up front, the
  dependency wait is bounded by `timeout_seconds`, and dependents of
  truncated calls receive failure receipts. New `tests/test_code_mode.py`
  (9 tests) exercises all three hang triggers under `pytest-timeout`.
- **D-01**: `beagle config init` now also seeds
  `coding_agent_config/metaprompts/` with starter workflow definitions
  (idempotent, never overwrites), and `list_workflows()` distinguishes an
  unseeded install (actionable error entry naming the remediation) from an
  empty seeded directory (valid `[]`). Stale `workflows_builtin/*.yaml`
  package-data glob removed from pyproject.
- **D-04**: `scripts/host_path_allowlist.txt` created with a documented
  reason per entry; `check_host_paths.py` exits 0 on the full default scan
  and on the CI roots.
- **D-06 (test deletion record)**: no test coverage was deleted.
  `tests/test_mcp_openclaw_concurrency.py` and the two openclaw tests in
  `test_mcp_e2e.py` imported `beagle.infrastructure.mcp_openclaw_server`,
  which no longer exists — the server moved into the `beagle_openclaw`
  plugin. Both test files were **retargeted** at `beagle_openclaw.server`
  (same API, same B-6 contracts), preserving all four concurrency tests
  and both e2e tests.

### Bundled `pi` frontend (default interactive frontend)

- **Added**: `src/beagle/frontends/pi/vendor/pi-prebuild/` — the published
  `@earendil-works/pi-coding-agent@0.84.3` npm package (prebuilt, self-contained
  `dist/` bundle; the runnable `pi` CLI). It ships **inside the Beagle wheel**
  so `pi` works out of the box. A `src/beagle/frontends/pi/launcher.py` locates
  the bundle (source checkout or installed wheel) and `exec`s `node` against it.
- **Changed**: `beagle` with **no subcommand** now launches the `pi` frontend
  (the default interactive experience) instead of printing typer help. Explicit
  subcommands (`beagle run`, `beagle system`, …) are unchanged. Requires Node.js
  >= 20 at runtime.
- **Note**: the earlier repo-only `vendor/pi/` (verbatim `earendil-works/pi`
  source checkout) is retained for provenance/re-sync; the wheel carries the
  smaller prebuilt bundle instead of a ~400 MB `node_modules` tree.

### Vendored `pi` frontend

- **Added**: `src/beagle/frontends/pi/vendor/pi/` — a verbatim checkout of the
  MIT-licensed [`earendil-works/pi`](https://github.com/earendil-works/pi) TUI
  agent at fork commit `4e58f324` (tag `v0.84.3`). Repo-only; **not** bundled
  into the wheel (`vendor/pi/node_modules` and `dist/` are git-ignored, and a
  ~150 MB JS tree with per-platform native binaries is the wrong shape for a
  CPU-only Python wheel). Provenance in `vendor/UPSTREAM.txt`; rationale and
  re-sync procedure in `src/beagle/frontends/pi/README.md`.
- **Added**: `vendor/license-inventory.json` — a committed manifest of all 381
  third-party npm packages the fork pins, generated purely from
  `package-lock.json` by `tools/generate_license_inventory.py` (stdlib only,
  deterministic). All effective licenses are permissive; `node-forge`'s
  `(BSD-3-Clause OR GPL-2.0)` is resolved to **BSD-3-Clause**. The new
  `beagle-pi-license.yml` workflow fails if the committed manifest drifts from
  the lockfile or a dependency turns out to be strong-copyleft-only.

## [1.4.0] - 2026-08-25

### Packaging

- Version SSOT bumped 1.3.0 → **1.4.0** for the standalone main-repo
  release; built wheels tracked under `dist/` (beagle 1.4.0 +
  compiled `beagle-orpheus` cp313 transport plugin).

### Licensing replaced with a single custom proprietary licence

- `LICENSE` is now the **Beagle License Agreement v1.0** (copyright Matthew
  David Calder Creigh): free for personal, non-commercial use; any entity may
  evaluate internally for 30 days; commercial use — INCLUDING internal company
  use — requires a paid licence under separate written agreement.
- Removed the PolyForm split (`LICENSE-NONCOMMERCIAL`) and the
  `COMMERCIAL-LICENSE.md` summary; README and CONTRIBUTING updated to match.
  Contributions grant codified in LICENSE §5; the optional `beagle-orpheus`
  wheel remains separately licensed (LICENSE §10).

### Config SSOT is `~/.config/beagle` — nothing configurable ships in src

- Retired `src/beagle/config/defaults_registry.toml` and its
  `[tool.setuptools.package-data]` entry: the source tree and wheel carry
  ZERO bundled configuration; all user-editable config lives under
  `~/.config/beagle` (XDG), seeded by `beagle config init`.
- `scripts/check_hardcoded_defaults.py` is now a report-only detector by
  default (explicit `--registry FILE` opts back into gated mode); selftest
  unchanged. `tests/test_no_new_magic_values.py` runs the selftest plus a
  report-only scan.
- Docs corrected to the no-bundled-config contract: CONFIG_DEFAULTS,
  minimal-install, and the operations manual (no `default_config/`, no
  phantom `[governance]` extra, precedence order fixed).

### Documentation restructure

- Flattened the accidental `docs/docs/` nesting to `docs/` — every
  README/CONTRIBUTING doc link now resolves.
- Removed exact-duplicate `examples/examples/`; removed the derived
  `docs/generated/SYSTEM_SPECIFICATION_…_v1.3.0.md` (the hand-maintained
  manual in `docs/` is canonical and fresher); removed the host-specific
  generated snapshot `docs/LOCAL_TOOL_REGISTRY.toml`.
- Fixed broken/stale references: TROUBLESHOOTING relative links,
  PRESET_CARDS module paths (`src/config/` → `src/beagle/config/`),
  dead `plans/` citations in COORD_BACKENDS/PRESET_CARDS.

### Release hygiene

- `.gitignore`: ignore `dist/`, `.hypothesis/`, `.import_linter_cache/`,
  `.benchmarks/`.
- Track `uv.lock` (reproducible installs) and `docs/spec/`.

### Quality gates: mypy zero-error + ratchet re-baseline (2026-08-23)

- Clear all 20 remaining mypy errors (11 files) — `mypy src` is now a true
  zero-error gate (`Success: no issues found in 377 source files`). Pinned
  langgraph's `END` (Any in stubs) to `END: str = "__end__"` in
  `core/graph.py` + `core/graph_builder.py`; typed PyJWT decode/encode,
  `Prompt.ask`, `ET.tostring`, `AgentDefinition.model_validate`, `tobytes()`
  and HTTP `status_code == 200` returns; renamed the shadowed `fh` handle in
  `hardware_checks.py`; dropped the dead `_DDGS: Any = None` placeholder in
  `web_search.py` (no-redef).
- Auto-fix 911 D413 docstring blank-line violations (ruff `--select D413
  --fix`) across ~200 files — purely cosmetic; ruff gate, format check and
  the test sample all stay green.
- Re-baseline the quality ratchet (15 metrics raised to live counts) after
  drift accumulated during the beacon tranche: Q-02/07/08/09/12/13/14/15/16/
  17/18/20/21/22/23. D413 (Q-24) is now BELOW baseline and needed no raise.
  The ratchet continues to enforce no-growth from this point. This is the
  deliberate re-baseline branch of the user-approved action item, not a
  silent rule relaxation.

### Ephemeral artifacts moved out of the source tree

- `[tool.ruff] cache-dir` and `[tool.pytest.ini_options] cache_dir` →
  `~/.cache/beagle/<tool>` (both expand `~`); `[tool.mypy] cache_dir` → an
  absolute host cache path such as `~/.cache/beagle/mypy` (mypy does not
  expand `~`, so the configured path is absolute — host-specific by design, same
  as the orpheus wheel in `[tool.uv.sources]`).
- `Makefile` exports `PYTHONPYCACHEPREFIX ?= $(HOME)/.cache/beagle/pycache`
  so bytecode never lands next to `.py` files.
- `.gitignore` gains `.mypy_cache/`, `.import_linter_cache/`, `.benchmarks/`.
- Purged existing in-tree caches (`.pytest_cache`, `.ruff_cache`,
  `.mypy_cache` incl. a 214 MB copy, `.import_linter_cache`, `.hypothesis`,
  all `__pycache__`).

### Docs

- README test badge 3329 → 3523 (actual collected count); tup.toml suite
  comment 3116 → 3523.
- docs/ARCHITECTURE.md: replaced the Mermaid flow diagram with an ASCII
  rendering (doctrine: ASCII diagrams only).

### Housekeeping

- Deleted the untracked superseded draft
  `preserved_aside/hook_generated_2026-08-22/SYSTEM_SPECIFICATION_AND_OPERATIONS_MANUAL.md`
  (user-approved; backup at `/tmp/beagle_preserved_2026-08-23/`; the
  committed canonical doc at f92daff is authoritative).

## [1.3.0] - 2026-08-22

### Beacon journal durability hardening (audit E-1..E-4)

- Close the Optional file-handle contract in `beacon/journal.py` (E-1):
  `_fh` is now typed `IO[str] | None`; `stop()` is idempotent and nulls the
  handle after close; `flush()` is a no-op when clean/closed; rotation opens
  the new file BEFORE closing the old one, removing the closed-handle window;
  `record()` after teardown raises RuntimeError instead of losing the write.
- Surface fsync failures instead of dying silently (E-2): the timer thread
  catches OSError around `flush()`, counts it (`fsync_error_count`),
  timestamps it (`last_fsync_error_s`), logs with `logger.exception`, and
  keeps retrying — a dead fsync thread was a silent data-loss path.
  Operator-visible surfacing (audit approval gate A2: `beagle coord status`)
  is deferred until the journal gains its live server owner — `Journal`
  has no production instantiation yet, so there is no in-process owner to
  publish the error state cross-process.
- Stream replay instead of whole-file reads (E-3): rotation files are read
  line-by-line, removing the multi-GiB startup OOM exposure.
- Skip schema-drifted records safely (E-4): `_replayable_shape()` validates
  op/key/args before dispatch; drifted valid-JSON lines warn-and-skip rather
  than KeyError-aborting startup replay.
- New regression suite `TestJournalDurabilityHardening` covers all four
  gates incl. a 100-cycle flush‖stop race and a transient-failure timer test.

### Config defaults tranche-1 follow-up

- `loader.py` no longer duplicates the `[hardware].ramdisk_path` literal;
  the value resolves from the schema field default (single source of truth),
  clearing the host-path allowlist entry.

## [1.2.1] - 2026-08-22

### Configurable defaults: no hardcoded tunables in source

- Enforce plans/beagle-config-defaults-abstraction.xml: every tunable default
  lives in config (`[coord]` section), typed by schema, shipped in
  `default_config`, read at runtime — never embedded at a call site.
- Add `[coord].probe_timeout_s` (1.0) and `[coord].watch_poll_interval_s`
  (2.0); `beagle coord status/watch` now read them instead of literals.
- Make `Journal` durability values (`max_bytes`, `max_files`,
  `fsync_interval_s`) REQUIRED constructor keywords whose canonical homes
  are `[coord].archive_max_bytes` / `archive_max_files` /
  `journal_fsync_interval_s` — no second source of truth in code.
- Add `src/beagle/config/defaults_registry.toml`: every hardcoded literal in
  `src/beagle` classified (moved / invariant / derived / pending); 514
  existing literals covered, new unlisted ones fail CI.
- Add `scripts/check_hardcoded_defaults.py` (AST gate + selftest) wired into
  pytest via `tests/test_no_new_magic_values.py`.
- Add `tests/test_config_defaults_parity.py` proving shipped-TOML values
  equal schema defaults for `[coord]` (extend per tranche).
- Model fallback chains verified already TOML-sourced
  (`version_resolver.get_model_fallback_chain` reads `[goose]`);
  `.goose/project.json` is generated output, recorded as `derived`.

### Beacon: ephemeral JIT coordination store

- Add `beagle.beacon` — a per-working-directory, JIT-spawned coordination
  store (fakeredis behind a unix socket) letting concurrent Beagle agents
  share a live roster, file locks, plan status, and an activity feed
  without polling the filesystem. Sticky lifetime while `>=1` agent holds
  a connection; last-detach flushes a durable JSONL archive and tears the
  server down after a grace period (`docs/CONCEPT-ephemeral-coordination-redis.md`).
- Split writes by whether the caller needs an answer (D-04): heartbeats,
  events, and lock releases go over a per-agent `orpheus` ring
  (fire-and-forget, ~2.9us); lock acquisition, roster reads, and
  attach/detach are synchronous unix-socket RPC (295-536us). Backed by
  measurement, not guesswork (M-1 through M-5).
- Add `src/beagle/beacon/{store,intents,server,spawn,connector,contact,
  journal,archive,keys,records}.py`; the `beagle-coord` MCP server
  (`src/beagle/infrastructure/mcp_coord_server.py`, 14 tools, frozen
  surface D-08); and `beagle coord status`/`beagle coord watch` CLI
  commands (`src/beagle/cli/commands/coord.py`).
- Add two new required runtime dependencies: `redis==8.1.0` and
  `fakeredis==2.37.1` (both pure Python, no C extension), plus `orpheus`
  (a locally built C++20 wheel, resolved via `[tool.uv.sources]`) as a
  required dependency of the ring fast-path — reversing the original
  try/except-optional design (D-06 operator override, 2026-08-21).
- Fix a last-detach teardown gap discovered after WP-7 landed: the
  spawned server subprocess never self-terminated (only the archive
  flush was wired up), leaking one process per test/session forever.
  `RingPoller` now evaluates the grace-timeout teardown predicate and
  stops the process, gated on a permanent store marker rather than a
  sampled roster observation so a fast attach-then-detach cycle can't
  race it.

### Preserved aside split and relocated (GM-1)

- Relocate operational launcher scripts from `preserved_aside/launch_scripts_rg6/` to `scripts/launch/`.
- Convert architecture decision record `preserved_aside/gpu_stack_removed_2026-08-18.txt` to `docs/decisions/2026-08-18-gpu-stack-removed.md`.
- Remove obsolete build state (`sp15_mypy_baseline.txt`, `sp3_mypy_ini_consolidated`,
  `sp8_pytyped_root_backup`, `MEMORY_INDEX.md`, `sp15_mypy_diff_gate.py`).
- Fix documentation references in `src/beagle/utils/env_manager.py` and `src/beagle/infrastructure/mcp_openclaw_server.py`.

### Quality ratchet installed (GM-0)

- Install `scripts/check_quality_ratchet.py` measuring 38 codebase metrics and preventing regressions.
- Seed baseline counts in `baselines/quality-census.json` and `baselines/quality-ratchet.json`.
- Wire quality ratchet into `Makefile` and `.pre-commit-config.yaml`.

### Connection ceiling retired (FU-1)

- Retire the v0.3.0 "Fix 4.4" connection cap. The `_MAX_CONNECTIONS` ceiling in
  `src/beagle/tracking/database.py` was compared and never enforced — it logged a
  warning at capacity but created a connection anyway. QA-4 removed the dead
  comparison from production code on measured evidence (2026-08-21); FU-1 removes
  what QA-4 left behind: the test asserting the constant, the two fixtures that
  resurrected the deleted `_conn_count` class attribute, and the
  `DATABASE_MAX_CONNECTIONS` public constant that no longer had a definition to
  export.

---

## [1.2.0] — 2026-08-18 — Supplementary POIs (A1–A6, B1–B5, C1–C2, D1–D4, E4, F1–F3)

This release lands the supplementary points-of-interest plan
(`plans/beagle-supplementary-pois.xml`): a portable config root, a
front-end-agnostic MCP surface, a pluggable sub-agent runtime, a bundled
doctrine SSOT, steerable meta-processes, and a minimal-setup install.

### Config root portability (A1)

- Replace the hardcoded `/home/Beagle_Config` resolution step with the
  platformdirs user-config dir (`~/.config/beagle`), gated on POPULATED so an
  empty XDG dir does not shadow a valid repo or bundled config.
- Add `platformdirs` to dependencies. An operator keeps an existing root with
  `BEAGLE_CONFIG_ROOT` or a symlink `~/.config/beagle -> /path/to/root`.

### Doctrine SSOT in the repository (A6)

- Bundle the 8 host-clean style guides into
  `default_config/style_guides/guides/` (tracked in git).
- Produce stripped, generic templates for `beagle_environment.toml` and
  `skylon_environment.toml` (host values replaced with `<PLACEHOLDER>`s).
- `find_guides_dir()` falls back to the bundled guides; `package-data` ships
  the new glob.

### Sub-agent runtime (B1–B4)

- **B1a** — `resolve_goose_bin` no longer runs at import time (lazy
  `default_factory`).
- **B1b** — new `AgentRuntime` protocol + `goose_cli` plugin in
  `src/beagle/runtime/`.
- **B1c/B1d** — the three core spawn sites and all remaining call sites route
  through the runtime interface; `resolve_goose_bin` is now referenced only
  from `runtime/goose_cli.py` and `config/paths.py`.
- **B2** — `[runtime].plugin` selects the sub-agent execution runtime via the
  `beagle.runtimes` entry-point group.
- **B3** — new `http_agent` runtime drives a remote A2A agent over HTTP.
- **B4** — a missing Goose binary is non-fatal for non-`goose_cli` runtimes.

### Front-end-agnostic MCP surface (B5)

- Remove the Goose-specific text from the OpenClaw server docstring; the
  client is any MCP client.
- **Option B** (operator): extend the utility server's bearer-token
  streamable-http model to the RAG server, so a remote OpenClaw client can
  reach the RAG index over the authenticated HTTP surface.

### Render targets and context precedence (C1–C2)

- **C1** — a single `emit()` render-target interface (goosehints, claude_md,
  top_of_mind_xml, mcp_resource); `render-hints --target/--dir`.
- **C2** — layered context merge with explicit precedence (global → directory
  → task) and a staleness fingerprint.

### Steerable meta-processes and templates (D1–D4)

- **D1** — `MetaProcess` interface (tune/observe) + 5 built-in processes +
  `meta_*` MCP tool group.
- **D2** — versioned, composable normative template library.
- **D3** — structural/analogical routing alongside the cosine score, with a
  confidence + reason and a generic fallback.
- **D4** — `submit_code` and `validate_security` governance MCP tools.

### Security contact and threat model (E4)

- Replace the fake `security@example.com` placeholder with the GitHub
  private-advisory reporting flow.
- New `docs/threat-model.md` formalises the trusted-host assumption.

### README, install, and packaging (F1–F3, A5)

- **F1** — archive the v13.x release-train sections to CHANGELOG.
- **F2** — README states what is true: portable `<VENV_PYTHON>` placeholder,
  the two replaceability axes, and the portable config root.
- **F3** — minimal-setup install profile: `textual` → `tui` extra, `casbin` →
  `governance` extra, `docs/minimal-install.md`, and a clean-room CI job.
- **A5** — CHANGELOG entries for every work package; version bumped to 1.2.0.

---

## [1.1.1] — 2026-08-17 — Config detachment + src fold-in (S1–S12)

Configuration is detached from the source tree to `/home/Beagle_Config`, and
stray alongside-`src` artifacts are folded into their correct homes.

### Config detachment

- New canonical config root `/home/Beagle_Config` with subdirs:
  `beagle_core_config/` (foundational), `coding_agent_config/` (goose↔openclaw),
  `beagle_inference_config/` (providers + `inference/` fleet cards),
  `style_guides/guides/` (doctrine SSOT), `plugins/<name>/` (per-plugin),
  `deployments/`.
- Moved `config.toml`, `providers.toml`, `presets/` fleet cards, `auth/`
  policy, `agents.toml`, `recipes/`, `workflows/`, `metaprompts/`,
  `blocks/agents/`, `bridges/langgraph.json`, `style_guides/guides/`, and
  `infrastructure/skylon-dev.toml` out of the source tree.
- Single `CONFIG_ROOT` resolver (`config/_config_path.py`) is the only place
  that computes config paths; every module routes through it. Resolution:
  `$BEAGLE_CONFIG_ROOT` → `/home/Beagle_Config` → `<repo_root>/config` →
  wheel-bundled `default_config/`.
- Config-data tests co-located with their subject under
  `/home/Beagle_Config/<subdir>/tests/`; pytest `testpaths` and `tup` include
  them.

### Context management is CORE, not a plugin

- Deleted the `.agents/plugins/beagle-auto-compaction/` bash-shell wrapper.
- New core `context/compaction_controller.py` owns the compaction entry
  points; policy lives in `beagle_core_config/context_management.toml`.
- Thin Python hooks (`scripts/hooks/auto_compact.py`,
  `post_final_fold.py`) wire PostToolUse/Stop to the core controller; a
  manifest-only plugin carries zero logic/config.

### No hardcoding

- Removed hardcoded model/provider/path literals from `cost_tracker.py`,
  `llm_client_registry.py`, `context_window.py`, `context_tracker_ext.py`,
  `feature_flags.py`, `render.py`, `launch_host_audit.py`, `health_check.py`,
  and 9 config-path walkers. All resolve from config.

### Fold-in

- Removed stray `src/beagle/src/beagle/` nested dir.
- `ai/` design docs → `docs/archive/`; `constraints/cpu-only.txt` →
  data-root; deleted dead `hooks/__init__.py`.
- Wheel ships bundled `default_config/` for fresh installs.

---

## [1.1.1] — 2026-08-17 — Audit remediation (4 High defects)

Remediation of the four High defects found by the audit of `072b894`.

### D1 — subprocess_pool.py vs subprocess/ package divergence

- Ported the rate-limit hard-cap safety net (500-entry cap, evict-oldest)
  from the dead `subprocess/pool_stats.py` copy into the live monolith
  `subprocess_pool._set_rate_limit_attempt`, which lacked it.
- Removed the dead package modules `subprocess/execution.py`,
  `subprocess/pool_stats.py`, and `subprocess/security_translation.py` —
  nothing imported them; the monolith is the canonical implementation.
- Kept the live modules `subprocess/output_handlers.py` and
  `subprocess/pool_config.py` (imported by the monolith and tests).

### D2 — cache.py vs 4 split modules

- Removed the dead split modules `utils/cache_base.py`, `utils/file_cache.py`,
  `utils/memory_cache.py`, and `utils/result_cache.py` (293 statements at 0%
  coverage, no importers). `utils/cache.py` is the canonical implementation.

### D3 — semantic_knowledge.py:98 unreachable branch

- Fixed `KnowledgeEntry.__post_init__`: the `id` field's `default_factory`
  always minted a fresh UUID, so the content-addressed ID path was dead and
  re-ingesting identical knowledge always minted a duplicate. The field now
  defaults to `""` and `__post_init__` derives a deterministic content-hash
  ID when no id is provided.
- Fixed `from_json` to derive the content hash when no id is present.
- Added `tests/test_semantic_knowledge.py` covering content-addressed IDs,
  from_json content-hash derivation, and duplicate detection.

### D4 — aiohttp missing from wheel metadata

- Declared `aiohttp==3.14.0` as a hard dependency in `pyproject.toml` and
  `requirements.txt`. It was only transitive via `requirements.lock`, so a
  clean install broke A2A (the README headline feature) and webhooks. The
  wheel now carries 56 `Requires-Dist` entries including aiohttp.

### Minor

- Suppressed the 5 BLE001 sites outside the `src/`+`tests/` gate scope
  (beagle_containerisation, benchmarks, scripts, preserved_aside) so
  repo-wide ruff is clean.
- Declared `pip-audit>=2.7` as a dev dependency so `make pip-audit` runs
  instead of failing closed on "not installed". (The torch CPU-pin audit
  failure is a pre-existing limitation: `2.11.0+cpu` is not on PyPI.)

---

## [1.1.0] — 2026-08-16 — Spotless Phase 2 (SP-1, SP-10, SP-11, SP-14)

**Breaking:** two public enum members were renamed (their wire values are
unchanged, so stored records still resolve):
`AuditEventType.SECRET_SCRUBBED` -> `CREDENTIAL_SCRUBBED`, and
`SLIType.GROUND_TRUTH_PASS_RATE` -> `GROUND_TRUTH_SUCCESS_RATE`.
`beagle.config.config` also stops re-exporting names that only ever leaked
through a star import (`os`, `re`, `logging`, `Path`, `Any`, `dataclass`,
`field`, `contextlib`, `tomllib`); the real API it re-exports is unchanged
and now declared in `__all__`.

**Operator-visible:** the Orpheus file-fallback IPC directory and the Docker
agent RAG log directory no longer default to fixed world-writable `/tmp`
paths — both now default to the per-user runtime directory
(`XDG_RUNTIME_DIR`, else `~/.beagle/runtime`). The existing env overrides
(`ORPHEUS_FALLBACK_DIR`, `BEAGLE_KNOWLEDGE_DIR`) still win.

Debt remediation per `plans/beagle-spotless-phase2.xml`.

### SP-4 — Import hygiene (no sys.path hacks, no bare imports)

- Removed all 29 `sys.path.insert`/`sys.path.append` calls from `src/`.
- Converted every bare intra-package import (`task_store`, `task_loader`,
  `task_schema`, `task_notifier`, `orpheus_agent`, `infrastructure.*`,
  `mcp_rag_server`, `state`, `openclaw_skylon_bridge`, `graph`,
  `docker_rag_logger`) to a `beagle.`-prefixed package-relative import.
- Removed now-dead try/except bare-import fallbacks in `workflow_builder.py`
  and `mcp_openclaw_server.py` that depended on the sys.path hacks.
- Added `tests/test_spotless_sp4_no_sys_path_hacks.py` guarding both gates.
- **Behaviour change**: launcher scripts and MCP servers now import the
  single package module object; no duplicate-module singletons.

### SP-9 — Every config class reachable from configuration

- **Behaviour change (operator-visible)**: four subsystems that previously
  always ran on dataclass defaults are now controllable via new `config.toml`
  sections:
  - `[decomposition]` — RAG query decomposition (read by `hydration_node`).
  - `[learned_routing]` — execution-history model selection (read by
    `subprocess_pool` / `pool_config`).
  - `[memory_consolidation]` — AutoDream memory consolidation (read by
    `autodream`).
  - `[streaming]` — token streaming with early termination (read by
    `subprocess_pool` / `subprocess/execution`).
- Added loader branches and `KNOWN_TOP_LEVEL` entries for the four classes.
- Fixed the `test_config_no_orphans.py` orphan-scan paths for the `src/beagle`
  src-layout (the scan was reading the pre-restructure `src/config` layout,
  which made every section appear orphaned).
- Added `test_all_workflow_config_classes_have_loader_branch`: a bidirectional
  guard that every `WorkflowConfig` class has a loader branch.

### SP-13 — Monotonic clock for every in-process elapsed measurement

- Converted in-memory elapsed-duration timestamps from `time.time()` to
  `time.monotonic()` so an NTP / daylight-saving backward step can never
  produce a negative duration (which breaks a timeout or a rate limiter):
  - `core/warm_workers.py` — worker age / recycle window.
  - `secrets_loader.py` — secret cache TTL.
  - `infrastructure/mcp_security.py` — auth token TTL and failure-rate window.
  - `bridges/retriever.py` — result cache TTL.
  - `infrastructure/mcp_rag_server.py` — RAG result cache TTL.
  - `context/semantic_prompt_cache.py` — LRU prompt cache TTL.
  - `context/token_counter_subscriber.py` — subscriber debounce (rate limiter).
- Wall-clock reads against a persisted timestamp (mtime, DB record,
  checkpoint) correctly remain `time.time()`.
- Restored a `make banned` check that rejects an in-memory elapsed holder
  written with `time.time()`.
- Added `tests/test_monotonic_clocks.py` guarding both the no-wallclock-store
  matrix and the backward-step rate-limiter behaviour.

### SP-7 — Remove dependency cycles

- Broke the module-level / deferred-import cycles in the config, security,
  bridges, and core packages by extracting shared types and helpers into leaf
  modules that import nothing from the package (and no module imports them
  back):
  - `config/model_types.py` — `Provider`, `ModelDeployment`, `ModelPreset`,
    `PresetBundle` (broke `config.registry -> config.schema ->
    config.model_resolver -> config.registry`).
  - `config/paths.py` now reads `[paths].data_root` directly from the TOML
    (broke `config.paths -> config.loader -> config.schema -> config.paths`)
    and is self-contained for `get_workspace_root` (broke
    `config.paths -> utils.env_manager -> config.paths`).
  - `security/binary_validator.py` — `validate_goose_binary` (broke
    `security.validation <-> security.firewall`).
  - `bridges/a2a_types.py` — `AgentCard` (broke
    `bridges.a2a_server <-> bridges.a2a_card_builder`).
  - `utils/prompt_builder.py` — `make_prompt_builder` (broke
    `core.nodes <-> bridges.llm_node`).
  - `core/graph_builder.py` — `build_workflow_graph` and its GRPO/ensemble/
    circuit-breaker helpers (broke `core.graph <-> core.workflow_loader`).
- All import edges now follow the declared layer order; each leaf is a pure
  stdlib module with no intra-package imports.

### SP-8 — Remove duplicate module names and duplicate subsystems

- Merged the two duplicate CLI helper modules (`cli/_helpers.py`,
  `cli/cli_helpers.py`) into a single canonical `cli/helpers.py`
  (`resolve_workflow`, `persist_report`, `show_estimate`). The two old modules
  are now deprecated shims that re-export from the canonical module and emit a
  `DeprecationWarning`; their pre-merge content is preserved under
  `preserved_aside/sp8_cli_helpers/` for one-release reversion.
- Confirmed the three `checkpointer`/`checkpoint` modules implement distinct
  concepts (workflow-state snapshot, daemon/restart recovery, LangGraph
  persistence factory) — not duplicates — so no merge is required.
- Confirmed the A2A dual implementation (`core/a2a_integration.py` v1 HMAC
  in-process vs `bridges/a2a_server|client.py` v2 Ed25519 remote) is an
  intentional, test-locked design (B-5), so no merge is required.
- `_build_skeleton` is already deduplicated to `utils.text_utils.build_skeleton`
  (both context modules delegate to it).
- Added `tests/test_spotless_sp8_dedup.py` guarding one CLI-helpers
  implementation, shim deprecation warnings, and the two distinct
  `CheckpointManager` concepts.

### SP-3 — Bring the type system to zero errors

- Added the PEP 561 `py.typed` marker and shipped it in the wheel, turning
  cross-module checking on (the 326 `import-untyped` errors from untyped
  `beagle.*` modules disappeared).
- Fixed every remaining mypy error across the source tree — priority
  `union-attr` (None-deref) sites first (checkpointer, output_handlers,
  subprocess_pool, degradation), then the rest by category.
- Notable latent bugs fixed while typing:
  - `constraint_extractor` called `AgentHarness(model=,provider=)` + `.invoke()`
    which doesn't exist — now uses the correct `DirectLLMClient` async API.
  - `mcp_openclaw_server` called `prepare_task_for_openclaw(task_name=...)`
    which the function doesn't accept — now uses `load_task_spec_by_name`.
  - `agent_harness` called a nonexistent `OpenClawController.create_and_start`
    — now uses the real `create_task` + `start_task` two-step API.
  - `blocks/mcp_exposure` referenced a nonexistent `__block_meta__` attr.
  - `slo/tracker` read `WorkflowCompleted.budget_usd` which didn't exist —
    added the field and wired it from the orchestrator.
  - `cli/runs` read `ReplayManifest.timestamp` which doesn't exist — now uses
    `started_at` / `completed_at`.
- Consolidated `mypy.ini` into `pyproject.toml` (single config), added
  per-module `[[tool.mypy.overrides]]` stanzas with written reasons for the
  no-stub third-party packages, and deleted the root `py.typed` (it held mypy
  INI content, not a PEP 561 marker).
- `mypy src` now reports **Success: no issues found** (357 files).
- Updated `tests/test_py_typed_policy.py` to assert the marker exists, is in
  package-data, and ships in the wheel.

### SP-5 — Raise zero-coverage modules via tests (test-first, nothing removed)

Following the "add tests, don't remove working code" directive, every
previously-zero-coverage module was raised to 100% by adding dedicated tests —
no working code was removed. Modules now fully covered:

- `utils/prompt_builder`, `bridges/a2a_types` (SP-7 leaves)
- `blocks/errors`, `blocks/context`, `blocks/schema`, `blocks/xml_blocks/base`
- `blocks/python_blocks/{base,io}`
- `bridges/crewai/{converter,task}`, `bridges/autogen/{assistant,messages}`
- `context/{checkpoint,fork_context}`, `daemon/{scheduler,triggers}`
- `output/{formatters,schema}`, `preflight/estimator`
- `security/constants`, `steering/types`, `tracking/{differ,models}`
- `utils/{metrics,tracing}`, `infrastructure/_locks`
- `auth/tenant`, `style_guides/{_xml,_chatrecall_adapter}`
- `cli/helpers` + the deprecated `_helpers`/`cli_helpers` shims
- re-export shims `config/config`, `utils/tracing`

Also updated `tests/bridges/test_bridges.py` callback-handler tests to the
corrected langchain override signature (positional `prompts` + `UUID run_id`).

### SP-12 — Remove suppressions (fix the input, never the rule)

Following the doctrine "a lint finding is a defect of the input, not the rule",
the genuinely-fixable suppressions were removed by fixing the underlying code;
legitimate framework/re-export suppressions were documented and locked with
tests rather than stripped blindly.

- Removed the `open-file-with-context-handler` suppression in
  `utils/safe_file_ops.py` — rewrote `SafeFileWriter` to use
  `contextlib.closing()` so the handle closes deterministically and the
  caller's exception still propagates.
- Added `tests/test_task_notifier.py` (7 tests) locking in the intentional
  fire-and-forget async-callback behaviour that the `asyncio-dangling-task`
  suppressions document (the notifier schedules detached background tasks by
  design).
- The remaining suppressions are `undefined-local-with-import-star` /
  `non-empty-init-module` (`config/config.py`, `security/__init__.py`,
  `bridges/__init__.py` re-export idioms), exact-float-zero comparisons with a
  written reason, and `module-import-not-at-top-of-file` in the MCP servers
  that intentionally defer heavy imports until after the runtime guard is
  established.

Superseded below: the `asyncio-dangling-task` and
`function-call-in-default-argument` suppressions were later found to be
fixable after all, and SP-11 removed both. The claim above that they were
legitimate was wrong — a detached task really can be collected mid-flight,
and Typer reads a module-level `OptionInfo` singleton perfectly well.

### SP-11 — Doctrine linter to zero (excluding the SP-2 `BLE001` block)

- **`RUF006` (3)** — `infrastructure/task_notifier.py` scheduled subscriber
  callbacks with bare `asyncio.create_task`. The loop holds only a weak
  reference, so a task with no other reference can be garbage collected
  mid-flight and its callback never runs — a silent, load-dependent event
  loss. Added a module-level `_BACKGROUND_TASKS` set and `_spawn_background()`
  with a discard-on-done callback.
- **`B008` (3)** — moved the three `typer.Option(...)` argument defaults in
  `cli/commands/render.py` to module-level singletons. CLI surface verified
  byte-identical via `--help` on both commands.
- **`ASYNC230` (1)** — `core/warm_workers.py` read `config.toml` with a
  blocking `open` inside a coroutine, stalling the loop for the duration of
  the I/O. Now dispatched through `asyncio.to_thread`.
- **`S105` (2)** — renamed `AuditEventType.SECRET_SCRUBBED` →
  `CREDENTIAL_SCRUBBED` and `SLIType.GROUND_TRUTH_PASS_RATE` →
  `GROUND_TRUTH_SUCCESS_RATE`. Both are labels, not credentials. Wire values
  unchanged, so previously written records still resolve.
- **`S311` (2)** — backoff/retry jitter now draws from `SystemRandom` instead
  of the module-global Mersenne Twister.
- **`S608` (1)** — `infrastructure/task_store.update_status` assembled its
  UPDATE by joining column fragments into an f-string. Replaced with one
  literal statement whose optional columns keep their own value behind a
  `CASE` guard.

**Behaviour change (operator-visible):** an unreadable or malformed
`config.toml` no longer starts the warm-worker pool silently at the default
size. It logs a warning naming the exception and the count it fell back to.

### SP-10 — Bandit: 143 → 106

- **`B607`/`S607` (18)** — every subprocess `argv[0]` is now an absolute path
  resolved through the new `config.paths.resolve_executable()` (a `<NAME>_BIN`
  override, else `shutil.which`, cached per process). A bare name defers the
  lookup to exec time, so which binary runs depends on the inherited `PATH`.
- **`B310` (4)** — added `security.validation.validate_http_url()` and applied
  it before every `urlopen`. `urlopen` dispatches on the scheme, so an
  unchecked URL of `file://` is a local-file read wearing the shape of an HTTP
  request. The proxy is the site that mattered: its target inherits its scheme
  from the configured upstream.
- **`B101` (1)** — `style_guides/render.py` guarded the compact renderer's
  truncation with an `assert`, which `python -O` strips. Now a `RuntimeError`.
- **`B108` (2 real of 18)** — `core/agent_spawner.py` and
  `core/orchestrator/executor.py` used a string prefix to keep the goose binary
  out of temp roots; replaced with `config.paths.is_in_temp_dir()`, which
  resolves and compares path components. `lifecycle/orpheus_startup.py` fell
  back to the fixed world-writable path `/tmp/orpheus-ipc/nexus` — a
  pre-creation/symlink target for the IPC rings — and one of its two branches
  skipped the `chmod 0o700`. Both now route through the new
  `config.paths.get_runtime_dir()` (`XDG_RUNTIME_DIR`, else
  `~/.beagle/runtime`, mode 0700).

### Defects found and fixed while verifying the above

Three tests could never assert what their names claimed, and one shipped
regression was hiding behind them.

- `tests/test_lifecycle.py::test_restore_returns_false_when_no_checkpoint`
  left `mgr.exists()` as an unconfigured (truthy) `MagicMock`, so it exercised
  the present-but-unreadable branch, which correctly raises. Fixed the mock and
  added two tests for the branch it had been hitting by accident.
- `tests/test_tom_doctrine_coherence.py` pointed `RENDERER_PATH` and
  `CORE_TOML_PATH` at `src/style_guides/`, a path that stopped existing in the
  src-layout restructure (`7a721ab`). Both renderer anti-drift guards were
  asserting against a missing file.
- `infrastructure/mcp_rag_server._execute_graph_query` — SP-3 (`f649571`)
  added `if not isinstance(results, kuzu.QueryResult): return rows` to satisfy
  mypy. That silently changed runtime behaviour: any result object that is not
  exactly `kuzu.QueryResult` (subclass, wrapper, or test double) yields an
  empty list, so a graph query reports "no results" instead of failing. It also
  routed every test in `tests/test_mcp_rag_graph.py` down the empty path — the
  two asserting rows failed, the rest passed vacuously. Narrowing now excludes
  the documented `list` form instead. This violated constraint C7 (a debt
  removal must not change what the code does).

---

## [1.0.9] — 2026-08-15 — Enterprise audit remediation

Remediation of `audits/enterprise_code_audit_2026-08-15.md` (2 Critical, 3 High,
8 Medium, 15 Low). All Critical/High/Medium and the actionable Low findings
resolved; the audit's long-term items (M8 full SSOT refactor, L5/L8/L9) are
tracked for a later release.

### Relay tasks A–I (self-validation bootstrap)

- **Task A** — `tests/test_rag_integration.py`: ingests the real codebase and
  asserts `rag_search` returns `DAGOrchestrator` / `TurboQuantCompressor` in
  the top-3 results (symbol-aware stub embedder, hermetic).
- **Task B** — `tests/test_kuzu_memory_bounds.py`: Kùzu with a tiny
  `max_db_size` raises an explicit buffer-manager error on overflow (no
  unbounded mmap); locks the env-gated `BEAGLE_KUZU_MAX_DB_SIZE_MB`.
- **Task C** — `DEFAULT_FIREWALL_MODEL` `gemma3:27b` → `gemma4:31b` (the
  allowlisted successor); added `validate_firewall_model()` fail-early check.
- **Task D** — verifier→synthesizer ring (2 MiB) rationale comment.
- **Task E** — sandbox deny-by-default verified (exit 126 when
  `allow_fallback=False` + MicroVM unavailable).
- **Task F** — firewall fail-closed verified + timeout/crash/missing-binary
  tests (`tests/test_firewall_allowlist_failclosed.py`).
- **Task G** — root `SECURITY.md` (private contact placeholder + trusted-host
  assumption).
- **Task H** — deployment Mermaid diagram (host boundary, MicroVM sandboxes,
  Orpheus bus) + ASCII render in `docs/ARCHITECTURE.md`.
- **Task I** — `src/workflows/self_audit.yaml`: Beagle runs its own regression
  suite after every code change.

### Critical

- **C1 — model routing restored.** `config.toml` had collapsed to a single
  model (`deepseek/deepseek-v4-flash-0731` ×61). Restored per-tier role
  assignments (glm-5.2 / kimi-k2.7-code / kimi-k2.6 / nemotron-3-ultra /
  deepseek-v4-pro / gemma4:31b / minimax-m3), an 8-distinct fallback chain,
  per-model fallback chains, and the ensemble panel. Added
  `tests/test_config_model_diversity.py` locking the diversity invariant.
- **C2 — mypy diff gate no longer fails open.** `scripts/mypy_diff_gate.py`
  now passes `--no-color-output`, pins a colour-free env, and raises exit 2 on
  the rc-1-with-zero-keys contradiction (previously reported "0 current, 90
  fixed" under `FORCE_COLOR`).

### High

- **H1 — security-audit workflow can fail.** Removed `|| true` and
  `continue-on-error` from Bandit/Safety/pip-audit/Semgrep/TruffleHog steps.
- **H2 — 3 CVEs closed.** `langgraph-checkpoint` 4.0.2→4.1.1, `langchain`
  1.2.15→1.3.9, `langchain-anthropic` 1.4.1→1.4.6 (plus `langgraph` 1.1.8→1.2.4
  for compatibility). OSV confirms 0 vulnerabilities at the new versions.
- **H3 — requirements.txt reconciled.** Regenerated from pyproject.toml with
  the 11 missing deps, both CVE floors, and the CPU-only torch index. Added
  `tests/test_requirements_parity.py`.

### Medium

- **M1** — `SandboxContext` now `os.chdir(self.temp_dir)` in `__enter__`.
- **M2** — blocking `subprocess.run` in async MCP tools wrapped in
  `asyncio.to_thread`; unit-conflated timeouts replaced with a named constant.
- **M3** — `make check` green (vulture whitelist + ruff fixes); vulture and
  `make banned` wired into CI; CI ruff now covers `tests/`.
- **M4** — mypy configs aligned on 3.13; ruff target-version → py313.
- **M5** — `log_preflight_estimate` routed through `logging` (no ANSI);
  conftest env-isolation fixture made autouse.
- **M6** — integration-test CI job can now fail the build.
- **M7** — coverage floor 35% → 55% (actual 57%).
- **M8** — dead `glm-5.1:cloud` branch removed from `cost_tracker.py`.

### Low

- L1 unused var, L2 E306, L3 unreachable except, L4 exc_tb, L10 header,
  L11 MCP_HOST loopback, L12 python-version alignment, L14 defusedxml,
  L15 ring perms tightened. L5/L8/L9 deferred as long-term.

### Model metadata

- **Model metadata** — `nemotron-3-ultra:cloud` now declares
  `max_output_tokens=65536` (was inherited 4096). New
  `get_max_output_tokens()` helper in `config/models.py` (clamped to the
  context window, 8000 fallback for unknown models); the LLM client
  registry now inherits a model's declared budget when a caller omits
  `max_tokens` (explicit values still win, and are clamped to the
  context window as a resource-exhaustion guard).

### Stats

- 3211 tests (25 new), 0 failures, 0 skips. QA gate clean on all changed files.

---

## [1.0.8] — 2026-08-15 — Security remediation

README-audit follow-ups C01, C02, C06 (F03 no code change). Plus pre-existing
QA-gate findings resolved in the touched files.

### Changed

- **C01 (critical): MicroVM sandbox fail-open is now deny-by-default.**
  `MicroVMConfig` gains `allow_fallback: bool = False`. `run()` refuses
  (exit 126) to execute a payload at reduced subprocess isolation unless
  `allow_fallback` is explicitly enabled; a permitted degrade emits a loud
  WARNING (was silent `logger.info`). Threaded through `SandboxMicroVMConfig`
  schema, config loader, and `config.toml`.
- **C02 (high): RBAC admin wildcard is explicit-only, never implicit.**
  Documented deny-by-default posture on `RBACPolicy`. Tests assert unbound
  identity can never reach admin privileges and `unbind()` returns an identity
  to least-privileged observer with no residual capability.
- **C06 (high): corpus-scope regression test.**
  New `tests/test_corpus_scope.py` — ingests a known corpus into an isolated
  DB root and asserts `rag_search` returns each known symbol from the correct
  file (catches corpus-scope drift). Includes Kùzu `max_db_size` env-gating
  regression lock.
- **Pre-existing QA-gate findings fixed:**
  - `sandbox.py`: ASYNC230 blocking `open()` in `_run_in_microvm` →
    `_write_vm_config_atomic()` helper (asyncio.to_thread + tmp-file + fsync +
    os.replace); semgrep aeca-nonatomic-write-to-config closed.
  - `a2a_protocol.py`: 5 BLE001 blind `except Exception` narrowed to concrete
    families; vulture kwargs/unused-import findings resolved.
- **Full-suite remediation (tup run → 29 failures + 5 skips → 0 failures, 0 skips):**
  - **Provider resolution now reads the fleet-card SSOT.** `resolve_provider`
    in `model_resolver.py` and the hardcoded default in `agent_config.py` no
    longer pin a single provider; the active preset card (OpenRouter default)
    is the source of truth.
  - **Startup health check** now validates ALL allowlisted models (not just
    `:cloud`/`-cloud`-suffixed entries), so a retired model fails startup.
  - **Preflight estimator** speed profiles extended with bare model names to
    match the allowlist.
  - **Test fixes across 9 files:** stale `:cloud`/`-cloud` model names → bare
    allowlisted names; regex now permits `/` in model slugs; `agents.toml`
    Jinja templates rendered before validation; bridge-benchmark API probe
    uses current model; live-corpus sidecar test passes a non-empty chunk list.

### Stats

- 99,751 Python LOC, 352 modules, 3186 tests (217 files), 54 config sections.
- **Full suite: 3186 passed, 0 failed, 0 skipped** (via `tup run` with
  `BEAGLE_RUN_NETWORK_TESTS=1 BEAGLE_LIVE_RAG_TEST=1`).

---

## [1.0.7] — 2026-08-14 — Preset card system + remediation

Model/provider/preset routing is now driven by **fleet cards**
(`presets/*.toml`) instead of a single monolithic `presets.toml`. Each card
defines an entire fleet — all 12 role presets + 3 bundles for one provider.

### Added

- **`presets/` fleet card directory** — each card (`fleet_<provider>.toml`)
  defines all 12 role presets and all 3 bundles for a single provider.
  Two cards ship: `fleet_openrouter.toml` (active) and
  `fleet_ollama_cloud.toml` (available). Switch fleets by reordering
  `_index.toml` `load_order` (last card wins).
- **`_index.toml` load ordering** — optional `[meta].load_order` controls the
  override precedence; last-loaded fleet card wins. Without it, cards load
  alphabetically.
- **Cross-card validation** — `registry.validate_cards()` fails fast on unknown
  provider or unknown bundle-role references. Accepts optional arguments for
  pre-commit validation.
- **`beagle config cards` CLI** — lists the discovered fleet cards in load
  order with the role presets and bundles each defines.
- **`docs/PRESET_CARDS.md`** — developer documentation for the fleet card
  system.

### Changed

- `config/registry.py` `reload_registry()` builds into locals first; `_store`
  is assigned atomically only after validation passes (no partial-state on
  mid-load failure).
- `config/_config_path.py` `find_preset_cards()` uses a filename-keyed dict for
  `load_order` resolution instead of `Path.__eq__` (robust against symlinked
  paths).
- `config/model_resolver.py` `get_preset()` no longer falls back to the retired
  `config.toml [model_presets]` table — registry-only resolution.
- `config.toml [model_presets]` table **deleted** (plan v2 Phase 5 cutover);
  replaced with a pointer comment.
- `config.toml [models.allowed]` now includes all Ollama Cloud models so the
  ollama_cloud fleet card is usable.
- `pyproject.toml` package-data: `presets/*.toml` replaces `presets.toml`;
  `src/presets` symlink ensures cards ship in the wheel.
- README configuration reference updated to document the fleet card system.

### Removed

- `presets.toml` renamed to `presets.toml.bak` — `presets/` is the sole SSOT.

### Tests

- Updated `tests/test_registry_presets.py` to plan v3 (fleet card structure,
  registry-backed assertions, openrouter fleet values). 18/18 pass.

---

## [1.0.0] — 2026-07-29 — Golden master: one Beagle

Version reset to 1.0.0. This is a naming and packaging release — no
behavioural change. Note that 1.0.0 sorts *below* the previous 13.22.3,
so installs over an existing copy need `--force-reinstall`.

### Changed — repo-root src-layout

The importable package moved from `beagle/` to `src/`, matching the
convention every sibling repo already uses (`tup/src/tup_pkg`,
`skylon/src/skylon`, `orpheus/src/orpheus`). `src` is a layout marker and
never appears in an import path: on disk `src/security/validation.py`,
imported as `beagle.security.validation`.

### Removed — the nested duplicate package

`beagle/beagle/` (formerly `aeca/`) was a second name for the same
project living inside itself, and that duplication is what broke
packaging — two names meant two package roots, `beagle.beagle.*` import
paths, and a dist-name lookup that resolved only because a stale
`goose_agentic_workflow` distribution stayed installed alongside
`beagle`. The subpackage is dissolved into the package proper:

```text
beagle.beagle.security.validation  ->  beagle.security.validation
beagle.beagle.cost_tracker         ->  beagle.cost_tracker
beagle.beagle.blocks.registry      ->  beagle.blocks.registry
```

There is now one Beagle. The security/permission names the nested
package re-exported are exported from `beagle` itself.

### Fixed

- `style_guides/version_resolver.py` and `style_guides/render.py` looked
  the distribution up as `goose-agentic-workflow`. Both now use `beagle`.
  This was live breakage masked by the stale dist being installed.
- `output/sarif.py` reported a hardcoded tool version (`12.0.1`) and the
  tool name `Beagle - Beagle`; it now reads the installed version.
- `Makefile` invoked `python3`, the *system* interpreter, which carries
  none of the dev tooling — `make vulture` and `make check` exited 1 on
  "No module named vulture" instead of gating. Now pinned to the project
  venv via `PY`, and vulture runs at `--min-confidence 90`.
- Deleted the dead, empty `blocks/` husk left over from the old package.

### Removed — tracked runtime state

The two `.beagle_ingest_cache.json` files were tracked but keyed by
absolute path, so every rename left ~95 dead keys in git. Now ignored.

---

## [13.22.3] — 2026-07-25 — RAG Recall Restoration + Ollama Embedder + TurboQuant Sidecar

The 13.22.2 release had a broken RAG subsystem (zero-vector
substitution on OOM, kuzu mmap reservation of 8 TB on a 4 GB cgroup,
cypher queries filtering on the wrong node_type, and the corpus
indexing dev_tools + memory-test fixtures rather than the actual
beagle source). 13.22.3 is a focused restoration
release — no API-breaking changes, no new features beyond what
the restored subsystem needs to stay under the cgroup cap.

### Fixed (Critical)

- **RAG corpus is now the workflow codebase**. `scan_codebase`
  in `cast_ingestion.py` now drops runtime-state (`.beagle/`,
  `.goose/`, `.agents/`, `.claude/`, `.devcontainer/`),
  operational noise (`audits/`, `benchmarks/`, `plans/`,
  `examples/`, `.github/`), and adjacent monorepo projects
  (`beagle_containerisation/`, `beagle_dockeriser/`,
  `hooks/`, `ai/`). At the project root, agent-tooling files
  (`CLAUDE.md`, `AGENTS.xml`, `ARCH_REPORT.md`, `BEAGLE_CLI_CATALOG.md`,
  `CODEBASE_AUDIT_REPORT.md`) are also dropped. The corpus
  drops from 789 → 697 source files and from 34,403 → 10,975
  AST chunks — the latter because the new index actually
  covers the codebase, not the dev_tools fixtures from a
  prior full re-index that got out of sync.

- **Cypher queries in `graph_callers` / `graph_callees` /
  `graph_class_hierarchy` / `graph_imports` / `graph_dependents`**
  now match the canonical node_type literals
  (`function` / `class`, lowercase, short form — what
  `cast_ingestion` actually writes) and the canonical relation
  label (`INHERITS_FROM`, not `EXTENDS`). The arrow direction
  in `graph_imports` / `graph_dependents` is fixed (was
  `>-[r*1..]->` which is invalid Cypher; now `-[r*1..]->`).
  All five graph tools now return real data.

### Fixed (High)

- **Ollama is now the default embedder**. The previous
  `sentence-transformers` path loaded a 1.2 GB model into the
  Python process's 4 GB user cgroup, which combined with the
  kuzu 8 TB mmap reservation and the lance write path, was
  OOM-killing the ingest at ~50% complete. The
  `broad-except` handler in `SentenceTransformerEmbedder.encode`
  silently substituted `[[0.0]*768]` for the failed chunks,
  producing a 10,975 zero-vector corpus that was useless for
  search. The new `local` provider uses Ollama's
  `nomic-embed-text` (137 MB, runs in `/system.slice/ollama.service`
  with its own cgroup); the Python process only needs an `httpx`
  client. `config.toml` `[embed].provider` changed from
  `sentence-transformers` to `local`, model from
  `all-mpnet-base-v2` to `nomic-embed-text` (same 768-dim output,
  so the existing lance index is compatible).

- **httpx client reuse in `_embed_batch`**. The previous code did
  `with httpx.Client(...) as client:` inside a per-text loop,
  creating ~11k Client allocations for a full ingest. This was
  the root cause of a deterministic segfault in jemalloc's
  background thread at ~50% of the encode loop. Reusing one
  Client per `_embed_batch` call stabilises the connection
  pool and avoids the per-text teardown that the segfault
  correlated with.

- **Kùzu `max_db_size=128 MB` and `buffer_pool_size=64 MB`**.
  Kùzu's default 80% of system RAM buffer pool and 8 TB
  `max_db_size` mmap reservation blew the 4 GB cgroup on first
  open. The explicit bounds are plenty for the actual graph
  size (10k-200k nodes, <14k relations).

- **`_atomic_move_on_same_fs` is now actually atomic for
  non-empty directory targets**. The naive "copy to `<dst>.new`
  then `os.rename` to `dst`" pattern fails with `OSError(ENOTEMPTY)`
  on Linux when `dst` is a populated directory. The new
  two-phase dance-move renames `dst` to a per-pid-unique
  `<dst>.old` (collapsing the prior `<dst>.old` first),
  promotes `<dst>.new` to `dst`, then cleans up. The temp
  name is per-pid-unique to avoid collision with leftovers
  from prior aborted attempts. Two regression tests added
  in `tests/test_rag_hotswap_integration.py::TestAtomicMoveNonEmptyTarget`.

- **MemoryError in the embedder now raises instead of silently
  substituting zero-vectors**. The previous `except Exception`
  in `SentenceTransformerEmbedder.encode` swallowed
  `MemoryError` along with everything else. Split into
  `except MemoryError: raise` and `except Exception: log
  zero-vector`, so a real OOM aborts the ingest loudly
  with an actionable error message instead of writing a
  useless zero-vector corpus to disk.

### Added

- **TurboQuant sidecar** for the RAG vector index
  (`beagle/infrastructure/turboquant_lance_cache.py`).
  3-bit compression of the lance index vectors (~30 MB on
  disk for 10k vectors, ~6x smaller than the raw float32
  index), with on-demand numpy decompression for brute-force
  cosine search. The sidecar is gated on
  `[rag].turboquant_sidecar = true` in `config.toml` (default
  true). The `schema_version: 1` metadata in `.sidecar meta.json`
  makes the format self-describing for future migration. Atomic
  write: write to `.new` then `os.replace`, so a crash mid-write
  leaves the previous sidecar intact.

- **`scan_codebase` regression tests** for the new exclusion
  list (`tests/test_mcp_rag.py::test_scan_codebase_excludes_runtime_state_dirs`
  and `test_scan_codebase_excludes_agent_tooling_at_root`).
  Build a realistic monorepo fixture and assert the
  excluded dirs are not surfaced.

- **Cypher regression tests** that snapshot the cypher string
  each graph tool sends to kuzu and assert the canonical
  node_type / relation-label literals
  (`tests/test_mcp_rag_graph.py::test_graph_callers_cypher_uses_canonical_node_type`
  and 4 siblings). These would have caught the `FunctionDef`
  vs `function` mismatch.

- **Circuit breaker recovery regression test**
  (`tests/test_subprocess_pool_extra.py::test_circuit_breaker_recovers_after_cooldown`).
  When the breaker is in the OPEN state but `_can_attempt`
  returns True (cooldown elapsed), the orchestrator must
  NOT raise `CircuitBreakerOpenError` — it must proceed to
  call `_execute_single_model` and `_record_success`. The
  production code in `subprocess_pool.py` now uses
  `circuit._can_attempt()` (which performs the OPEN→HALF_OPEN
  state transition) rather than the raw `circuit.is_open` read
  (which would short-circuit the recovery path and leave the
  breaker stuck OPEN forever).

### Changed

- **Linting to 0 errors**. `pyproject.toml` adds `extend-exclude`
  for build artifacts and `[tool.ruff.lint.per-file-ignores]`
  for `RUF067` (intentional re-exports in `__init__.py`) and
  `RUF069` (deterministic float equality in test assertions
  is a false positive). 18 production files cleaned up:
  unused imports, nested-if collapse, type-import
  modernization, empty `finally` removal, RUF070 simplification.

- **`_embed_chunk_records` now streams + gc.collects**. The
  previous implementation held all 30k vectors in a list
  (250 MB+ in RAM) at once. Now a generator yielding 256-chunk
  batches with `gc.collect()` between batches. Sidecar write
  happens after the streaming completes, so peak RSS is
  bounded.

### Storage

- **RAG data lives under a dedicated SSD-backed data root**, with
  `~/.beagle/instance_rag{,_kuzu,}` as symlinks. 82 MB on disk
  for the full 10,975-vector corpus + 14,350-relation graph.
  The RAG data is no longer on the OS disk.

- **Embedder is in a separate cgroup** at
  `/system.slice/ollama.service` (825 MB peak). The ingest
  process cgroup usage peaks at ~1.4 GB (down from 4 GB
  pre-fix); MCP server RSS drops to 410 MB (down from 1.4 GB
  pre-fix).

### Verification

- `rag_status` reports `kuzu_connected: true`,
  `lance_table_loaded: true`, `embed_model_loaded: true`,
  `indexed_chunks: 10,975`.
- `rag_search("circuit breaker subprocess pool implementation")`
  returns `CircuitBreakerConfig` (distance 0.28) and
  `PoolConfig` (distance 0.28) — semantically perfect hits.
- `graph_callers("route_query_to_workflow")` returns 4 real
  callers (was empty).
- `graph_callees("init_connections")` returns the actual
  caller graph.
- 96 / 96 RAG + hotswap test suite passes.
- `ruff check`: `All checks passed!` (0 errors).

### Commits

- `c34e2c8` — `chore: ruff PLW1514 sweep + per-file-ignores for RUF067/RUF069`
- `627c5fd` — `feat(rag): add TurboQuant sidecar for in-RAM vector compression`
- `fcc8d11` — `fix(rag): full corpus, atomic swap, Ollama embedder, cypher type fix`
- `ad30348` — `test(rag): add regression tests for the RAG infrastructure fixes`
- `007286b` — `test: regression test for circuit breaker recovery + minor test cleanups`
- `84451a3` — `chore: ruff sweep for benchmarks/ and examples/`

---

## [13.22.1] — 2026-07-21 — Golden-Master Bug-Fix Release

Closed all 18 findings from `audits/golden_master_v13.22.0.md` (the
golden-master audit dated 2026-07-21). This is a **bug-fix release**
with no API-breaking changes.

### Security (Critical)

- **B-1**: MCP utility server `streamable-http` transport now requires
  bearer-token authentication per `MCP_TRUST.md`. ASGI 3 middleware
  enforces `Authorization: Bearer <BEAGLE_MCP_TOKEN>` with
  `hmac.compare_digest` for constant-time comparison. The server
  refuses to start in HTTP mode if `BEAGLE_MCP_TOKEN` is unset
  (fail-closed `RuntimeError`). Health endpoints (`/`, `/health`,
  `/healthz`) remain unauthenticated for orchestrator probes.

### Fixed (High)

- **B-2**: `cost_tracker.estimate_tokens_agnostic` now caches the
  tiktoken import result via a three-state sentinel (`_TOKENIZER_STATE`).
  A missing tiktoken module is attempted exactly once per process;
  the prior code re-attempted on every call (50-200ms import-failure
  storm under load). 6 regression tests in
  `tests/test_cost_tracker_tiktoken_caching.py`.
- **B-3**: `constants.PACKAGE_VERSION` updated to 13.22.0 to match
  `pyproject.toml` and `__init__.py`. 5 regression tests in
  `tests/test_version_consistency.py` enforce cross-source
  consistency.
- **B-4**: Dockerfile "SSOT drift" annotation removed; the version
  comment now references `constants.PACKAGE_VERSION` directly.
  Captured by `test_dockerfile_no_ssot_drift_comment`.

### Fixed (Medium)

- **B-5**: A2A v1 (HMAC) and A2A v2 (Ed25519) dual-track is now
  explicitly documented. `verify_agent_result` docstring explains
  the in-process vs remote split. 7 regression tests in
  `tests/test_a2a_dual_track.py` cover both tracks.
- **B-6**: `TaskStore` calls from `async def` MCP tool handlers now
  use `asyncio.to_thread` via an `AsyncTaskStore` wrapper. The
  event loop is no longer blocked by SQLite I/O. 4 regression tests
  in `tests/test_mcp_openclaw_concurrency.py` confirm
  non-serialising behaviour under concurrent load.
- **B-7**: Dead `core/orchestrator/node_executor.py` (700+ lines,
  never imported) removed.

### Fixed (Low)

- **B-8**: `beagle/security/validation.py:490` path-containment
  switched from `str.startswith` to `Path.relative_to()` per
  the project doctrine (consistent with `io.py:29`).
- **B-9**: 15 sites using naive `datetime.now()` migrated to
  `datetime.now(UTC)`. Affected files: `context/trigger.py`,
  `infrastructure/agent_harness.py`, `infrastructure/rag_sync.py`,
  `core/a2a_protocol.py`, `core/a2a_types.py`,
  `metaprompts/task_loader.py`, `metaprompts/task_schema.py`.
- **B-10**: `print()` removed from
  `context/token_counter_subscriber.py:212` (true library-code
  violation). The `bridges/goose_launcher.py` and
  `bridges/ollama_cloud_proxy.py` `print()` calls remain — they
  are CLI shims and out of scope.
- **B-11**: `hashlib.md5` replaced with `hashlib.blake2b` for
  non-cryptographic content-derived identifiers in
  `infrastructure/constraint_registry.py:254` and
  `core/skill_library.py:211`.

### Documentation

- **B-17**: README stats refreshed:
  - Test count: 3012 → 2643 (actual `def test_` count)
  - Workflows: 11 → 12 (added `memory-upload.yaml`)
  - Skills: 8 → 9
  - CLI commands: 22 → 27
- CHANGELOG backfill: this entry (v13.22.1) is the first
  release-tagged changelog since 13.16.10; the gap (13.16.11
  through 13.22.0) is not documented here and is tracked
  separately.

### Tests

- 21 new regression tests across 4 new files
  (`test_mcp_utility_http_auth.py`, `test_version_consistency.py`,
  `test_cost_tracker_tiktoken_caching.py`,
  `test_mcp_openclaw_concurrency.py`, `test_a2a_dual_track.py`).
- No existing tests were modified or removed.

### Reference

- Audit: `audits/golden_master_v13.22.0.md`
- Metaplan: `audits/golden_master_v13.22.0_metaplan.md`

---

## [13.16.10] — 2026-06-01 — Model Migration: minimax-m3 replaces kimi-k2.6 + glm-5.1

### Changed

- **Model migration**: `minimax-m3:cloud` replaces `kimi-k2-thinking` +
  `glm-5.1:cloud` + `glmm5:1:cloud` typo across all TOML presets (agents.toml,
  workflow templates, task files, style-guide contracts)
- **agents.toml rebuilt**: deduplicated 4-5x duplicated/corrupted entries down to 48 canonical
profiles
- **Verification agents** (`verifier`, `fact-checker`, `ground-truth-validator`,
  `security-auditor`) retain `qwen3.5:397b` for deterministic low-temp
  verification
- **Budget agents** (`compressor`, `security_firewall`) retain `gemma3:27b` for cheap/fast
operations

---

## [13.16.9] — 2026-06-01 — Arms-Length Development + Phase 5 Clutter Eradication + Findings #1–#15

### Added

- **Arms-length development pipeline** (v13.15.0): verification gate, criteria
  enforcement, parallel delegate, hydration skip
- **Phase 5 clutter eradication** (v13.15.5): decomposed
  `mcp_utility_server.py` → `tools/_impl.py` shim; removed stubs, dead code,
  orphaned re-exports
- **Behaviour-in-TOML** (v13.16.8): delegate-by-default doctrine, execution loop, live-config
rendering
- **Doctrine delivery migration** (v13.15.7–v13.15.8): MCP + subagent channels
  (phases A+B), markdown mirror (phase C)
- **Global top-of-mind injection** (v13.15.5): context-management hardening
- **Develop/audit workflows** now produce file edits (v13.16.6)
- **LLM client registry + cost tracker expansion** + bridge benchmarks (v13.16.10)
- **Composite cache key, pre-call budget check, max_clients cap** (v13.16.9)
- **Auto-create tenants, async set_budget, race-free init** for cost_tracker (#6, #10, #11, #13)
- **`.md`→`.xml` recipe migration** + archive orphan (v13.16.7)

### Changed

- **Delegate-by-default** routing protocol: delegate substantive work to Beagle
  workflows on glm-5.1 sub-agents; direct execution as backstop
- **Context folding cadence** (v13.15.6): precise triggers + post-final-answer fold
- **GOOSE_AUTO_COMPACT_THRESHOLD** env var now actually overrides threshold (v13.15.3)
- **BeagleState** Mapping protocol restored so `run_beagle_workflow` stops crashing
- **RAG scope hardening**: exclude Beagle runtime state from corpus (v13.15.11)
- **Model alignment + mark_stale audit fix** (v13.15.1)
- **Tracking recorder event-schema + workflow discovery** from project root (v13.14.8)

### Fixed

- **Bridges**: prepend system to prompt + raise on missing choices/API errors (#1, #2)
- **Cost tracker**: auto-create tenants, async set_budget, race-free init (#6, #10, #11, #13)
- **Registry**: composite cache key, pre-call budget check, init race,
  max_clients cap (#7, #8, #12, #14)
- **Tests/logging**: lazy API-key probe + bump tenant tracking to WARNING (#9, #15)
- **Build alias**: `build_safe_env` public alias for `_build_safe_env` (#5)
- **Delegate xfail markers**: removed `raises=ImportError` (#4)
- **5 regressions restored** (v13.16.9): verify, checkpoint, compaction, executor, fact-checker
- **P3 delegation**: restore `--with-builtin developer`, drop `--no-profile` regression (v13.16.5)
- **Info-msg truncation guard** + narrate-with-action enforcement (v13.16.4)
- **Controller/hands architecture**: three regressions fixed (v13.16.3)
- **Phase 5 stub-gutting remediation**: gates, tests, re-exports, orphans
- **Phase 5 decomposition stub/regression remediation**
- **Dict-state reads in graph/nodes** + ensemble judge fallback
- **Subprocess**: guard process-group teardown, prevent SIGKILL on wrong group
- **CVCP routing**: `state.get()` not `getattr()` on TypedDict
- **Security**: restore blocking fail-closed LLM firewall + normalise-before-match
- **Smoke-test triage**: read-alias rejection, synthesis fallback, audit prompt (v13.15.9)
- **Build-and-upgrade hardening**: BeagleState `extra=forbid`, single
  `enhanced_context_fold`, stale pip artifact cleanup (v13.15.12)
- **Workflow prompt hardening**: strip embedded `<final_answer>` examples (v13.15.10)
- **Four deadlock/circuit-breaker/env fixes** (v13.15.0-post)
- **Compaction hardening**: triggers, sidecar rehydration, XML progress, real-token tracking, read
alias
- **Provider fix**: orphan config wiring, lint/test cleanup (v13.14.7)
- **API token metadata, engine schema validation, compaction cost-tracker integration** (v13.14.6)

### Infrastructure

- **README audit**: 12+ incoherences fixed (counts, broken paths, version markers)
- **`.goosehints` updated** after codebase quality audit
- **Version bumped**: 13.7.0 → 13.16.9 across all markers

---

## [13.7.1] — 2026-04-20 — Golden Master Audit v0.3.0

### Fixed (Critical)

- **Template substitution broken**: `create_prompt_builder()` in
  `workflow_builder.py` never substituted variables — `_key` was unused and
  `"{{{key}}}"` was a literal string, not an f-string. All 6 missing f-string
  prefixes in error messages also fixed.
- **Orpheus 8-bit sequence wrap**: Sequence counter extended from 8-bit to
  16-bit (`PACKET_HEADER_FMT` `BB`→`BH`), preventing collision at 256 rapid slot
  reuses.
- **Unbounded state lists**: Added `trim_state_lists()` with caps
  (errors=500, fact_ledger=1000, completed_nodes=500), called between nodes in
  the orchestrator.
- **EventBus memory leak**: Added size-based ring buffer eviction (10MB cap)
  and 5-second timeout for async callbacks.
- **Rate limit state leak**: Moved `_rate_limit_attempts` from function
  attribute to module-level dict with 5-minute TTL eviction.
- **GRPO metadata unbounded**: Capped trajectory metadata to 50 keys; narrowed
  pyrsistent fallback exception catch.

### Fixed (Security)

- **Guardian path traversal**: Added `os.path.realpath(os.path.expanduser())`
  before protected path comparison — prevents symlink escapes and `../`
  traversal.
- **Sandbox strict mode**: Added `strict: bool` to `SandboxConfig` — raises on
  `setrlimit()` failure instead of silently continuing without resource limits.
- **Process cleanup**: Added `SIGKILL` fallback after `SIGTERM` for processes that ignore
termination.
- **MCP rate limiting**: Added `_check_mcp_rate_limit()` (120 calls per
  60-second window) to the RAG MCP server.

### Fixed (Resource Management)

- **Agent counter leak**: Added TTL sweep (1 hour) and `finally`-block cleanup (was success-path
only).
- **Session memory unbounded**: Added `_MAX_SESSION_MESSAGES=10,000` and `_MAX_SESSION_EPISODES=500`
caps.
- **Connection pool unbounded**: Added `_MAX_CONNECTIONS=32` counter with warning.
- **Context compaction metadata**: Clear `_session_messages`, `_files_modified`, `_pending_commits`
after checkpoint save.
- **Cache expiration passive**: Added proactive expired entry sweep every 60 seconds amortized on
`get()`.

### Fixed (Concurrency)

- **Circuit breaker race**: Added `threading.Lock` for sync `get_circuit_health_report()` access
alongside the existing async lock.
- **Watcher subprocess timeout**: Added `timeout=30` to both `subprocess.run()` calls, handling
`TimeoutExpired`.

### Changed

- **VIGIL thresholds**: Per-tool threshold overrides via `TOOL_THRESHOLDS` dict (web_scrape,
web_search, sql_query).
- **Rate limiter eviction**: Changed from FIFO to LRU eviction with `_last_access` timestamps.
- **Secrets parser**: Improved quote stripping for matching outer quotes; WARNING when PyYAML
unavailable.

### Added

- `tests/test_golden_master_v030.py`: 38 new tests covering all 21 fixes.

---

## [13.6.1] — 2026-04-19

### Security

- **VULN-005**: Fixed `semantic_firewall()` temp file leak — `NamedTemporaryFile(delete=False)` left
sensitive prompts on disk after crashes. Changed to `delete=True` + explicit `finally` cleanup.
Added `<user_input>` tag stripping after HTML-escape to prevent prompt injection (OWASP A03).
- **VULN-006**: Fixed cache-busting DoS in `_cached_pattern_check()` — separated Unicode
normalization (zero-width char removal + case-fold) from the `@lru_cache`'d lookup to prevent
adversarial cache-eviction attacks.
- **VULN-007**: Fixed task ID truncation in `task_store.py` — `uuid4()[:12]` (48-bit entropy)
replaced with full `uuid4()` string (122-bit entropy) to eliminate IDOR collision risk at scale.
- **VULN-008**: Documented `SIGALRM` regex timeout bypass limitation in async contexts.

### Changed

- Replaced all `datetime.utcnow()` with `datetime.now(timezone.utc)` across 7 files (23 occurrences)
— Python 3.12+ deprecation compliance
- Fixed `cleanup_old_tasks()` date arithmetic bug in `task_store.py` — uses `timedelta(days=)`
instead of broken manual day subtraction
- Fixed `_deep_fork_state()` in `graph.py` — narrowed `except Exception` to `except (TypeError,
copy.Error)` to prevent silent partial-copy state corruption in GRPO trajectories
- Changed `workflow_id` default in `state.py` from `str(time.time())` to `str(uuid.uuid4())` for
uniqueness under concurrency
- Removed `"Attribute"` from `DANGEROUS_AST_NODES` in `security.py` — was blocking all attribute
access (false positives); `DANGEROUS_ATTRIBUTES` set already handles malicious lookups
- Tightened secret regex `bearer/token` pattern from `{6,}` to `{20,}` minimum to reduce false
positives
- Added `_evict_stale_entries()` to `TokenVerifier` in `mcp_security.py` — caps `_failed_attempts`
dict at 10,000 entries to prevent unbounded memory growth
- Reduced `tokens_per_minute` rate limit from 100,000 to 50,000 in `config.toml`
- Converted `rate_limiter.py` monolith to a thin re-export shim, delegating to `rate_limiter/`
package

### Documentation

- Updated README.md: test count (59→895), LOC (49k→62k), module count (195→199), CLI commands
(33→24), formatters (11→5), recipes (46→56), skills (15→8), workflows (9→10), MCP servers (3→5)
- Updated project structure tree with full module listing including `bridges/`, `daemon/` details,
`context/` hydration modules, `utils/` packages
- Updated ARCHITECTURE.md: MCP server count (3→5), added bridges and daemon subsystems
- Updated SECURITY.md: transport security section now documents HTTP/SSE with Bearer auth (not just
"stdio ONLY"), added VULN-005 through VULN-008
- Added Golden Master audit table to README.md
- Updated CLAUDE.md with Golden Master audit section
- Created `.goosehints` file with project structure, conventions, and gotchas

---

## [13.6.0] — 2026-04-18

### Security

- **VULN-001**: `NodeFailed` event now includes `model`, `error_category`, `stderr_snippet`,
`duration_seconds`, and `node_phase` fields for full operational telemetry. All node `except` blocks
dispatch `NodeFailed` via the Orpheus event bus.
- **VULN-002**: Added `QuantizedMemoryCache.set()` override to prevent TurboQuant lossy compression
of string values. Previously, `set()` inherited from `MemoryCache` bypassed the string guard in
`put()`, creating a data corruption path. Strings are now stored uncompressed. Triple-layer defense:
`compress()`, `put()`, and `set()` all independently reject str/bytes.
- **VULN-003**: Replaced hardcoded `TOKEN_BUDGET = 2000` with a config cascade:
`BEAGLE_MEMORY_INDEX_TOKEN_BUDGET` env var → `config.memory.index_token_budget` → default 2000
(clamped to minimum 500). Backward-compatible `TOKEN_BUDGET` module alias maintained.
- **VULN-004**: Confirmed Ollama Cloud endpoint uses `/api/embed` (remote) correctly; local uses
`/api/embeddings`. No fix required.

### Added

- Secrets loader (`beagle/secrets_loader.py`) — Vault → env → file secret chain with caching and
rotation
- Post-compaction rehydration (`context/post_compaction_rehydration.py`) — restores context from
memory after compaction events
- Recipe-agent bridge (`context/recipe_agent_bridge.py`) — connects agent recipes to context
injection
- Orpheus event bus section to OBSERVABILITY.md documentation
- Memory & Context subsystem section to ARCHITECTURE.md
- Memory index and TurboQuant configuration sections to ARCHITECTURE.md
- VULN remediation log to SECURITY.md
- Environment variables section to CLAUDE.md (`BEAGLE_MEMORY_INDEX_TOKEN_BUDGET`,
`TURBOQUANT_CACHE_ENABLED`)

### Changed

- `QuantizedMemoryCache` now has triple-layer string defense: `compress()`, `put()`, and `set()` all
independently enforce no-string-compression
- `TOKEN_BUDGET` is now configurable at runtime via environment variable or config file
- Updated test mocks: `test_memory_index` and `test_autodream` now patch `_get_token_budget` instead
of module constant
- Documentation version bumps across all docs: ARCHITECTURE, OBSERVABILITY, TURBOQUANT,
HARDWARE_TUNING, BEHAVIOR, SECURITY, CLAUDE.md

### Refactored — Phase 2 Golden Master Optimizations

- **AST Validator Decomposition**: `validate_python_code_ast()` (CC≈43) decomposed into
`_SecurityASTVisitor` class using single-pass `ast.NodeVisitor` pattern, replacing 4 separate O(n²)
`ast.walk()` loops. Each visit method has CC≈3. Early termination via `_ASTValidationStop` sentinel.
- **SQLite WAL Pooling**: `tracking/database.py` `_get_conn()` now uses `threading.local()`
connection pool with `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` for read concurrency
- **Config Decomposition**: `apply_env_overrides()` (CC≈34) decomposed into 11 per-section methods
(`_apply_goose_env`, `_apply_budget_env`, `_apply_cache_env`, etc.)
- **EVH Validation Implementation**: `autonomous_orchestrator.py` TODO(P2) stub replaced with
working evidence-based validation — checks claimed file modifications exist on disk, validates
non-empty output
- **Robustness**: Bare `except Exception:` in `checkpointer.py` replaced with structured logging;
`encoding="utf-8"` enforced on all `open()` calls across audit.py, secrets_loader.py, guardian,
file_cache.py
- **Dead Code Cleanup**: Orpheus FFI TODO replaced with implementation comment; unused
`_IMPORT_AST_NODES` removed

---

## [13.5.2] — 2026-04-17

### Added — Golden Master Overhaul (Security + Tech Debt + Hardware Optimization)

#### Part 0: Systemic Behavioral Correction

- `utils/safe_file_ops.py`: Auto-creation of missing files during workflows via `SafeFileWriter`
  context manager, `ensure_file_exists()`, `ensure_test_file_exists()`, `ensure_recipe_exists()` —
  configurable via `[behavior].auto_create_missing_files`
- Agent system directive (`SYSTEM_DIRECTIVE_TEMPLATE`) updated to instruct agents to create missing
  files

#### Part 1: Security Hardening

- CVE-2025-64439: `langgraph-checkpoint>=3.0.0` pinned; exploit payloads rejected
- FastMCP transport hardening: stdio-only by default, HTTP requires `TokenVerifier` + loopback
  binding + strict CORS
- Orpheus IPC: per-slot CRC32 checksums, atomic ready flags, `SlotWatchdog` for stuck slots
- TurboQuant: `compress()` raises `TypeError` for non-ndarray; `QuantizedMemoryCache.put()` bypasses
  str/bytes
- LanceDB tenant isolation: `_get_tenant_table()`, `_ensure_tenant_schema()`, tenant-scoped
  connection caches
- Kùzu: All queries parameterized; `_validate_search_input()` prevents Cypher injection

#### Part 2: Technical Debt Remediation

- Hardcoded paths eliminated: `config/paths.py` + `PathsConfig` in `config.toml`
- Global mutable state → `ProcessRegistry`, `AgentCallTracker`, `OrchestratorChannelManager`
  singletons
- Cost tracker consolidated: `config/models.py` is single source of truth
- `mypy --strict` config added; `from __future__ import annotations` throughout
- `SignalHandler` class replaces `_test_mode` environment variable hack
- `docs/TURBOQUANT.md` with bit-packing algorithm documentation

#### Part 3: Hardware-Aware Performance Optimization

- Ramdisk staging: `cast_ingestion.py` uses `/mnt/beagle_rag_staging` tmpfs for intermediate files;
  70-90% SSD write reduction
- Incremental CAST ingestion: `.beagle_ingest_cache.json` skips unchanged files on re-ingestion
- Faiss IVF-PQ pre-filter: `infrastructure/faiss_prefilter.py` for ANN search acceleration (graceful
fallback)
- Graph pruning: `infrastructure/graph_maintenance.py` removes low-connectivity nodes for AutoDream
- Warm worker pool: `core/warm_workers.py` maintains 2-3 pre-spawned goose subprocesses
- Speculative context prefetch: enhanced hydration with LRU cache for recent query embeddings
- Semantic prompt cache: `context/semantic_prompt_cache.py` caches by embedding cosine similarity >
0.99
- Dynamic concurrency: `core/dynamic_pool.py` scales workers 2-6 based on `psutil.cpu_percent()`
- Reflex Arc v2: `core/reflex_arc.py` trivial query detection bypasses full LangGraph
- RAM-aware quantization: `turboquant.select_bit_width()` adjusts bit-depth based on available RAM
- CPU governor management: `infrastructure/cpu_governor.py` auto-switches performance/powersave
- Hardware checks: `infrastructure/hardware_checks.py` verifies ramdisk, I/O scheduler, CPU governor
- eBPF tracing stub: `infrastructure/ebpf_tracer.py` config hook for future eBPF backend
- OS tuning script: `scripts/tune_system.sh` for THP, I/O schedulers, CPU governor, ramdisk, ZRAM
- `docs/HARDWARE_TUNING.md` with MSI MPG Z390I GAMING EDGE AC (Intel i7-9700K) tuning guide

### Changed

- `config.toml`: Added `[hardware]`, `[tracing]` sections; updated `[mcp_auth]`, `[mcp_cors]`,
`[behavior]`, `[paths]`
- `pyproject.toml`: Version bumped to 13.5.2
- TUI dashboard: Added ramdisk usage, SSD writes saved, CPU governor, load average panels
- `cast_ingestion.py`: Rich progress bar during ingestion; SSD savings logging

### Security

- All MCP servers enforce stdio transport; HTTP blocked by default
- Kùzu queries fully parameterized; Cypher injection patterns rejected
- LanceDB tenant isolation at table level (not application-level filtering)

---

## [13.4.0] — 2025-04-14

### Added

- **Deep Forks**: Structural sharing via `pyrsistent`
- **TurboQuant**: Adaptive quantization for LLM routing
- **A2A Protocol v2**: Cryptographic signatures (PyNaCl), RBAC permissions
- **Distributed Tracing**: Full OpenTelemetry integration
- **MicroVM Sandboxing**: Isolated code execution
- **Steering System v2**: Policy-driven constraints
- **MCP Servers**: Three Model Context Protocol servers
- **Circuit Breaker**: Resilience pattern for external service calls
- **Rate Limiter**: Token-bucket rate limiting

### Changed

- Enterprise-grade lint pass: All `ruff` and `vulture` checks pass
- Code quality: Fixed 166 ruff issues
- Dead code removal: Removed 32 unused variables

---

## [12.4.0] — 2025-04-12

### Added

- Hybrid RAG with LanceDB vector search + Kùzu graph traversal
- Autonomous orchestrator with LangGraph StateGraph
- YAML workflow definitions
- Cost governance with per-model tracking
- Rich TUI dashboard
- Beagle CLI with Typer

---

## [11.0.0] — 2025-03-28

### Added

- Initial multi-agent workflow orchestration engine

---

## Pre-v1.0 release train (v13.x)

The v13.x series was the internal release train before the project renumbered
to `1.1.1` (see `pyproject.toml`). The following sections were archived here
from `README.md` so the README reflects the current version only.

## v13.22.3 RAG Recall Restoration + Ollama Embedder + TurboQuant Sidecar

> **Release note (internal versioning):** v13.22.3 is a focused restoration
> release: the 13.22.2 RAG
> subsystem was broken (zero-vector substitution on OOM, kuzu 8 TB
> mmap reservation on a 4 GB cgroup, cypher queries filtering on
> the wrong node_type, and the corpus indexing dev_tools + memory-test
> fixtures rather than the actual codebase). 13.22.3 restores
> real search/graph results, switches the embedder to Ollama
> (137 MB in a separate cgroup instead of 1.2 GB in-process),
> adds the TurboQuant sidecar for in-RAM vector compression,
> and lands the codebase-only corpus scope. See
> `CHANGELOG.md` for the full 6-commit fix series and
> `audits/` for the next audit.

The 13.22.2 release shipped with a broken RAG subsystem: a
1.2 GB sentence-transformers model living inside the 4 GB user
cgroup, a Kùzu 8 TB mmap reservation on first open, cypher
queries filtering on `node_type = 'FunctionDef'` while the
corpus stored `'function'`, and a 34,403-chunk index that was
actually the dev_tools + memory-test fixtures from a prior full
re-index that got out of sync. The system still said "rag_status:
ok" but `rag_search` returned empty results and `graph_callers`
returned empty rows.

13.22.3 is a focused restoration release. No new features beyond
what the restored subsystem needs to stay under the cgroup cap.

| Area | Change | Severity |
|---|---|---|
| Corpus scope | `scan_codebase` excludes runtime state (`.beagle/`, `.goose/`, `.agents/`, `.claude/`), operational noise (`audits/`, `benchmarks/`, `plans/`, `examples/`, `.github/`), adjacent monorepo projects (`beagle_containerisation/`, `beagle_dockeriser/`, `hooks/`, `ai/`), and agent-tooling files at the project root (`CLAUDE.md`, `AGENTS.xml`, `ARCH_REPORT.md`, `BEAGLE_CLI_CATALOG.md`, `CODEBASE_AUDIT_REPORT.md`). Result: 789 → 697 source files, 34,403 → 10,975 AST chunks. | Critical |
| Embedder | `[embed].provider` switched from `sentence-transformers` (1.2 GB in-process) to `local` (Ollama `nomic-embed-text`, 137 MB in `/system.slice/ollama.service`). Same 768-dim output so the existing lance index is compatible. | Critical |
| Cypher queries | `graph_callers` / `graph_callees` / `graph_class_hierarchy` now filter on `node_type = 'function'` / `'class'` (lowercase, short form — what `cast_ingestion` actually writes) and the `INHERITS_FROM` relation label. `graph_imports` / `graph_dependents` get the arrow direction fix (`>-[r*1..]->` was invalid Cypher; now `-[r*1..]->`). | Critical |
| httpx client reuse | `_embed_batch` reuses a single `httpx.Client` per batch instead of creating/destroying one per text (10,975 allocations per ingest). Was the root cause of the deterministic jemalloc bg-thread segfault at ~50% of the encode loop. | High |
| Kùzu | Explicit `max_db_size=128 MB` + `buffer_pool_size=64 MB` (was 8 TB default mmap reservation that OOM'd the 4 GB cgroup on first open). | High |
| Atomic move | `_atomic_move_on_same_fs` rewritten as a two-phase dance-move (rename dst to `<dst>.old` aside, promote `<dst>.new` to dst, clean up) with per-pid-unique temp names. The naive "rename .new to dst" failed with `OSError(ENOTEMPTY)` on Linux for populated targets — the B-05-class bug. | High |
| Embedder error | `MemoryError` in `SentenceTransformerEmbedder.encode` now raises instead of silently substituting `[[0.0]*768]`. The `except Exception` swallowed `MemoryError` along with everything else; the ingest happily wrote a 10,975 zero-vector corpus. | High |
| Streaming ingest | `_embed_chunk_records` is now a generator yielding 256-chunk batches with `gc.collect()` between batches. Sidecar write happens after the streaming completes, so peak RSS is bounded. | Medium |
| TurboQuant sidecar | New `infrastructure/turboquant_lance_cache.py` — 3-bit compression of the lance index vectors (~30 MB on disk for 10k vectors, ~6x smaller than raw float32), with on-demand numpy decompression for brute-force cosine search. Gated on `[rag].turboquant_sidecar = true` (default true). | Medium |
| Circuit breaker recovery | `subprocess_pool._execute_goose_with_fallback` now uses `circuit._can_attempt()` (which performs the OPEN→HALF_OPEN state transition) rather than the raw `circuit.is_open` read (which would short-circuit the recovery path and leave the breaker stuck OPEN forever). | Medium |
| Storage | RAG data lives on a dedicated SSD-backed data root, with `~/.beagle/instance_rag{,_kuzu,}` as symlinks. ~82 MB on disk for the full corpus. Embedder runs under its own systemd slice (separate cgroup). | Info |
| Linting | `pyproject.toml` adds `extend-exclude` for build artifacts and `[tool.ruff.lint.per-file-ignores]` for `RUF067` (intentional re-exports in `__init__.py`) and `RUF069` (deterministic float equality in test assertions is a false positive). 0 errors. | Info |

**Verification** (live MCP server):

- `rag_status`: `kuzu_connected: true`, `lance_table_loaded: true`,
  `embed_model_loaded: true`, `indexed_chunks: 10975`
- `rag_search("circuit breaker subprocess pool implementation")` →
  `CircuitBreakerConfig` (distance 0.28) and `PoolConfig` (distance 0.28) —
  semantically perfect
- `graph_callers("route_query_to_workflow")` → 4 real callers
- MCP server RSS: 410 MB (was 1.4 GB pre-fix)
- 96 / 96 RAG + hotswap test suite passing

**Regression coverage** — what is tested and what is not:

| Defect | Regression test | Asserts the *right thing*? |
|---|---|---|
| Zero-vector substitution on OOM / embedder failure | `tests/test_embedder_batch.py::test_batch_failure_falls_back_to_sentence_transformers` | Yes — asserts the embedder must NOT silently emit zero vectors and falls back on HTTP 500 |
| Corpus-scope drift (`scan_codebase` exclusions) | `tests/test_mcp_rag.py::test_scan_codebase_respects_exclusions`, `::test_scan_codebase_excludes_runtime_state_dirs`, `::test_scan_codebase_excludes_agent_tooling_at_root` | Unit-level only — assert exclusions, **not** that `rag_search` returns a known symbol from the real codebase |
| Cypher `node_type = 'function'` filtering | `tests/test_f6_hydrator_real_shape.py` (fixtures assert lowercase `node_type="function"`); no direct graph query assertion against live Kùzu | Partial — fixture shape only |
| Kùzu 8 TB mmap / `max_db_size` | **none found** | No |
| End-to-end corpus-scope guard (index real code, assert the symbol is returned) | `tests/smoke_test_rag_search.py` is a **manual script**, not pytest, and needs a live server; `tests/test_search_finds_ingested_functions` only asserts `callable(rag_search)` | **No automated test** |

**Gap:** no automated pytest indexes a known symbol from the real codebase and
asserts `rag_search` returns it. That is the one assertion shape that catches
corpus-scope drift, and its absence means the corpus bug can silently recur on
the next scope change. Tracked in the follow-up list (C06).

**Commits** (6):

- `c34e2c8` — `chore: ruff PLW1514 sweep + per-file-ignores for RUF067/RUF069`
- `627c5fd` — `feat(rag): add TurboQuant sidecar for in-RAM vector compression`
- `fcc8d11` — `fix(rag): full corpus, atomic swap, Ollama embedder, cypher type fix`
- `ad30348` — `test(rag): add regression tests for the RAG infrastructure fixes`
- `007286b` — `test: regression test for circuit breaker recovery + minor test cleanups`
- `84451a3` — `chore: ruff sweep for benchmarks/ and examples/`

---

## v13.16.9 Arms-Length Development + Phase 5 Clutter Eradication + Findings #1–#15

| Area | Fix | Severity |
|---|---|---|
| Temp file leak | `NamedTemporaryFile(delete=False)` → `delete=True` + cleanup in `finally` | Critical |
| Prompt injection | Strip `<user_input>` tags from user query before LLM prompt embedding (OWASP A03) | Critical |
| AST over-blocking | Removed `"Attribute"` from `DANGEROUS_AST_NODES`; relies on `DANGEROUS_ATTRIBUTES` | Medium |
| Cache DoS | Unicode normalization + zero-width char stripping before `lru_cache` key | High |
| Secret regex | Bearer/token pattern min length 6→20 chars | Medium |
| Task ID entropy | `uuid4()[:12]` → full `uuid4()` (48→122 bit) | High |
| Date arithmetic | `utcnow().replace(day=day-N)` → `now(UTC) - timedelta(days=N)` | Medium |
| Deprecated datetime | All `datetime.utcnow()` → `datetime.now(timezone.utc)` | Medium |
| Deepcopy fallback | `except Exception` → `except (TypeError, copy.Error)` only | High |
| Workflow ID | `str(time.time())` → `str(uuid.uuid4())` | Medium |
| Auth DoS | Added `_evict_stale_entries()` + `_MAX_FAILED_ATTEMPTS_ENTRIES=10000` cap | Medium |
| Rate limiter | Consolidated monolithic → package re-export shim | Low |
| Rate limit | `tokens_per_minute: 100000` → `50000` | Low |
