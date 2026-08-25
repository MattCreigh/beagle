# Release Readiness Code Audit — Beagle 1.2.0 (feat/beacon-coordination)

Date: 2026-08-22 · Mode: READ-ONLY (no source changed) · Auditor: goose/ox-alpha
Key question: software errors, bad structures, differences vs Enterprise Linux rules;
release readiness; error list + correction procedure with test/approval/real-world gates.

## PART 1 — Planning & Evidence Log

Plan: static gates (ruff/mypy/bandit/vulture/format) → doctrine-forbidden-pattern sweep
→ read new uncommitted beacon module (highest risk: unreviewed durability code) →
targeted tests via tup → severity-ranked findings → correction procedure.

Examined: `src/beagle/beacon/` modules journal, backend, `backends/__init__`,
  connector, contact, archive, keys, records, intents (partial reads); plus
  `src/beagle/config/loader.py`, `src/beagle/cli/commands/coord.py`,
  `src/beagle/beacon/spawn.py`, `pyproject.toml [tool.mypy]`; repo-wide rg sweeps.
rg sweeps. Queries run: `mypy src/beagle`; `ruff check . --statistics`; `ruff format
--check .`; `bandit -q -r src/beagle -f json`; `vulture --min-confidence 90`;
`rg "datetime.utcnow|shell=True|startswith(str(|except Exception|time.time()"`.

Initial answer to key question: **conditional go** — automated gates near-clean; defects
cluster in the NEW journal durability path and doc-formatting scope.

## PART 2 — Final Audit Report

### Executive Summary

Beagle 1.2.0 is close to releasable but **not ready today**. Static hygiene is strong:
`ruff check` exits 0, bandit reports 0 Medium/High findings, forbidden patterns
(`utcnow`, `shell=True`, `startswith` containment) are absent from `.py` code, and the
new Beacon coordination suites pass 107/107. The blocking defects sit in the uncommitted
write-behind journal — the module the design itself calls "the ONLY thing standing between
a crash and losing a session's work": an unsound Optional file-handle contract (6 mypy
errors), a silently-dying fsync thread, an unbounded-memory replay path, and a
schema-drift crash in replay. All four are small, localised fixes. Verdict:
**CONDITIONAL GO** after the P1/P2 corrections below and a re-run of the gate battery.

### 1.1 Component Assessment

| Component | Score /10 | Evidence |
|---|---|---|
| beacon/backend.py (contract) | 9 | Frozen Protocols, fail-loud registry, vulture hits are Protocol params |
| beacon/journal.py (durability) | 5 | 6 mypy errors; silent thread death; memory-bounded replay missing |
| beacon connector/contact/server | 8 | Consistent 0o600/0o700 perms; Popen without shell, start_new_session=True |
| config loader ([coord].backend) | 9 | Lazy import avoids cycle; unknown backend = loud error (C-B2 honoured) |
| Repo-wide type safety | 7 | 23 mypy errors / 377 files; Any-leak cluster listed in §Errors |
| Security surface | 8 | bandit LOW-only; fail-closed validators; secret-key journalling rejected |
| Tests (beacon) | 9 | 107/107 via tup; two-process rendezvous covered |

### 1.2 Security Highlights

- Secret-name pattern rejects journalling of credential-shaped keys (C-03) — good.
- All beacon artefacts chmod 0o600 inside 0o700 dirs; matches Enterprise Linux private-tmp norms.
- No shell=True anywhere; spawn detaches via start_new_session=True (systemd-style).
- Residual: `session_memory.py:265` uses sha256[:12] (48-bit) as an ID — below the full-UUID doctrine.

### Software Error List (severity = likelihood × impact)

| # | Location | Defect | Sev | L×I |
|---|---|---|---|---|
| E-1 | beacon/journal.py:126,137,143,177,186,187 | `_fh` initialised None, never annotated `TextIO \| None`; flush/rotate/close deref unguarded → AttributeError/use-after-close if flush runs post-stop | High (P1) | Likely × Major |
| E-2 | beacon/journal.py:191-192 `_fsync_loop` | fsync OSError kills daemon thread silently; writes continue believing durable — violates "no benign failures" | High (P1) | Possible × Major |
| E-3 | beacon/journal.py:246-252 `replay()` | `path.read_text().splitlines()` loads each rotation whole; worst case 30×1 GiB → OOM at startup on 16 GB host | Med-High (P2) | Possible × Major |
| E-4 | beacon/journal.py:268-284 `_replay_one` | `record["op"]/["args"]` KeyError on schema-drifted (valid-JSON) line aborts entire startup replay | Medium (P2) | Possible × Moderate |
| E-5 | utils/session_memory.py:265 | Truncated-hash id (48-bit) vs full-uuid4 doctrine | Low (P3) | Possible × Minor |
| E-6 | 17 sites (jwt.py:19,48; graph.py:465-548; graph_builder.py:323-507; skill_library.py:101; turboquant.py:112; orpheus_http_transport.py:207; services/embedding.py:356; preflight/display.py:96) | `no-any-return` — Any crossing declared-typed boundaries | Low (P3) | Possible × Minor |
| E-7 | web_search.py:14-17 | `_DDGS` no-redef; fallback-import typing wrong | Low (P3) | Certain × Insignificant |
| E-8 | ruff format scope | Formatter rewrites 24 `.md` files (23 in archive/, historical records) — gate never green; history-mutation hazard | Low (P3) | Certain × Insignificant |
| E-9 | tests (store/contact suites) | fork()-in-multithreaded-process DeprecationWarning — deadlock-class hazard in test harness (prod spawn.py unaffected) | Info (P4) | Rare × Moderate |

Non-defects verified clean: bandit 52×LOW (B603/B404 informational); vulture 3 hits =
Protocol params; 267 of 278 `except Exception` mentions carry documented "intentional"
annotations (tracked debt via scripts/check_exception_debt.py).

### Correction Procedure (with test, approval, real-world gates)

Order is dependency-safe; every step ends in a hard gate before the next begins.

**Step 1 — journal.py handle lifecycle (E-1).**
Annotate `self._fh: TextIO | None`; guard `flush()`/`_rotate_if_needed()`/`close()` for
None/closed; set `_fh = None` after close; make `stop()` idempotent.
Test gate T1: `mypy src/beagle` reports 0 journal errors; `tup run_tests
tests/test_beacon_journal.py` green; NEW regression tests added first: (a) flush-after-stop
raises nothing, (b) concurrent stop‖flush race looped ≥100 iterations.
Approval gate A1: adversarial review (CVCP second-agent) of diff confirms guards add no
behaviour change on happy path.

**Step 2 — fsync-thread failure surfacing (E-2).**
Wrap loop body: catch `(OSError, ValueError)`; `logger.exception`; set
`self.last_fsync_error` timestamp; expose in coord status; do NOT swallow CancelledError.
Test gate T2: monkeypatched os.fsync raising OSError → warning logged, flag set, subsequent
flush attempts retried; existing 25 journal tests still green.
Approval gate A2: confirm error surfaces on operator-visible path (`beagle coord status`),
not log-only.

**Step 3 — streaming, schema-safe replay (E-3, E-4).**
Replace read_text with `with path.open()` line iteration; validate record shape
(op/key/args present, op ∈ `_REPLAYABLE_OPS`) → warn-and-skip on drift, mirroring the
malformed-JSON path.
Test gate T3: corpus fixtures — valid, truncated-last-line, schema-drifted, unknown-op;
assert replay continues and returns counts; 10 k-line replay < 1 s; peak RSS < 100 MB
(tracemalloc assertion).
Real-world gate R1 (crash drill): start live beacon, apply ≥200 board mutations, `kill -9`
the server mid-write-storm, restart, verify board restored exactly (issue/comment/transition
counts equal pre-kill snapshot) via two-process client.
Real-world gate R2 (rotation drill): drive > max_bytes with max_files=3, restart, verify
oldest-first replay order and pruning to 3 files.

**Step 4 — release hygiene batch (E-5..E-9).**
E-5: replace id with uuid.uuid4(). E-6/E-7: cast/annotate the 17 Any-boundary returns +
fix `_DDGS` redef typing. E-8: propose excluding `archive/**` from ruff markdown-code-block
formatting — CONFIG-CHANGE REQUIRING USER APPROVAL (alters gate semantics; the symmetric
rule prefers fixing inputs, but archives are immutable history — decision escalated, not
unilaterally disabled). E-9: set multiprocessing start-method `forkserver` in conftest.
Test gate T4: full gate battery re-run — `ruff check .` exit 0; `ruff format --check`
green outside approved exclusions; `mypy src/beagle` ≤ agreed baseline (0 new);
bandit 0 Medium/High; `vulture` whitelisted.

**Step 5 — ship gate.**
Commit the 18 pending files (conventional messages); `uv build`; reinstall wheel
`uv pip install --force-reinstall --no-deps <wheel> --python /opt/beagle/beagle_venv/bin/python3`;
update plans/ prefixes with cited command output; CHANGELOG entry same session.
Approval gate A3: release checklist owner signs off citing T1-T4, R1-R2 outputs.

Rollback: single-commit revert per step; wheel reinstall restores prior build.

### 5. Actionable Roadmap (Quick Wins)

1. **[KEY QUESTION] Fix journal E-1/E-2/E-3/E-4** — S effort each, removes the only
   data-loss-class risks; blocks release. Risk if unfixed: High.
2. Re-scope ruff markdown formatting off archive/** (after approval) — S, makes the
   format gate green forever. Risk: Low.
3. Entropy fix E-5 + Any-boundary batch E-6/E-7 — S/M, restores type-doctrine floor.
   Risk: Low.
4. Forkserver in test harness E-9 — S, removes flake/deadlock class. Risk: Low.

### Closing Remarks

The codebase evidences unusually disciplined engineering: fail-loud registries, frozen
protocols, annotated exception debt, permission hygiene, and a 107/107 green new-feature
suite. The key question's answer is precise: **the software is conditionally ready** —
four bounded defects in the newest, most load-bearing module stand between it and release,
and the procedure above closes them behind executable gates rather than assertions.

### Template Self-Audit

Worked: key_question kept the audit code-focused; quick-start sections kept length sane.
Unclear: "Enterprise Linux rules" needed mapping onto this repo's own enforced baseline;
future templates should name the standard explicitly. Improvement: provide expected
artifact paths so evidence lands in a canonical audit/ location.

</final_answer>
