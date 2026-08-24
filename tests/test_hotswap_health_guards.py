"""Health-guard tests for hotswap_ingest (2026-08-23 torn-fragment incident).

Pins three contracts:
  1. A torn/unreadable LIVE LanceDB must never be pre-seeded into staging —
     CAST rebuilds from scratch instead of carrying poison forward.
  2. BEAGLE_RAG_FULL_REBUILD=1 forces the same skip even when live is healthy.
  3. A staged Kùzu DB that cannot open / has no ASTNode rows fails
     _staged_kuzu_is_healthy so swap_staged_to_live keeps the live graph.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beagle.infrastructure.hotswap_ingest import (
    _live_lance_is_healthy,
    _seed_staging_from_live,
    _staged_kuzu_is_healthy,
)


@pytest.fixture
def knowledge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point BEAGLE_KNOWLEDGE_DIR at a temp root (rag_paths reads at call time)."""
    root = tmp_path / "knowledge"
    root.mkdir()
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(root))
    return root


def _make_healthy_table(live_root: Path, rows: int = 3) -> Path:
    import lancedb

    lance_dir = live_root / "lancedb"
    tbl_path = lance_dir / "ast_code_chunks.lance"
    data = [
        {"vector": [float(i), 0.5, 0.25], "filepath": f"f{i}.py", "code_content": "x"}
        for i in range(rows)
    ]
    lancedb.connect(str(lance_dir)).create_table("ast_code_chunks", data=data, mode="overwrite")
    return tbl_path


def test_seed_skipped_when_live_table_is_torn(knowledge_root: Path) -> None:
    live_tbl = _make_healthy_table(knowledge_root)
    # Tear it: delete a fragment payload so any scan explodes.
    frag = next((live_tbl / "data").iterdir())
    frag.unlink()

    assert _live_lance_is_healthy(live_tbl) is False

    staging = knowledge_root / "staging"
    staging.mkdir()
    _seed_staging_from_live(staging)

    assert not (staging / "lancedb" / "ast_code_chunks.lance").exists(), (
        "poison was carried forward into staging — the swap would have promoted a broken table"
    )


def test_seed_honors_full_rebuild_env(
    knowledge_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_tbl = _make_healthy_table(knowledge_root)
    assert _live_lance_is_healthy(live_tbl) is True  # sanity: live is fine

    monkeypatch.setenv("BEAGLE_RAG_FULL_REBUILD", "1")
    staging = knowledge_root / "staging"
    staging.mkdir()
    _seed_staging_from_live(staging)

    assert not (staging / "lancedb" / "ast_code_chunks.lance").exists()


def test_seed_copies_healthy_live_table(knowledge_root: Path) -> None:
    _make_healthy_table(knowledge_root, rows=5)
    staging = knowledge_root / "staging"
    staging.mkdir()
    _seed_staging_from_live(staging)

    seeded = staging / "lancedb" / "ast_code_chunks.lance"
    assert seeded.is_dir()

    import lancedb

    copied = lancedb.connect(str(seeded.parent)).open_table("ast_code_chunks")
    assert copied.count_rows() == 5


def test_staged_kuzu_garbage_fails_health(tmp_path: Path) -> None:
    garbage = tmp_path / "staged_kuzu"
    garbage.write_bytes(b"not a database")
    assert _staged_kuzu_is_healthy(garbage) is False


def test_staged_kuzu_empty_graph_fails_health(tmp_path: Path) -> None:
    import kuzu

    p = tmp_path / "empty_kuzu"
    db = kuzu.Database(str(p))
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE ASTNode (id INT64, name STRING, PRIMARY KEY(id))")
    del conn, db
    assert _staged_kuzu_is_healthy(p) is False, (
        "an empty graph must not be promoted over a healthy live one"
    )


def test_staged_kuzu_populated_passes_health(tmp_path: Path) -> None:
    import kuzu

    p = tmp_path / "ok_kuzu"
    db = kuzu.Database(str(p))
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE ASTNode (id INT64, name STRING, PRIMARY KEY(id))")
    conn.execute("CREATE (:ASTNode {id: 1, name: 'mod'})")
    del conn, db
    assert _staged_kuzu_is_healthy(p) is True


def test_missing_live_table_means_first_run_no_seed(knowledge_root: Path) -> None:
    # No lancedb dir at all: first-ever ingest path — must not raise.
    staging = knowledge_root / "staging"
    staging.mkdir()
    _seed_staging_from_live(staging)
    assert not (staging / "lancedb").exists()


def test_tear_signature_documented(tmp_path: Path) -> None:
    """The 2026-08-23 failure mode, pinned as executable documentation."""
    live_tbl = _make_healthy_table(tmp_path)
    deletions_ref = {
        "manifest": "references _deletions/1-3-<uuid>.arrow",
        "torn_when": "file absent but manifest lists it",
    }
    (live_tbl.parent / "tear_note.json").write_text(json.dumps(deletions_ref))
    assert json.loads((live_tbl.parent / "tear_note.json").read_text())["manifest"]
