"""B-3 regression lock — Kùzu nodes must be removable per file.

`build_kuzu_graph` MERGEs nodes by id, which is idempotent for unchanged
code but leaves orphans when a function is renamed, moved or deleted: the
old id is simply never revisited. An incremental update therefore has to
drop a file's nodes before re-inserting them, or the graph accumulates
entities that no longer exist in the codebase and rag_search cites them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beagle.infrastructure import cast_ingestion as ci

kuzu = pytest.importorskip("kuzu", reason="kuzu not installed")


@pytest.fixture
def kuzu_env(tmp_path, monkeypatch):
    """Point cast_ingestion at a throwaway Kùzu database.

    Uses setenv so the __getattr__-backed KUZU_URI resolves dynamically.
    Returns the path that ci.kuzu_uri() will actually compute.
    """
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))
    return str(tmp_path) + "_kuzu"


def _chunk(path: str, name: str, idx: int = 0) -> ci.ASTChunk:
    return ci.ASTChunk(
        ast_entity_id=f"{path}::{name}",
        chunk_id=f"{path}::{name}::{idx}",
        filepath=path,
        language="python",
        node_type="function_definition",
        node_name=name,
        start_line=1,
        end_line=9,
        text=f"def {name}(): pass",
        token_count=5,
    )


def _node_names(uri: str, filepath: str | None = None) -> list[str]:
    db = kuzu.Database(uri)
    conn = kuzu.Connection(db)
    if filepath is None:
        res = conn.execute("MATCH (n:ASTNode) RETURN n.name ORDER BY n.name")
    else:
        res = conn.execute(
            "MATCH (n:ASTNode) WHERE n.filepath = $fp RETURN n.name ORDER BY n.name",
            parameters={"fp": filepath},
        )
    out = []
    while res.has_next():
        out.append(res.get_next()[0])
    return out


def test_delete_removes_only_the_named_files_nodes(kuzu_env):
    ok = ci.build_kuzu_graph(
        [
            _chunk("/repo/keep.py", "keep_fn"),
            _chunk("/repo/gone.py", "gone_one"),
            _chunk("/repo/gone.py", "gone_two", idx=1),
        ],
        [],
    )
    assert ok is True
    assert _node_names(kuzu_env) == ["gone_one", "gone_two", "keep_fn"]

    assert ci.delete_kuzu_nodes_for_files(["/repo/gone.py"]) is True

    assert _node_names(kuzu_env, "/repo/gone.py") == []
    assert _node_names(kuzu_env) == ["keep_fn"], "wrong file's nodes were removed"


def test_delete_then_reinsert_replaces_a_renamed_function(kuzu_env):
    """Without the delete, both the old and new name would persist."""
    ci.build_kuzu_graph([_chunk("/repo/a.py", "old_name")], [])
    assert _node_names(kuzu_env) == ["old_name"]

    ci.delete_kuzu_nodes_for_files(["/repo/a.py"])
    ci.build_kuzu_graph([_chunk("/repo/a.py", "new_name")], [])

    assert _node_names(kuzu_env) == ["new_name"]


def test_delete_removes_incident_edges(kuzu_env):
    """DETACH DELETE must not leave dangling relations behind."""
    chunks = [_chunk("/repo/caller.py", "caller"), _chunk("/repo/callee.py", "callee")]
    rel = ci.ASTRelation(
        source_id="/repo/caller.py::caller",
        target_id="/repo/callee.py::callee",
        relation_type="CALLS",
        source_name="caller",
        target_name="callee",
    )
    assert ci.build_kuzu_graph(chunks, [rel]) is True

    db = kuzu.Database(kuzu_env)
    conn = kuzu.Connection(db)
    res = conn.execute("MATCH ()-[r:CALLS]->() RETURN COUNT(r)")
    assert res.get_next()[0] == 1

    assert ci.delete_kuzu_nodes_for_files(["/repo/callee.py"]) is True

    db2 = kuzu.Database(kuzu_env)
    conn2 = kuzu.Connection(db2)
    res2 = conn2.execute("MATCH ()-[r:CALLS]->() RETURN COUNT(r)")
    assert res2.get_next()[0] == 0, "edge survived its endpoint's deletion"


def test_delete_is_a_noop_for_empty_input(kuzu_env):
    assert ci.delete_kuzu_nodes_for_files([]) is True
    assert ci.delete_kuzu_nodes_for_files(["", None or ""]) is True


def test_delete_before_any_ingest_is_a_noop(kuzu_env):
    """No database on disk yet — must not raise or create one."""
    assert ci.delete_kuzu_nodes_for_files(["/repo/whatever.py"]) is True


def test_quote_in_filepath_is_parameterised(kuzu_env):
    """Kùzu writes are parameterised, so quoting is not our problem here."""
    odd = "/repo/it's a file.py"
    ci.build_kuzu_graph([_chunk(odd, "odd_fn"), _chunk("/repo/safe.py", "safe_fn")], [])
    assert ci.delete_kuzu_nodes_for_files([odd]) is True
    assert _node_names(kuzu_env) == ["safe_fn"]


# ── v13.22.3 regression: stale WAL from a crashed ingest blocks the
#    next fresh-ingest Database() open with a Kùzu C++ exception
#    (``IndexError: unordered_map::at`` from the WAL-replay path).
#    Fix: _clean_stale_kuzu_wal() removes the orphan WAL iff the main DB
#    file is absent; if the main DB is present the WAL is left for
#    Kùzu to replay normally on the next open.
def test_stale_wal_blocks_fresh_ingest(monkeypatch, tmp_path):
    """Reproduce the 2026-07-27 incident.

    A crashed ingest (``Kùzu graph construction failed`` or a user
    kill -9) leaves a ``<db>.wal`` file alongside the main DB.
    Without cleanup, the next fresh ingest — main DB removed, WAL
    present — raises ``IndexError: unordered_map::at`` from Kùzu's
    WAL replay. The fix detects this state and removes the orphan.
    """
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))
    target_kuzu = tmp_path / "instance_rag_kuzu"
    # Fresh state: no main DB, no WAL.
    assert not target_kuzu.exists()

    # Simulate a crashed session: drop only the WAL.
    wal = Path(str(target_kuzu) + ".wal")
    wal.write_bytes(b"STALE_WAL_FROM_CRASHED_SESSION")
    assert wal.exists()

    # v13.22.3 fix: _clean_stale_kuzu_wal() must remove the orphan.
    ci._clean_stale_kuzu_wal(str(target_kuzu))
    assert not wal.exists(), (
        "stale WAL was not removed — next kuzu.Database() open would "
        "raise 'IndexError: unordered_map::at' from WAL replay"
    )


def test_wal_preserved_when_main_db_present(monkeypatch, tmp_path):
    """When the main DB exists, the WAL belongs to a normal session —
    Kùzu must replay it on the next open; cleaning would discard
    unflushed committed transactions.
    """
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))
    target_kuzu = tmp_path / "instance_rag_kuzu"
    target_kuzu.write_bytes(b"EXISTING_MAIN_DB")
    wal = Path(str(target_kuzu) + ".wal")
    wal.write_bytes(b"UNCOMMITTED_TXN_REPLAY_ME")

    ci._clean_stale_kuzu_wal(str(target_kuzu))

    # Both files preserved — main DB kept, WAL kept for replay.
    assert target_kuzu.exists()
    assert wal.exists(), (
        "WAL was removed even though the main DB is present — this "
        "would discard an unflushed committed transaction"
    )


def test_clean_is_noop_when_no_wal_present(monkeypatch, tmp_path):
    """No WAL, no DB — the helper must be a silent no-op (not crash)."""
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))
    target_kuzu = tmp_path / "instance_rag_kuzu"
    # No main file, no WAL — nothing to clean.
    ci._clean_stale_kuzu_wal(str(target_kuzu))
    # No exception raised; nothing created.
    assert not target_kuzu.exists()
