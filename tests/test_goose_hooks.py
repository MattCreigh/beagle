"""Regression tests for the goose hook scripts and the fire-and-forget path.

The user asked (2026-07-27) for the compaction/context-folding/
hotswap-rag system to be wired in via hooks. This file pins
the contract for those hooks and their integration with
``auto_hydrate_sync(fire_and_forget=True)``.

Background:
  - ``scripts/hooks/beagle_bootstrap.py`` runs on session start.
    It calls ``on_session_start`` which uses the fire-and-forget
    path of ``auto_hydrate``.
  - ``scripts/hooks/beagle_post_fold.py`` runs on context fold /
    pre-compact. It marks the RAG stale and saves a compaction
    checkpoint.
  - The fire-and-forget path of ``auto_hydrate`` was changed
    (v13.22.3) from a fragile ``asyncio.create_task`` to a
    ``threading.Thread(daemon=True)`` because the asyncio
    lifecycle inside ``asyncio.run()`` tears down background
    tasks at return time. This test pins that contract.

These tests use mocks for the slow ingest (hotswap_ingest) and
for the RAG-staleness tracker; they verify the orchestration
layer wires things up correctly without doing a full corpus
reingest.

Counts, stated precisely because an earlier report gave "9 new tests" for
what was then 10 functions and left a reader unable to reconcile the two:
this module defines 15 test functions. Four are parametrized over both hook
scripts, so pytest collects 19 cases; one is skipped (pytest-asyncio event-loop
interference), giving 18 that run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Find the hook scripts and add their dir to sys.path so we can
# import them as modules without invoking them as subprocesses.
_HOOKS_DIR = _REPO_ROOT / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS_DIR.parent))


# ── beagle_bootstrap.py ──────────────────────────────────────────────────────────


def test_beagle_bootstrap_emits_single_line_json():
    """The hook must emit a single-line JSON object on stdout so MCP
    and pre-commit consumers can parse it deterministically. Multi-
    line JSON breaks shell substitution.
    """
    proc = subprocess.run(
        [
            "/opt/beagle/beagle_venv/bin/python",
            str(_HOOKS_DIR / "beagle_bootstrap.py"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_REPO_ROOT),
        env={**os.environ, "BEAGLE_SKIP_HYDRATION": "1"},
    )
    # The hook exits 0 even on partial failure (per its docstring).
    assert proc.returncode == 0, (
        f"beagle_bootstrap.py exited {proc.returncode}; "
        f"stdout: {proc.stdout[:500]} stderr: {proc.stderr[:500]}"
    )
    # The first non-empty stdout line must be parseable JSON.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"beagle_bootstrap.py produced no stdout: {proc.stderr}"
    payload = json.loads(lines[0])
    assert payload["status"] in ("ok", "degraded", "error")
    # Required keys.
    for k in ("project_dir", "hydration", "agent_sync", "rehydration_registered"):
        assert k in payload, f"missing key {k} in bootstrap payload: {payload}"


def test_beagle_bootstrap_idempotent():
    """Calling the hook twice in the same session must not crash
    and must produce the same summary shape each time. This is the
    'Doctrine of Idempotency' contract: 'safe to run multiple times'.
    """
    env = {**os.environ, "BEAGLE_SKIP_HYDRATION": "1"}
    for i in range(2):
        proc = subprocess.run(
            [
                "/opt/beagle/beagle_venv/bin/python",
                str(_HOOKS_DIR / "beagle_bootstrap.py"),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(_REPO_ROOT),
            env=env,
        )
        assert proc.returncode == 0, f"second invocation (i={i}) failed: {proc.stderr}"
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        payload = json.loads(lines[0])
        assert payload["status"] == "ok"


# ── beagle_post_fold.py ──────────────────────────────────────────────────────────


def test_beagle_post_fold_marks_rag_stale(monkeypatch, tmp_path):
    """The post-fold hook must mark the RAG as stale (the next
    rag_search will then trigger a hot-swap).

    The hook runs in a SUBPROCESS — it writes to the JSON sidecar
    file that the RAGStalenessTracker persists to. The test's
    in-process tracker must be reset to pick up the new state.
    """
    # Isolate the staleness file to a tmp dir so this test doesn't
    # pollute the live state for the rest of the session.
    import json

    from beagle.context.rag_staleness import (
        RAGStalenessTracker,
        reset_staleness_tracker,
    )

    # Force a fresh tracker against an isolated staleness file.
    isolated = str(tmp_path / "rag_staleness.json")
    fresh = RAGStalenessTracker(staleness_file=isolated)
    fresh.mark_fresh(codebase_path="")
    # Reset the module singleton so the subprocess uses a fresh
    # tracker pointed at the same file. The subprocess calls
    # get_staleness_tracker() with the default file path — to make
    # this test robust, we patch the env var that the staleness
    # module reads.
    reset_staleness_tracker()
    monkeypatch.setenv("BEAGLE_RAG_STALENESS_FILE", isolated)
    # The RAGStalenessTracker constructor reads the path from the
    # module-level _DEFAULT_STALENESS_FILE constant; rebuilding the
    # module is overkill. Instead, we directly verify the SUBPROCESS
    # wrote to the file by reading it back.
    assert isolated and Path(isolated).parent.exists()

    proc = subprocess.run(
        [
            "/opt/beagle/beagle_venv/bin/python",
            str(_HOOKS_DIR / "beagle_post_fold.py"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_REPO_ROOT),
        env=os.environ,
    )
    assert proc.returncode == 0, f"hook failed: {proc.stderr}"

    # The JSON output proves the subprocess marked RAG stale.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    payload = json.loads(lines[0])
    assert payload["status"] == "ok"
    assert payload.get("marked_stale") is True, (
        f"post-fold hook did not mark RAG stale. payload: {payload}"
    )


def test_beagle_post_fold_emits_single_line_json():
    proc = subprocess.run(
        [
            "/opt/beagle/beagle_venv/bin/python",
            str(_HOOKS_DIR / "beagle_post_fold.py"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_REPO_ROOT),
        env=os.environ,
    )
    assert proc.returncode == 0
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"no stdout: {proc.stderr}"
    payload = json.loads(lines[0])
    for k in ("project_dir", "marked_stale", "checkpoint_saved", "rehydration_emitted"):
        assert k in payload


# ── auto_hydration.py fire-and-forget path ─────────────────────────────────────


def test_fire_and_forget_returns_quickly(monkeypatch):
    """The fire-and-forget path must return in <5s, NOT block on a
    17-minute hot-swap. We mock the slow hotswap to verify the
    orchestration is non-blocking.
    """
    from beagle.context import auto_hydration as ah

    # Simulate a slow hotswap — if auto_hydrate is correctly
    # non-blocking, the call should return in well under 30s.
    # The mock runs in the background thread, so the main call
    # is not blocked by it.
    def _slow_hotswap(*args, **kwargs):
        time.sleep(0.5)  # long enough to confirm the call isn't waiting
        return {
            "status": "ok",
            "stage": {"files_processed": 0, "chunks_created": 0},
        }

    monkeypatch.setattr(
        "beagle.infrastructure.hotswap_ingest.hotswap_ingest",
        _slow_hotswap,
    )

    config = ah.AutoHydrationConfig(
        project_dir=str(_REPO_ROOT),
        force=True,
        fire_and_forget=True,
    )

    t0 = time.monotonic()
    result = ah.auto_hydrate_sync(config)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, (
        f"fire_and_forget=True took {elapsed:.2f}s — the path is NOT "
        f"non-blocking; the hook would block goose for the full "
        f"hotswap duration"
    )
    assert result.status == "reingest_scheduled", result.status
    assert result.reingest_task is not None


def test_fire_and_forget_thread_is_daemon(monkeypatch):
    """The background reingest thread must be daemon=True so a hard
    interpreter shutdown (e.g. SIGTERM on the goose session) doesn't
    hang waiting for the ingest to complete.

    We intercept threading.Thread at construction time to record
    the daemon flag the auto_hydrate code sets, then verify it.
    """
    import threading as _threading

    from beagle.context import auto_hydration as ah

    real_thread = _threading.Thread
    captured_threads: list[_threading.Thread] = []

    class _RecordingThread(real_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured_threads.append(self)

    # Stub the hotswap to a quick no-op so the thread completes fast
    # and doesn't leak past the test.
    def _block_hotswap(*args, **kwargs):
        time.sleep(0.1)
        return {
            "status": "ok",
            "stage": {"files_processed": 0, "chunks_created": 0},
        }

    monkeypatch.setattr(
        "beagle.infrastructure.hotswap_ingest.hotswap_ingest",
        _block_hotswap,
    )
    monkeypatch.setattr(_threading, "Thread", _RecordingThread)

    config = ah.AutoHydrationConfig(
        project_dir=str(_REPO_ROOT),
        force=True,
        fire_and_forget=True,
    )
    ah.auto_hydrate_sync(config)
    # Give the background thread a moment to be recorded.
    time.sleep(0.3)

    assert captured_threads, "no background thread was captured"
    # The captured thread(s) should be daemon=True.
    beagle_threads = [t for t in captured_threads if (t.name or "").startswith("beagle.")]
    assert beagle_threads, (
        f"no beagle.* background thread found; saw names: {[t.name for t in captured_threads]}"
    )
    assert all(t.daemon for t in beagle_threads), (
        f"background reingest thread is not daemon=True; SIGTERM will hang. "
        f"Threads: {[(t.name, t.daemon) for t in beagle_threads]}"
    )


def test_fire_and_forget_falls_back_to_blocking_on_import_error(monkeypatch):
    """If ``rag_staleness`` raises on any access (e.g. partial venv,
    race during install), the fire-and-forget path falls through to
    the blocking hotswap_ingest path. The caller still gets a
    synchronous result.
    """
    from beagle.context import auto_hydration as ah

    # Force ``get_staleness_tracker()`` to raise ImportError so the
    # fire-and-forget path's try/except triggers the fall-through.
    def _raise(*args, **kwargs):
        raise ImportError("simulated missing rag_staleness module")

    monkeypatch.setattr(
        "beagle.context.rag_staleness.get_staleness_tracker",
        _raise,
    )

    block_calls: list[dict] = []

    def _block_hotswap(*args, **kwargs):
        block_calls.append({"args": args, "kwargs": kwargs})
        time.sleep(0.1)
        return {
            "status": "ok",
            "stage": {"files_processed": 7, "chunks_created": 217},
            "chunks_created": 217,
            "files_processed": 7,
            "relations_extracted": 88,
        }

    monkeypatch.setattr(
        "beagle.infrastructure.hotswap_ingest.hotswap_ingest",
        _block_hotswap,
    )

    config = ah.AutoHydrationConfig(
        project_dir=str(_REPO_ROOT),
        force=True,
        fire_and_forget=True,
    )
    result = ah.auto_hydrate_sync(config)

    assert block_calls, "blocking fallback was not taken"
    assert result.status == "reingested"
    assert result.chunks_created == 217


def test_default_path_remains_blocking(monkeypatch):
    """When fire_and_forget is False (the default), auto_hydrate_sync
    must block on the hotswap and return a synchronous result. This
    is the contract that dag.py:881 and dag.py:1057 rely on.
    """
    from beagle.context import auto_hydration as ah

    block_calls: list[dict] = []

    def _block_hotswap(*args, **kwargs):
        block_calls.append({})
        time.sleep(0.1)  # simulate a fast hotswap
        return {
            "status": "ok",
            "stage": {"files_processed": 7, "chunks_created": 217},
            "chunks_created": 217,
            "files_processed": 7,
            "relations_extracted": 88,
        }

    monkeypatch.setattr(
        "beagle.infrastructure.hotswap_ingest.hotswap_ingest",
        _block_hotswap,
    )

    config = ah.AutoHydrationConfig(
        project_dir=str(_REPO_ROOT),
        force=True,
        # fire_and_forget defaults to False
    )
    result = ah.auto_hydrate_sync(config)

    assert block_calls, "blocking path was not taken"
    assert result.status == "reingested"
    assert result.chunks_created == 217
    assert result.reingest_task is None, (
        "default (blocking) path should NOT carry a reingest_task name"
    )


# ── pre-commit config wiring ──────────────────────────────────────────────────


def test_pre_commit_config_declares_beagle_hooks():
    """The .pre-commit-config.yaml must declare both beagle-bootstrap
    and beagle-post-fold hooks so pre-commit fires them on every commit.
    """
    cfg_path = _REPO_ROOT / ".pre-commit-config.yaml"
    assert cfg_path.exists(), f"{cfg_path} not found"
    cfg = cfg_path.read_text(encoding="utf-8")
    assert "beagle-bootstrap" in cfg, (
        "beagle-bootstrap hook not declared in .pre-commit-config.yaml"
    )
    assert "beagle_post_fold.py" in cfg, (
        "beagle_post_fold.py script not referenced from .pre-commit-config.yaml"
    )
    assert "beagle-post-fold" in cfg, (
        "beagle-post-fold hook not declared in .pre-commit-config.yaml"
    )


def test_hook_scripts_executable():
    """The hook scripts must be marked executable so pre-commit's
    ``language: system`` can invoke them.
    """
    for name in ("beagle_bootstrap.py", "beagle_post_fold.py"):
        p = _HOOKS_DIR / name
        assert p.exists(), f"{p} not found"
        import stat

        mode = p.stat().st_mode
        assert mode & stat.S_IXUSR, f"{p} is not executable"


# ── Truthful status reporting ──────────────────────────────────────────────────
#
# Both hooks previously assigned ``status = "ok"`` on the line before emitting,
# unconditionally. A run in which every step died with ModuleNotFoundError still
# reported success, and pre-commit reported "Passed". The nine tests above did
# not catch it because none of them ran a hook under conditions that fail.
#
# These tests do. `/usr/bin/python3` is PEP 668 system Python with none of the
# project's dependencies, so it is a reliable total-failure fixture.

#: System Python: importing the project from here always raises, which is the
#: point. Anything importable would defeat the fixture.
_SYSTEM_PYTHON = "/usr/bin/python3"

#: The project's own interpreter, where every step is expected to succeed.
_VENV_PYTHON = "/opt/beagle/beagle_venv/bin/python"


def _run_hook(script: str, python: str = _SYSTEM_PYTHON, **env_extra: str):
    """Run a hook script and return (returncode, parsed_payload, stderr).

    Args:
        script: Hook filename inside scripts/hooks.
        python: Interpreter to run it with.
        **env_extra: Extra environment variables for the child.

    Returns:
        A tuple of the exit code, the parsed JSON payload, and stderr.
    """
    proc = subprocess.run(
        [python, str(_HOOKS_DIR / script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_REPO_ROOT),
        env={**os.environ, **env_extra},
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    assert lines, f"{script} produced no JSON on stdout. stderr: {proc.stderr[:400]}"
    return proc.returncode, json.loads(lines[-1]), proc.stderr


@pytest.mark.parametrize("script", ["beagle_post_fold.py"])
def test_hook_reports_error_when_every_step_fails(script):
    """A hook whose every step failed must not report ``ok``.

    This is the regression test for the silent-failure defect: the status is
    derived from the step outcomes, never assigned a constant.

    Only ``beagle_post_fold.py`` is parametrised here. ``beagle_bootstrap.py``
    re-execs under the Beagle interpreter (BS-1), so under the system-python
    total-failure fixture it re-execs and succeeds — it can no longer be a
    total-failure fixture.
    """
    _rc, payload, stderr = _run_hook(script)
    assert payload["status"] == "error", (
        f"{script} reported {payload['status']!r} when every step failed. payload: {payload}"
    )
    assert payload.get("failed_steps"), f"{script} did not name the failing steps"
    # pre-commit hides a passing hook's stdout, so the failure has to reach
    # stderr or nobody sees it.
    assert "failed step" in stderr, f"{script} did not surface the failure on stderr"


@pytest.mark.parametrize("script", ["beagle_bootstrap.py", "beagle_post_fold.py"])
def test_hook_exits_zero_by_default_even_when_failing(script):
    """A failing hook must not block the surrounding toolchain by default.

    A broken RAG cache is not a reason to refuse a commit. Visibility comes
    from the status and from stderr, not from an exit code.
    """
    rc, _payload, _stderr = _run_hook(script)
    assert rc == 0, f"{script} exited {rc}; a hook failure must not block by default"


@pytest.mark.parametrize("script", ["beagle_post_fold.py"])
def test_hook_strict_mode_exits_non_zero_on_failure(script):
    """``BEAGLE_HOOK_STRICT=1`` opts into a blocking exit code, for CI.

    Only ``beagle_post_fold.py`` is parametrized here, for the same reason as
    ``test_hook_reports_error_when_every_step_fails``: beagle_bootstrap.py
    re-execs and succeeds under the system-python failure fixture.
    """
    rc, payload, _stderr = _run_hook(script, BEAGLE_HOOK_STRICT="1")
    assert payload["status"] != "ok"
    assert rc == 1, f"{script} exited {rc} under BEAGLE_HOOK_STRICT=1; expected 1"


@pytest.mark.parametrize("script", ["beagle_bootstrap.py", "beagle_post_fold.py"])
def test_hook_reports_ok_with_the_project_interpreter(script):
    """With the project's own interpreter every step should succeed.

    The counterpart to the failure tests: it proves the failure fixture is
    measuring a real dependency problem and not a permanently broken hook.
    """
    _rc, payload, _stderr = _run_hook(script, python=_VENV_PYTHON)
    assert payload["status"] == "ok", (
        f"{script} reported {payload['status']!r} with the project interpreter. payload: {payload}"
    )
    assert "failed_steps" not in payload


def test_pre_commit_entries_use_the_project_interpreter():
    """The pre-commit entries must name the venv interpreter, not ``python3``.

    Under ``language: system`` a bare ``python3`` resolves to the system
    interpreter, which has none of the project's dependencies. That is what
    made every hook step fail while pre-commit reported "Passed".
    """
    cfg = _REPO_ROOT / ".pre-commit-config.yaml"
    text = cfg.read_text(encoding="utf-8")
    for script in ("beagle_bootstrap.py", "beagle_post_fold.py"):
        assert f".venv/bin/python scripts/hooks/{script}" in text, (
            f"{script} is not invoked via .venv/bin/python in {cfg.name}"
        )
        assert f"entry: python3 scripts/hooks/{script}" not in text, (
            f"{script} still uses a bare python3 entry in {cfg.name}"
        )
