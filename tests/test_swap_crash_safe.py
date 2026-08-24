"""B-17 regression test: crash-safe hot-swap with rollback.

The original swap did shutil.move from /tmp to /mnt, which is copy+delete
rather than atomic.  A crash mid-move left the live index empty.  This test
simulates a crash during promotion and verifies that swap_staged_to_live()'s
internal rollback restores the prior live index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.infrastructure import cast_ingestion as ci
from beagle.infrastructure import hotswap_ingest as hi


@pytest.fixture
def stub_embedder(monkeypatch):
    class _Stub:
        provider = "stub"
        dimension = 4

        def identity(self):
            return {"provider": "stub", "model": "stub", "prefix": ""}

        def encode(self, texts, **kw):
            return [[float(len(t) % 7), 1.0, 2.0, 3.0] for t in texts]

    monkeypatch.setattr(ci, "_resolve_embedder", lambda: _Stub())


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


def test_atomic_move_copies_directory_without_removing_source(stub_embedder, tmp_path):
    """Sanity check for the helper used by swap/rollback."""
    src = tmp_path / "src" / "lancedb"
    src.mkdir(parents=True)
    (src / "marker.txt").write_text("old", encoding="utf-8")
    dst = tmp_path / "dst" / "lancedb"

    hi._atomic_move_on_same_fs(src, dst, remove_src=False)

    assert dst.exists()
    assert (dst / "marker.txt").read_text(encoding="utf-8") == "old"
    assert src.exists(), "source must remain intact when remove_src=False"


def test_failed_promotion_triggers_internal_rollback(tmp_path, monkeypatch, stub_embedder):
    """When promotion crashes, swap_staged_to_live()'s except block invokes
    rollback() internally, which restores the old live index from backup."""
    live = tmp_path / "live"
    staging = tmp_path / "staging"
    backup = tmp_path / "backup"
    for d in (live, staging, backup):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(live))
    monkeypatch.setenv("BEAGLE_STAGING_DIR", str(staging))
    monkeypatch.setenv("BEAGLE_RAG_BACKUP_DIR", str(backup))

    # Seed the live index with one file.
    old_file = tmp_path / "repo" / "old.py"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("def old():\n    return 1\n", encoding="utf-8")
    ci.rebuild_lancedb_index([_chunk_for(old_file)], db_root_path=str(live))

    # Create a staging index with a different file.
    new_file = tmp_path / "repo" / "new.py"
    new_file.write_text("def new():\n    return 2\n", encoding="utf-8")
    ci.rebuild_lancedb_index([_chunk_for(new_file)], db_root_path=str(staging))

    # Crash only when promoting the staging LanceDB dir.
    real_atomic = hi._atomic_move_on_same_fs

    def _crash_on_staging_lance(src: Path, dst: Path, **kw) -> None:
        if str(src) == str(staging / "lancedb"):
            raise OSError("simulated crash during staging promotion")
        real_atomic(src, dst, **kw)

    monkeypatch.setattr(hi, "_atomic_move_on_same_fs", _crash_on_staging_lance)

    # Capture the rollback that swap_staged_to_live() calls internally.
    real_rollback = hi.rollback
    rollback_results: list[dict] = []

    def _capture_rollback() -> dict:
        result = real_rollback()
        rollback_results.append(result)
        return result

    monkeypatch.setattr(hi, "rollback", _capture_rollback)

    with pytest.raises(OSError):
        hi.swap_staged_to_live()

    # The swap must have invoked rollback internally.
    assert rollback_results, "swap did not invoke rollback() on failure"
    assert rollback_results[-1]["status"] == "ok"
    assert "lancedb" in rollback_results[-1]["restored"]

    # The live index must still contain the old file.
    db = __import__("lancedb").connect(str(live / "lancedb"))
    rows = db.open_table(ci.LANCE_TABLE_NAME).search().limit(10_000).to_list()
    paths = {r["filepath"] for r in rows}
    assert str(old_file) in paths
