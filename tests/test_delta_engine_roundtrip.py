"""B-4 regression locks — the delta engine's state must actually be written.

Audit v13.22.1: `update_state_after_ingestion()` and `remove_from_state()`
had **zero callers** anywhere in the tree. `~/.beagle/rag_state.json` therefore
never existed, so `compute_delta()` always returned
`fallback_required=True, "No state file found (first ingestion)"` and the
incremental path could never be taken — every trigger ran the full
multi-minute re-index while the code and CHANGELOG advertised sub-second
updates.

These tests pin the full round trip: first run demands a full ingest, state
gets recorded, a second run is a no-op, and single-file changes are detected
as exactly that.
"""

from __future__ import annotations

import json

import pytest

from beagle.infrastructure import delta_engine as de


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Redirect the delta engine's state file into tmp_path."""
    path = tmp_path / "rag_state.json"
    monkeypatch.setattr(de, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(de, "_STATE_FILE", path)
    return path


@pytest.fixture
def repo(tmp_path):
    """A tiny source tree of five files."""
    root = tmp_path / "repo"
    root.mkdir()
    files = []
    for i in range(5):
        f = root / f"mod{i}.py"
        f.write_text(f"def fn{i}():\n    return {i}\n", encoding="utf-8")
        files.append(str(f))
    return root, sorted(files)


def _counts(paths, n=1):
    return dict.fromkeys(paths, n)


# ── (a) first run demands a full ingest ──────────────────────────────────


def test_first_delta_requires_fallback(state_file, repo):
    root, files = repo
    result = de.compute_delta(root, files)
    assert result.fallback_required is True
    assert "No state file found" in result.fallback_reason
    assert result.total_files == 5


# ── (b) after recording state, the same tree is a no-op ──────────────────


def test_state_is_written_and_second_delta_is_noop(state_file, repo):
    root, files = repo
    de.update_state_after_ingestion(files, _counts(files, 3))

    assert state_file.exists(), "state file was never written"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert sorted(saved.keys()) == files
    assert saved[files[0]]["chunk_count"] == "3"

    result = de.compute_delta(root, files)
    assert de.is_noop(result) is True
    assert result.unchanged_count == 5
    assert result.fallback_required is False


# ── (c) a single modification is seen as exactly that ────────────────────


def test_single_modification_is_detected(state_file, repo):
    root, files = repo
    de.update_state_after_ingestion(files, _counts(files))

    target = files[2]
    import os
    import time

    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n# touched\n")
    # Force a distinct mtime even on coarse-grained clocks.
    os.utime(target, (time.time() + 5, time.time() + 5))

    result = de.compute_delta(root, files)
    assert result.modified == [target]
    assert result.added == []
    assert result.deleted == []
    assert result.unchanged_count == 4
    assert de.is_noop(result) is False


def test_new_file_is_detected_as_added(state_file, repo):
    root, files = repo
    de.update_state_after_ingestion(files, _counts(files))

    extra = root / "brand_new.py"
    extra.write_text("def new(): pass\n", encoding="utf-8")
    scan = sorted([*files, str(extra)])

    result = de.compute_delta(root, scan)
    assert result.added == [str(extra)]
    assert result.modified == []
    assert result.unchanged_count == 5


# ── (d) deletion ─────────────────────────────────────────────────────────


def test_removed_file_is_detected_as_deleted(state_file, repo):
    root, files = repo
    de.update_state_after_ingestion(files, _counts(files))

    gone = files[1]
    remaining = [f for f in files if f != gone]

    result = de.compute_delta(root, remaining)
    assert result.deleted == [gone]
    assert result.unchanged_count == 4


def test_remove_from_state_drops_entries(state_file, repo):
    _root, files = repo
    de.update_state_after_ingestion(files, _counts(files))

    de.remove_from_state([files[0], files[1]])

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert sorted(saved.keys()) == files[2:]


# ── (e) corrupt state falls back ─────────────────────────────────────────


def test_corrupt_state_triggers_fallback(state_file, repo):
    root, files = repo
    state_file.write_text("{not valid json", encoding="utf-8")
    result = de.compute_delta(root, files)
    assert result.fallback_required is True


def test_schema_violating_state_triggers_fallback(state_file, repo):
    root, files = repo
    state_file.write_text(json.dumps({"/some/file.py": "not-a-dict"}), encoding="utf-8")
    result = de.compute_delta(root, files)
    assert result.fallback_required is True
    assert "schema" in result.fallback_reason.lower()


# ── merge semantics (the incremental path depends on these) ──────────────


def test_merge_preserves_untouched_entries(state_file, repo):
    """B-4: overwriting on a partial run would make the next run see every
    skipped file as 'added', i.e. a permanent full re-index loop."""
    root, files = repo
    de.update_state_after_ingestion(files, _counts(files))

    # Re-record only one file, as the incremental path does.
    de.update_state_after_ingestion([files[0]], {files[0]: 9}, merge=True)

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert sorted(saved.keys()) == files, "merge dropped the untouched files"
    assert saved[files[0]]["chunk_count"] == "9"

    assert de.is_noop(de.compute_delta(root, files)) is True


def test_no_merge_replaces_the_whole_state(state_file, repo):
    _root, files = repo
    de.update_state_after_ingestion(files, _counts(files))
    de.update_state_after_ingestion([files[0]], {files[0]: 1}, merge=False)

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert list(saved.keys()) == [files[0]]
