"""B-2 regression locks — the incremental hot-swap path.

Audit v13.22.1 found `_incremental_update` importing `_chunk_file` from
cast_ingestion, a function that does not exist. The ImportError was caught by
a broad handler and returned as `status="error"`, and `hotswap_ingest` only
fell back to the full re-index on `status="fallback"` — so any changeset
between 1 file and 40% of the tree was a hard failure that left the index
permanently stale, with the staleness flag never cleared.

The most important test here is
`test_hotswap_degrades_to_full_when_chunking_breaks`: whatever goes wrong
inside the incremental path, the full rebuild must still run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.infrastructure import cast_ingestion as ci
from beagle.infrastructure import delta_engine as de
from beagle.infrastructure import hotswap_ingest as hi


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(10):
        (root / f"mod{i}.py").write_text(f"def fn{i}():\n    return {i}\n", encoding="utf-8")
    return root


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """Point the delta state and both stores at tmp_path; stub the embedder.

    Uses monkeypatch.setenv to point BEAGLE_KNOWLEDGE_DIR at tmp_path so the
    rag_paths __getattr__-backed LANCEDB_URI/KUZU_URI resolve dynamically.
    Setting ci.LANCEDB_URI directly via setattr would create a real __dict__
    entry that monkeypatch cannot properly undo (it shadows __getattr__).
    """
    monkeypatch.setattr(de, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(de, "_STATE_FILE", tmp_path / "rag_state.json")
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))

    class _Stub:
        provider = "stub"
        dimension = 4

        def identity(self):
            return {"provider": "stub", "model": "stub", "prefix": ""}

        def encode(self, texts, **kw):
            return [[float(len(t) % 7), 1.0, 2.0, 3.0] for t in texts]

    monkeypatch.setattr(ci, "_resolve_embedder", lambda: _Stub())
    return tmp_path


def _seed_state(root: Path) -> list[str]:
    """Record state for the whole tree, as a completed full ingest would."""
    files = [str(p) for p in ci.scan_codebase(root)]
    de.update_state_after_ingestion(files, dict.fromkeys(files, 1))
    return files


def _touch(path: Path) -> None:
    import os
    import time

    path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))


# ── (a) a small change takes the incremental path ────────────────────────


def test_single_file_change_routes_to_staging_swap(repo, isolated_stores):
    """v13.22.3: small changesets no longer mutate in place.

    The previous contract — a 1-file change wrote directly to the live
    Kùzu/LanceDB — collided with the RAG server's held read-only Kùzu
    handle and the read-only chmod on the live data dir, so the
    in-place branch always failed and degraded to staging→swap anyway.
    The two paths are now collapsed: any non-empty changeset returns
    ``status="fallback"`` with a clear reason, and the caller (the
    auto-reingest path or a manual ``rag_hotswap_ingest``) drives the
    full stage→swap. This test pins the new contract.
    """
    _seed_state(repo)
    # Seed the vector store so the delta has something to be measured
    # against (otherwise the change ratio would be 1.0 and the
    # noop-detection would dominate).
    ci.rebuild_lancedb_index([_chunk_for(p) for p in ci.scan_codebase(repo)])

    _touch(repo / "mod3.py")

    result = hi._incremental_update(str(repo))
    assert result["status"] == "fallback", result
    assert result["fallback_reason"] == "in_place_branch_removed_v13_22_3", result
    assert result["modified"] == 1
    assert result["unchanged"] == 9


def _chunk_for(p: Path) -> ci.ASTChunk:
    return ci.ASTChunk(
        chunk_id=f"{p}::stub",
        filepath=str(p),
        language="python",
        node_type="function_definition",
        node_name=p.stem,
        start_line=1,
        end_line=2,
        text=p.read_text(encoding="utf-8"),
        token_count=5,
    )


def test_incremental_noop_does_not_re_run_when_state_is_clean(repo, isolated_stores):
    """v13.22.3: when nothing has changed, _incremental_update is a no-op.

    The noop fast path is preserved (delta.unchanged_count == files),
    so the second call is still a no-op. The in-place write was the
    only path removed; the cheap skip-cache lookup is unchanged.
    """
    _seed_state(repo)
    ci.rebuild_lancedb_index([_chunk_for(p) for p in ci.scan_codebase(repo)])
    _touch(repo / "mod4.py")
    # First call: real change → fallback to staging (no in-place write).
    first = hi._incremental_update(str(repo))
    assert first["status"] == "fallback", first
    assert first["fallback_reason"] == "in_place_branch_removed_v13_22_3"

    # Re-seed the state to simulate "the staging path finished cleanly";
    # the noop detection should fire on the second call.
    _seed_state(repo)
    ci.rebuild_lancedb_index([_chunk_for(p) for p in ci.scan_codebase(repo)])

    second = hi._incremental_update(str(repo))
    assert second["status"] == "ok"
    assert second["noop"] is True, "state was not recorded — the delta never converges"


def test_incremental_does_not_truncate_the_vector_index(repo, isolated_stores):
    """v13.22.3: incremental path no longer mutates the live index at all.

    The previous test verified that an in-place write preserved the
    unchanged files. The new contract is: a non-empty changeset returns
    ``status="fallback"`` and NEVER touches the live LanceDB. The
    caller (the swap path) is responsible for the atomic stage→swap.
    This test pins that the live index is not even read-mutated.
    """
    import lancedb

    _seed_state(repo)
    ci.rebuild_lancedb_index([_chunk_for(p) for p in ci.scan_codebase(repo)])

    def _rows():
        db = lancedb.connect(ci.LANCEDB_URI)
        return db.open_table(ci.LANCE_TABLE_NAME).search().limit(10_000).to_list()

    assert len(_rows()) == 10

    _touch(repo / "mod7.py")
    result = hi._incremental_update(str(repo))
    assert result["status"] == "fallback", result
    # CRITICAL: the live index row count must be UNCHANGED — the
    # incremental function is contractually forbidden from writing to
    # the live stores. The 10 files are still there.
    paths = {r["filepath"] for r in _rows()}
    assert len(paths) == 10, f"index truncated to {len(paths)} files"


# ── (b) THE B-2 LOCK: a broken incremental must still rebuild ────────────


def test_hotswap_degrades_to_full_when_chunking_breaks(repo, isolated_stores, monkeypatch):
    """Whatever fails inside the incremental path, the full path must run.

    This is the exact shape of B-2: `chunk_file` was missing, the resulting
    ImportError became status="error", and hotswap_ingest returned it to the
    caller instead of falling back — so the index was never refreshed.
    """
    _seed_state(repo)
    _touch(repo / "mod1.py")

    def _boom(*a, **kw):
        raise ImportError("simulated missing symbol, as in B-2")

    monkeypatch.setattr(ci, "chunk_file", _boom)

    calls: list[str] = []

    def _fake_stage(target_directory, staging_dir=None):
        calls.append(target_directory)
        return {"status": "ok", "files_processed": 10, "chunks_created": 40, "errors": []}

    def _fake_swap(**kw):
        return {"status": "ok", "swapped": ["lancedb", "kuzu"]}

    monkeypatch.setattr(hi, "stage_ingest", _fake_stage)
    monkeypatch.setattr(hi, "swap_staged_to_live", _fake_swap)

    result = hi.hotswap_ingest(str(repo))

    assert result["status"] == "ok", result
    assert result["mode"] == "full", "did not fall back to the full re-index"
    assert calls == [str(repo)], "stage_ingest was never reached"


def test_hotswap_reports_incremental_error_even_on_success(repo, isolated_stores, monkeypatch):
    """The degradation must be visible, not silent."""
    _seed_state(repo)
    _touch(repo / "mod2.py")
    monkeypatch.setattr(hi, "_incremental_update", lambda t: {"status": "error", "error": "boom"})
    monkeypatch.setattr(
        hi,
        "stage_ingest",
        lambda target_directory, staging_dir=None: {"status": "ok", "errors": []},
    )
    monkeypatch.setattr(hi, "swap_staged_to_live", lambda **kw: {"status": "ok"})

    result = hi.hotswap_ingest(str(repo))
    assert result["incremental_error"] == "boom"


# ── (c) big changesets go straight to the full path ──────────────────────


def test_large_changeset_falls_back(repo, isolated_stores):
    """v13.22.3: any non-empty changeset returns fallback; the >40%
    change_ratio check was specific to the in-place write path and is
    no longer the trigger (the trigger now is "non-empty at all").

    Both a small and a large changeset route through the same
    staging→swap path; the only difference is wall-clock cost.
    """
    _seed_state(repo)
    for i in range(6):  # 6/10 = 60% > 40% threshold
        _touch(repo / f"mod{i}.py")

    result = hi._incremental_update(str(repo))
    assert result["status"] == "fallback", result
    # fallback_reason is now the v13.22.3 marker, not the >40% ratio.
    assert result["fallback_reason"] == "in_place_branch_removed_v13_22_3", result


def test_missing_state_falls_back(repo, isolated_stores):
    """v13.22.3: with no state file and a real change, the function
    returns fallback to staging→swap. There is no "in-place" path
    to preserve. (A missing state with no changes is a noop; that
    case is covered by ``test_noop_performs_no_store_writes``.)
    """
    # No _seed_state → state file is absent.
    _touch(repo / "mod0.py")  # an existing file (one of mod0..mod9)

    result = hi._incremental_update(str(repo))
    assert result["status"] == "fallback", result
    # With state absent, every file counts as changed → the v13.22.3
    # fallback marker fires.
    assert result["fallback_reason"] == "in_place_branch_removed_v13_22_3", result


# ── (d) no changes ⇒ no store writes at all ──────────────────────────────


def test_noop_performs_no_store_writes(repo, isolated_stores, monkeypatch):
    """v13.22.3: the noop fast-path is the only path that doesn't write
    anything. The in-place write helpers are not called from
    _incremental_update anymore, so the monkeypatched
    ``upsert_lancedb_chunks`` / ``build_kuzu_graph`` are never
    invoked. The function imports them only inside the
    ``try: from .cast_ingestion import (build_kuzu_graph, ...):``
    block — that block now imports ``scan_codebase`` only — so the
    patches aren't even needed. We leave them in to assert the
    no-touch behaviour.
    """
    _seed_state(repo)

    touched: list[str] = []
    monkeypatch.setattr(
        ci, "upsert_lancedb_chunks", lambda *a, **k: touched.append("lance") or True
    )
    monkeypatch.setattr(ci, "build_kuzu_graph", lambda *a, **k: touched.append("kuzu") or True)

    result = hi._incremental_update(str(repo))
    assert result["status"] == "ok"
    assert result["noop"] is True
    # v13.22.3: the in-place branch is gone, so even if a future
    # change accidentally re-introduces it, this assertion would fire.
    assert touched == [], f"noop still wrote to the stores: {touched}"


# ── (e) deletions propagate ──────────────────────────────────────────────


def test_deleted_file_is_detected_and_routes_to_staging(repo, isolated_stores):
    """v13.22.3: a deleted file shows up in the delta but the
    in-place write that would have removed it from the live LanceDB
    is gone. The function now returns ``status="fallback"`` so the
    caller drives the atomic stage→swap. The live LanceDB is NOT
    mutated by the incremental function.
    """
    import lancedb

    _seed_state(repo)
    ci.rebuild_lancedb_index([_chunk_for(p) for p in ci.scan_codebase(repo)])

    victim = repo / "mod5.py"
    victim.unlink()

    result = hi._incremental_update(str(repo))
    # The deletion shows up in the delta; the function returns
    # fallback to staging.
    assert result["status"] == "fallback", result
    assert result["fallback_reason"] == "in_place_branch_removed_v13_22_3", result
    assert result["deleted"] == 1

    # CRITICAL: the live LanceDB is UNCHANGED — the incremental
    # function does not write to it. The caller must run
    # hotswap_ingest() to actually remove the deleted file's chunks.
    db = lancedb.connect(ci.LANCEDB_URI)
    rows = db.open_table(ci.LANCE_TABLE_NAME).search().limit(10_000).to_list()
    assert str(victim) in {r["filepath"] for r in rows}, (
        "live index was mutated; incremental function should be forbidden from writing to it"
    )


# ── (f) unreadable files are not recorded as indexed ─────────────────────


def test_unreadable_file_does_not_block_fallback(repo, isolated_stores, monkeypatch):
    """v13.22.3: the unreadable-file handling moved out of
    _incremental_update (which no longer has an in-place write
    path) into the staging path. The incremental function now
    returns fallback immediately on any non-empty delta, regardless
    of which files are unreadable.

    This test pins the new contract: a real change → fallback to
    staging (where unreadable files surface via the IngestionResult
    warnings, not via the incremental function).
    """
    _seed_state(repo)
    ci.rebuild_lancedb_index([_chunk_for(p) for p in ci.scan_codebase(repo)])
    bad = repo / "mod8.py"
    _touch(bad)
    _touch(repo / "mod7.py")  # ensure at least one file contributes

    # v13.22.3: _incremental_update no longer imports chunk_file, so
    # patching it here is harmless; the function returns fallback
    # before any chunking work is attempted.
    result = hi._incremental_update(str(repo))
    assert result["status"] == "fallback", result
    assert result["fallback_reason"] == "in_place_branch_removed_v13_22_3", result
    # The unreadable file is still in the delta (it was modified),
    # but its handling is now the staging path's problem, not the
    # incremental function's.
