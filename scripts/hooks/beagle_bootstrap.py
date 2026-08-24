#!/usr/bin/env python3
"""Beagle bootstrap hook — fires on the goose SessionStart event and on pre-commit.

Wires together the four systems the user asked to be plumbed, plus a
forced Top-of-Mind render on session start:
  0. ``GooseTopOfMindRenderer().render_canonical(force=True)`` — force-renders
     the doctrine artefact so it is fresh in context when the session begins.
     ``force=True`` (not the cache policy) because the previous artefact may
     be unhydrated or stale.
  1. ``RAGStalenessTracker`` — marks the RAG cache as fresh or stale.
  2. ``auto_hydrate`` — reingests the codebase if the RAG is stale
     (firing the staging→swap pipeline through ``hotswap_ingest``).
     Since 2026-08-22 this step runs in a DETACHED BACKGROUND process:
     the staleness assessment embeds through the local Ollama endpoint,
     and a wedged embed runner hung pre-commit for 10+ minutes, three
     commits in a row. The hook now returns immediately (summary field
     ``hydration.status = deferred_to_background``) and the child
     publishes a completion notification — status running/completed/
     failed with timestamps — to ~/.beagle/hydration_job.json under a
     hard wall-clock cap (BEAGLE_HYDRATION_BG_TIMEOUT_S, default 2400 s;
     a full ingest measured ~17 min). Set BEAGLE_BOOTSTRAP_SYNC_HYDRATION=1
     to force the old synchronous behaviour when debugging.
  3. ``recipe_agent_bridge.on_beagle_init`` — syncs goose's recipe
     registry with Beagle's agent configs so the orchestrator can
     dispatch tasks to Beagle agents.
  4. ``on_post_compaction`` — registers a checkpoint-and-rehydrate
     hook so the next compaction event doesn't drop session state.

This hook is a thin orchestration layer. All real work lives in
the modules above. The hook is the entry point the goose SessionStart
event and the pre-commit hook call; without it, none of the five systems
ever run.

Idempotent: running it twice in a session is a no-op for the side
effects (mark_fresh, RAG reingest) but produces the same summary dict
so it is safe to invoke from any entry point (precommit, session
start, MCP tool).

Exit code:
  0 — normally, whatever the individual steps did. A session-start
       hook must not block the surrounding toolchain: a broken RAG
       cache is not a reason to refuse a commit.
  1 — only when ``BEAGLE_HOOK_STRICT=1`` is set and at least one step
       failed. Opt-in, for CI or for a deliberate health check.

  The previous version documented both "1 — import error" and "exits 0"
  for the same condition, which is self-contradictory. It exits 0.

Stdout:
  Always a single-line JSON object carrying a truthful ``status``:

  * ``ok``       — every step succeeded.
  * ``degraded`` — at least one step failed, at least one succeeded.
  * ``error``    — every step failed.

<invariant>
``status`` is DERIVED from the step results, never assigned a constant.
An earlier version set ``status = "ok"`` unconditionally on the line
before emitting, so a run where all three steps died with
ModuleNotFoundError still reported success. A hook that cannot fail is
not a check — it is decoration. When status is not "ok" the failing
step names are also written to stderr, because that is what pre-commit
shows the user.
</invariant>

Pre-commit integration:
  Registered in ``.pre-commit-config.yaml`` as ``id: beagle-bootstrap``,
  ``language: system`` — it runs in the user's own interpreter, not the
  pre-commit sandbox. The entry names ``.venv/bin/python`` explicitly.
  A bare ``python3`` there resolves to ``/usr/bin/python3``, which has
  none of the project's dependencies, so every step failed with
  ModuleNotFoundError while the hook reported "ok" and pre-commit
  reported "Passed".
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make the project importable regardless of CWD. The hook is invoked
# from a variety of contexts (MCP server, pre-commit, shell) and
# sys.path[0] may be anything; an explicit insert is the only safe
# approach.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # scripts/hooks -> scripts -> root
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Background hydration (2026-08-22) ──────────────────────────────────────
# Step 2's staleness assessment embeds through the local Ollama endpoint.
# When that runner wedges, the embed call — which carries no timeout of its
# own inside the RAG stack — hung pre-commit for 10+ minutes, three commits
# in a row. Hydration therefore runs in a DETACHED child process: the hook
# returns immediately and the child reports completion to
# ~/.beagle/hydration_job.json under a hard wall-clock timeout.
_HYDRATION_CHILD_ENV = "BEAGLE_BOOTSTRAP_HYDRATION_CHILD"
_HYDRATION_SYNC_ENV = "BEAGLE_BOOTSTRAP_SYNC_HYDRATION"
_HYDRATION_TIMEOUT_ENV = "BEAGLE_HYDRATION_BG_TIMEOUT_S"
# A full hot-swap ingest measured ~17 min; the cap must exceed it with headroom.
_HYDRATION_DEFAULT_TIMEOUT_S = 2400
_HYDRATION_JOB_FILE = Path.home() / ".beagle" / "hydration_job.json"
_HYDRATION_BG_LOG = Path.home() / ".beagle" / "hydration_bg.log"
# The step-3 probe is warm-up work, not a gate: bound it hard so the hook
# can never hang on it, and report a cap hit as a failed (non-fatal) step.
_PROBE_TIMEOUT_S = 90
# Global wall-clock budget for the WHOLE parent hook. Per-step caps cannot
# chase every internal path into the RAG/embed stack (the 2026-08-22
# incidents wedged in step-0/1 import graphs on an idle Ollama socket), so
# the entire run is bounded: when the alarm fires, a degraded summary is
# emitted and the hook exits 0 — it must never hold a commit hostage.
_HOOK_BUDGET_ENV = "BEAGLE_BOOTSTRAP_BUDGET_S"
_WATCHDOG_ENV = "BEAGLE_BOOTSTRAP_WATCHDOG"  # set on the supervised inner run
_HOOK_DEFAULT_BUDGET_S = 180


class _BudgetExceeded(TimeoutError):
    """Raised by the SIGALRM handler when the hook outlives its budget."""


def _on_budget_alarm(_signum: int, _frame: Any) -> None:
    """SIGALRM handler — converts the tick into an exception.

    Args:
        _signum: The signal number (SIGALRM); positional-only, unused.
        _frame: The current stack frame; positional-only, unused.

    Raises:
        _BudgetExceeded: always, interrupting the blocked main thread.
    """
    raise _BudgetExceeded(
        f"bootstrap exceeded wall-clock budget "
        f"({os.environ.get(_HOOK_BUDGET_ENV, _HOOK_DEFAULT_BUDGET_S)}s)"
    )


def _write_hydration_job(payload: dict[str, Any]) -> None:
    """Atomically publish background-hydration job state.

    This file IS the completion notification: a watcher (operator, watchdog,
    next session's bootstrap) reads it to learn running/completed/failed.

    Args:
        payload: The job record to persist. Written via tmp + os.replace so
            a reader never sees a partial file.
    """
    try:
        _HYDRATION_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _HYDRATION_JOB_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, _HYDRATION_JOB_FILE)
    except OSError as exc:
        print(f"beagle hook: failed to write hydration job state: {exc}", file=sys.stderr)


def _run_child_hydration() -> int:
    """Run ONLY the step-2 hydration synchronously; notify on completion.

    Invoked when ``BEAGLE_BOOTSTRAP_HYDRATION_CHILD=1`` — i.e. by the
    detached child spawned from :func:`main`, or manually by an operator.

    <invariant>
    The child never spawns another child and never touches git state. It
    exits 0 even on failure: a failed background ingest is REPORTED (job
    file + log), not raised into a hook that has already returned.
    </invariant>

    Returns:
        0 always — failures are recorded in the notification, not propagated.
    """
    project_dir = os.environ.get("BEAGLE_PROJECT_ROOT") or str(_PROJECT_ROOT)
    timeout_s = int(os.environ.get(_HYDRATION_TIMEOUT_ENV, str(_HYDRATION_DEFAULT_TIMEOUT_S)))
    job_id = str(uuid.uuid4())
    started = datetime.now(UTC).isoformat()
    _write_hydration_job(
        {
            "job_id": job_id,
            "status": "running",
            "started_at": started,
            "timeout_s": timeout_s,
            "project_dir": project_dir,
        }
    )

    result: dict[str, Any] = {}
    error: BaseException | None = None

    def _work() -> None:
        nonlocal result, error
        try:
            from beagle.context.hydration_hook import on_session_start

            result = dict(on_session_start(project_dir=project_dir, force=False))
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            error = exc

    worker = threading.Thread(target=_work, daemon=True, name="beagle-hydration-bg")
    worker.start()
    worker.join(timeout=timeout_s)

    finished = datetime.now(UTC).isoformat()
    if worker.is_alive():
        # The daemon thread dies with this process; the embed call it was
        # blocked on carries no timeout of its own, so the wall-clock cap is
        # the only bound that exists.
        _write_hydration_job(
            {
                "job_id": job_id,
                "status": "failed",
                "error": f"TimeoutError: hydration exceeded {timeout_s}s",
                "started_at": started,
                "finished_at": finished,
            }
        )
    elif error is not None:
        _write_hydration_job(
            {
                "job_id": job_id,
                "status": "failed",
                "error": repr(error),
                "started_at": started,
                "finished_at": finished,
            }
        )
    else:
        _write_hydration_job(
            {
                "job_id": job_id,
                "status": "completed",
                "result": result,
                "started_at": started,
                "finished_at": finished,
            }
        )
    print(
        json.dumps(
            {"job_id": job_id, "status": "background_hydration_finished"},
            default=str,
            sort_keys=True,
        )
    )
    return 0


def _load_bootstrap():
    """Load the sibling interpreter bootstrap by path.

    The hook may be running under an interpreter that cannot import beagle,
    so the bootstrap cannot live in the package. It sits beside this file.

    Returns:
        The loaded module, or None when it is absent.
    """
    path = Path(__file__).resolve().parent / "_hook_bootstrap.py"
    spec = importlib.util.spec_from_file_location("_hook_bootstrap", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_receipt(trigger: str, results: dict[str, bool]) -> None:
    """Write a receipt recording that the bootstrap ran.

    The receipt lives at ~/.beagle/bootstrap_run.json and holds a
    timezone-aware UTC timestamp, the trigger, and one boolean per system.
    It is written through a temporary file and os.replace so a reader never
    sees a partial file. A failed receipt must not fail the session.

    Args:
        trigger: "session_start" or "pre_commit".
        results: One boolean per system: rag_staleness, auto_hydrate,
            registry_sync, rehydration_checkpoint.
    """
    receipt = {
        "timestamp": datetime.now(UTC).isoformat(),
        "trigger": trigger,
        "render": results.get("render", False),
        "rag_staleness": results.get("rag_staleness", False),
        "auto_hydrate": results.get("auto_hydrate", False),
        "hydration_deferred": results.get("hydration_deferred", False),
        "registry_sync": results.get("registry_sync", False),
        "rehydration_checkpoint": results.get("rehydration_checkpoint", False),
    }
    target = Path.home() / ".beagle" / "bootstrap_run.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        print(f"beagle hook: failed to write receipt: {exc}", file=sys.stderr)


def _emit(payload: dict[str, Any]) -> None:
    """Print a single-line JSON object to stdout and return.

    A single line keeps the output parseable by every consumer
    (MCP utility server, pre-commit capture, log scraper). Multi-line
    JSON breaks shell substitution.

    Args:
        payload: The summary to serialise.
    """
    print(json.dumps(payload, default=str, sort_keys=True))


def _finalise(summary: dict[str, Any], failed: list[str], total_steps: int) -> int:
    """Derive a truthful status, emit the summary, and choose an exit code.

    Args:
        summary: The step results collected so far. Mutated in place with
            ``status`` and, when anything failed, ``failed_steps``.
        failed: Names of the steps that failed.
        total_steps: How many steps were attempted.

    Returns:
        The process exit code: 0 normally, or 1 when BEAGLE_HOOK_STRICT=1 and
        at least one step failed.
    """
    if not failed:
        summary["status"] = "ok"
    elif len(failed) >= total_steps:
        summary["status"] = "error"
    else:
        summary["status"] = "degraded"

    if failed:
        summary["failed_steps"] = failed
        # pre-commit shows a hook's stderr and hides its stdout when the hook
        # passes, so a status buried in the JSON alone is invisible in the
        # place people actually look.
        print(
            f"beagle hook: {summary['status']} — failed step(s): {', '.join(failed)}",
            file=sys.stderr,
        )

    _emit(summary)
    return 1 if (failed and os.environ.get("BEAGLE_HOOK_STRICT") == "1") else 0


def _rag_endpoint_available(timeout_s: float = 2.0) -> bool:
    """Probe the local embedding endpoint with a hard short timeout.

    Used so the hook can gracefully move on when no RAG/embedding backend
    is reachable instead of hanging on an untimed internal call later.

    Args:
        timeout_s: Socket timeout for the probe (kept tiny on purpose).

    Returns:
        True when the endpoint answered within ``timeout_s``.
    """
    import urllib.error
    import urllib.request

    url = os.environ.get("BEAGLE_OLLAMA_HOST_PROBE", "http://127.0.0.1:11434/api/tags")
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return bool(resp.read(16))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def main() -> int:
    """Run the bootstrap steps and report what actually happened.

    Returns:
        The process exit code. See the module docstring.
    """
    # Re-exec under an interpreter that can import beagle, before any read of
    # sys.stdin. The SessionStart event fires once per session; the re-exec
    # cost is paid once, not once per tool call.
    bootstrap = _load_bootstrap()
    if bootstrap is not None:
        bootstrap.ensure_beagle_interpreter("beagle_bootstrap")

    # Child mode: this process IS the detached hydration worker spawned by a
    # previous hook run. Do only step 2, notify, exit. Dispatched after the
    # interpreter re-exec so a manually launched child self-heals too.
    if os.environ.get(_HYDRATION_CHILD_ENV) == "1":
        return _run_child_hydration()

    # Resolve the project root for the Beagle internals.
    project_dir = os.environ.get("BEAGLE_PROJECT_ROOT") or str(_PROJECT_ROOT)

    summary: dict[str, Any] = {
        "project_dir": project_dir,
        "agent_sync": None,
        "hydration": None,
        "rehydration_registered": False,
    }
    failed: list[str] = []
    # Per-system booleans for the receipt (BS-1-D3).
    system_results: dict[str, bool] = {
        "render": False,
        "rag_staleness": False,
        "auto_hydrate": False,
        "registry_sync": False,
        "rehydration_checkpoint": False,
    }

    # Step 0: Force-render the Top-of-Mind artefact so the doctrine is fresh
    # in context at the start of the session. A session-start render must not
    # depend on the cache policy (the previous artefact may be unhydrated or
    # stale), so this uses force=True. It runs before the agent sync and
    # hydration steps.
    try:
        from beagle.style_guides.render import GooseTopOfMindRenderer

        GooseTopOfMindRenderer().render_canonical(force=True)
        summary["render"] = "ok"
        system_results["render"] = True
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        summary["render"] = {"status": "error", "error": repr(exc)}
        failed.append("render")

    # Step 1: Sync recipes -> agents so the orchestrator can
    # discover Beagle agents before any RAG query runs. We do this
    # BEFORE hydration so the agent registry is populated by the
    # time the first task lands.
    try:
        from beagle.context.recipe_agent_bridge import (
            on_beagle_init,
        )

        agent_result = on_beagle_init()
        summary["agent_sync"] = agent_result
        system_results["registry_sync"] = True
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        summary["agent_sync"] = {"status": "error", "error": repr(exc)}
        failed.append("agent_sync")

    # Step 2: Auto-hydrate (mark RAG fresh OR run a hot-swap if
    # stale). This is the primary user-visible action: the next
    # rag_search call will return up-to-date results without
    # blocking the session for the full 17-minute ingest.
    if os.environ.get(_HYDRATION_CHILD_ENV) == "1":
        # Unreachable in practice — main() dispatches to _run_child_hydration
        # before the step list. Kept as defence against re-entry.
        summary["hydration"] = {"status": "skipped_in_child_mode"}
    elif os.environ.get(_HYDRATION_SYNC_ENV) == "1":
        try:
            from beagle.context.hydration_hook import (
                on_session_start,
            )

            hydr_result = on_session_start(project_dir=project_dir, force=False)
            summary["hydration"] = hydr_result
            # The hydration step both marks the RAG fresh/stale and may reingest.
            system_results["rag_staleness"] = True
            system_results["auto_hydrate"] = True
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            summary["hydration"] = {"status": "error", "error": repr(exc)}
            failed.append("hydration")
    else:
        if not _rag_endpoint_available():
            # Graceful move-on (2026-08-22 directive): no reachable RAG/
            # embedding backend is NOT an error — record the note, skip the
            # spawn, and let the commit proceed. When the backend returns,
            # ingest must run in the BACKGROUND with LOW LIMITS (small
            # embed batches + inter-batch pause), chunked: slow but it
            # happens, never saturating the shared runner.
            note = (
                "RAG/embedding endpoint unreachable — ingest deferred. "
                "When available, run a background ingest with low limits "
                "(BEAGLE_EMBED_BATCH_SIZE=8, BEAGLE_EMBED_BATCH_PAUSE_S=2.0): "
                "chunked and slow but continuous."
            )
            summary["hydration"] = {"status": "skipped_no_rag", "note": note}
            system_results["hydration_deferred"] = True
            _write_hydration_job(
                {
                    "job_id": str(uuid.uuid4()),
                    "status": "skipped_no_rag",
                    "note": note,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
        else:
            try:
                child_env = dict(os.environ)
                child_env[_HYDRATION_CHILD_ENV] = "1"
                child_env.pop(_HYDRATION_SYNC_ENV, None)
                # Low-limit posture for the background ingest (see note above):
                # small batches + pacing so the chunked reingest stays slow
                # but continuous instead of saturating the embedding runner.
                child_env.setdefault("BEAGLE_EMBED_BATCH_SIZE", "8")
                child_env.setdefault("BEAGLE_EMBED_BATCH_PAUSE_S", "2.0")
                _HYDRATION_BG_LOG.parent.mkdir(parents=True, exist_ok=True)
                # Popen duplicates the descriptors, so closing our handle after
                # the spawn is safe — the detached child keeps its own copy.
                with open(_HYDRATION_BG_LOG, "a", encoding="utf-8") as log_handle:
                    proc = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve())],
                        env=child_env,
                        cwd=str(_PROJECT_ROOT),
                        stdout=log_handle,
                        stderr=log_handle,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                summary["hydration"] = {
                    "status": "deferred_to_background",
                    "pid": proc.pid,
                    "job_status_file": str(_HYDRATION_JOB_FILE),
                    "log_file": str(_HYDRATION_BG_LOG),
                }
                summary["hydration_note"] = (
                    f"commit proceeds now; completion notification lands in {_HYDRATION_JOB_FILE}"
                )
                system_results["hydration_deferred"] = True
            except (OSError, RuntimeError) as exc:
                summary["hydration"] = {"status": "error", "error": repr(exc)}
                failed.append("hydration")

    # Step 3: Register the post-compaction rehydration hook so a
    # future context compaction does not silently lose session
    # state. This is idempotent: registering twice is a no-op.
    try:
        from beagle.context.post_compaction_rehydration import (
            on_post_compaction,
        )

        # Probe-call: invoking on_post_compaction here would
        # actually fire the rehydration (good for the first
        # invocation, redundant on subsequent ones). The Doctrine
        # of Idempotency says "safe to run multiple times", so a
        # single fire-and-replace is fine. If the user has
        # explicitly disabled this via BEAGLE_SKIP_HYDRATION=1,
        # honour that and skip the probe-call.
        if not os.environ.get("BEAGLE_SKIP_HYDRATION"):
            # The probe builds the full rehydration prompt (reads project
            # context files, re-renders Top-of-Mind). That work is unbounded
            # inline I/O+CPU — the same class of stall as the hydration embed
            # — so it runs under a daemon-thread wall-clock cap. A cap hit is
            # REPORTED as a failed step, never allowed to hang the commit.
            probe_error: BaseException | None = None

            def _probe() -> None:
                nonlocal probe_error
                try:
                    on_post_compaction(project_dir=Path(project_dir))
                except (ImportError, OSError, RuntimeError, ValueError) as exc:
                    probe_error = exc

            probe_thread = threading.Thread(
                target=_probe, daemon=True, name="beagle-rehydrate-probe"
            )
            probe_thread.start()
            probe_thread.join(timeout=_PROBE_TIMEOUT_S)
            if probe_thread.is_alive():
                raise TimeoutError(f"rehydration probe exceeded {_PROBE_TIMEOUT_S}s")
            if probe_error is not None:
                raise probe_error
        summary["rehydration_registered"] = True
        system_results["rehydration_checkpoint"] = True
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        summary["rehydration_registered"] = False
        summary["rehydration_error"] = repr(exc)
        failed.append("rehydration")

    # Write the receipt so BS-2 can gate on "did the bootstrap run".
    trigger = "pre_commit" if os.environ.get("PRE_COMMIT") else "session_start"
    _write_receipt(trigger, system_results)

    return _finalise(summary, failed, total_steps=4)


def _supervised_reexec() -> int:
    """Re-exec this script under an external wall-clock supervisor.

    The in-process SIGALRM budget cannot break every observed wedge: a
    futex held inside a GIL-owning C section defeats signal delivery
    (seen twice on 2026-08-22). Running the real work as a CHILD of this
    supervisor gives kill-from-outside semantics with zero shell
    involvement — subprocess.run(timeout=...) SIGKILLs a wedged child
    and returns control here, where a degraded summary is emitted and
    the hook exits 0. Telemetry must never hold a commit hostage.

    Returns:
        The exit code for the top-level process (0 on watchdog expiry).
    """
    budget = int(os.environ.get(_HOOK_BUDGET_ENV, str(_HOOK_DEFAULT_BUDGET_S)))
    child_env = dict(os.environ)
    child_env[_WATCHDOG_ENV] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            env=child_env,
            cwd=str(_PROJECT_ROOT),
            timeout=budget,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        _emit(
            {
                "status": "degraded",
                "failed_steps": ["external_budget"],
                "error": f"bootstrap exceeded {budget}s external watchdog",
                "note": "commit proceeds; background work may still be "
                "pending — see ~/.beagle/hydration_job.json",
            }
        )
        print(
            f"beagle hook: degraded — external watchdog killed at {budget}s",
            file=sys.stderr,
        )
        return 0


if __name__ == "__main__":
    try:
        if os.environ.get(_WATCHDOG_ENV) != "1" and os.environ.get(_HYDRATION_CHILD_ENV) != "1":
            # Top-level invocation (pre-commit / session-start): supervise a
            # fresh child instead of running inline, so even an
            # uninterruptible wedge is killable from outside. The hydration
            # child skips this — it legitimately runs a long ingest.
            sys.exit(_supervised_reexec())
        if os.environ.get(_HYDRATION_CHILD_ENV) != "1":
            # Parent only: the child legitimately runs a long ingest and must
            # NOT be budget-killed. The alarm interrupts even a socket stuck
            # in futex/recv — the handler raises into the main thread.
            budget = int(os.environ.get(_HOOK_BUDGET_ENV, str(_HOOK_DEFAULT_BUDGET_S)))
            signal.signal(signal.SIGALRM, _on_budget_alarm)
            signal.alarm(budget)
        exit_code = main()
    except _BudgetExceeded as exc:
        _emit(
            {
                "status": "degraded",
                "failed_steps": ["wall_clock_budget"],
                "error": str(exc),
                "note": "hook budget hit; commit proceeds, background work may "
                "still be pending — see ~/.beagle/hydration_job.json",
            }
        )
        print(f"beagle hook: degraded — wall-clock budget hit: {exc}", file=sys.stderr)
        exit_code = 0
    except (ImportError, OSError, RuntimeError, ValueError, KeyError) as exc:
        # Last-resort handler: an ImportError in a hook should NOT
        # crash the surrounding toolchain. Print a JSON error and
        # exit 0 so pre-commit and MCP can continue.
        _emit(
            {
                "status": "error",
                "error": repr(exc),
                "error_type": type(exc).__name__,
            }
        )
        exit_code = 0
    finally:
        signal.alarm(0)
    sys.exit(exit_code)
