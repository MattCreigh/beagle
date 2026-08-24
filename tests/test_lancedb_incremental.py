"""B-3 regression locks — incremental index writes must not truncate.

Audit v13.22.1: `build_lancedb_index()` dropped the `ast_code_chunks`
table and recreated it from whatever chunk list it was handed. Two callers
passed a *subset*:

  - `hotswap_ingest._incremental_update` (only the delta's chunks), and
  - `ingest()`'s incremental-skip path (which omits unchanged files).

Either one would have reduced the whole vector index to just the files that
happened to change. These tests pin the split between the destructive
rebuild and the in-place upsert, and prove a modification replaces rather
than duplicates.

No network: the embedder is stubbed with a deterministic hash embedding.
"""

from __future__ import annotations

import pytest

from beagle.infrastructure import cast_ingestion as ci

lancedb = pytest.importorskip("lancedb", reason="lancedb not installed")

VEC_DIM = 8


class _StubEmbedder:
    """Deterministic, dependency-free stand-in for the real embedder."""

    provider = "stub"

    def identity(self):
        return {"provider": "stub", "model": "stub-v1", "prefix": "search_query: "}

    def encode(self, texts, **kwargs):
        out = []
        for t in texts:
            h = abs(hash(t))
            out.append([((h >> (i * 5)) & 0xFF) / 255.0 for i in range(VEC_DIM)])
        return out


@pytest.fixture
def lance_env(tmp_path, monkeypatch):
    """Point cast_ingestion at a throwaway LanceDB directory.

    Uses setenv so the __getattr__-backed LANCEDB_URI resolves dynamically
    (setattr on __getattr__-backed attributes creates a __dict__ entry that
    monkeypatch cannot properly undo, leaking into subsequent tests).
    """
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ci, "_resolve_embedder", lambda: _StubEmbedder())
    # The actual URI that ci.lancedb_uri() computes from BEAGLE_KNOWLEDGE_DIR
    return str(tmp_path / "lancedb")


def _chunk(path: str, name: str, idx: int = 0) -> ci.ASTChunk:
    return ci.ASTChunk(
        ast_entity_id=f"{path}::{name}",
        chunk_id=f"{path}::{name}::{idx}",
        filepath=path,
        language="python",
        node_type="function_definition",
        node_name=name,
        start_line=idx * 10 + 1,
        end_line=idx * 10 + 9,
        text=f"def {name}(): pass  # {path} {idx}",
        token_count=8,
    )


def _rows(uri: str) -> list[dict]:
    db = lancedb.connect(uri)
    return db.open_table(ci.LANCE_TABLE_NAME).search().limit(10_000).to_list()


def _paths(uri: str) -> list[str]:
    return sorted(r["filepath"] for r in _rows(uri))


# ── (a) rebuild is the full-corpus path ──────────────────────────────────


def test_rebuild_creates_all_rows(lance_env):
    chunks = [_chunk(f"/repo/f{i}.py", f"fn{i}") for i in range(10)]
    assert ci.rebuild_lancedb_index(chunks) is True
    assert len(_rows(lance_env)) == 10


def test_rebuild_is_destructive_by_design(lance_env):
    """The dangerous behaviour still exists — it just has an honest name."""
    ci.rebuild_lancedb_index([_chunk(f"/repo/f{i}.py", f"fn{i}") for i in range(10)])
    ci.rebuild_lancedb_index([_chunk("/repo/only.py", "only")])
    assert _paths(lance_env) == ["/repo/only.py"]


def test_build_lancedb_index_alias_still_works(lance_env):
    """Back-compat: the old name maps onto the rebuild semantics."""
    assert ci.build_lancedb_index([_chunk("/repo/a.py", "a")]) is True
    assert len(_rows(lance_env)) == 1


# ── (b) upsert adds without deleting ─────────────────────────────────────


def test_upsert_appends_and_preserves_existing_rows(lance_env):
    """The B-3 proof: adding 2 chunks must not drop the other 10."""
    ci.rebuild_lancedb_index([_chunk(f"/repo/f{i}.py", f"fn{i}") for i in range(10)])
    assert len(_rows(lance_env)) == 10

    new = [_chunk("/repo/new.py", "new_a"), _chunk("/repo/new.py", "new_b", idx=1)]
    assert ci.upsert_lancedb_chunks(new) is True

    rows = _rows(lance_env)
    assert len(rows) == 12, "upsert truncated the index"
    assert "/repo/f0.py" in _paths(lance_env)
    assert _paths(lance_env).count("/repo/new.py") == 2


# ── (c) modification replaces, not duplicates ────────────────────────────


def test_upsert_replaces_rows_for_a_modified_file(lance_env):
    ci.rebuild_lancedb_index(
        [
            _chunk("/repo/a.py", "old_one"),
            _chunk("/repo/a.py", "old_two", idx=1),
            _chunk("/repo/b.py", "b_fn"),
        ]
    )
    assert len(_rows(lance_env)) == 3

    # a.py now has a single function with a different name.
    assert ci.upsert_lancedb_chunks([_chunk("/repo/a.py", "brand_new")]) is True

    rows = _rows(lance_env)
    names = sorted(r["node_name"] for r in rows)
    assert names == ["b_fn", "brand_new"], f"stale rows survived: {names}"
    assert len(rows) == 2


# ── (d) deletion removes exactly that file's rows ────────────────────────


def test_upsert_deletes_rows_for_removed_files(lance_env):
    ci.rebuild_lancedb_index(
        [
            _chunk("/repo/keep.py", "keep_fn"),
            _chunk("/repo/gone.py", "gone_one"),
            _chunk("/repo/gone.py", "gone_two", idx=1),
        ]
    )
    assert len(_rows(lance_env)) == 3

    assert ci.upsert_lancedb_chunks([], deleted_filepaths=["/repo/gone.py"]) is True

    assert _paths(lance_env) == ["/repo/keep.py"]


def test_upsert_handles_simultaneous_add_modify_delete(lance_env):
    ci.rebuild_lancedb_index(
        [
            _chunk("/repo/unchanged.py", "u"),
            _chunk("/repo/modified.py", "m_old"),
            _chunk("/repo/deleted.py", "d"),
        ]
    )
    ok = ci.upsert_lancedb_chunks(
        [_chunk("/repo/modified.py", "m_new"), _chunk("/repo/added.py", "a")],
        deleted_filepaths=["/repo/deleted.py"],
    )
    assert ok is True
    assert _paths(lance_env) == ["/repo/added.py", "/repo/modified.py", "/repo/unchanged.py"]
    names = sorted(r["node_name"] for r in _rows(lance_env))
    assert names == ["a", "m_new", "u"]


# ── (e) quoting ──────────────────────────────────────────────────────────


def test_quote_in_filepath_does_not_break_the_delete_predicate(lance_env):
    """An unescaped quote would truncate the SQL and delete the wrong rows."""
    odd = "/repo/it's a file.py"
    ci.rebuild_lancedb_index([_chunk(odd, "odd_fn"), _chunk("/repo/safe.py", "safe_fn")])
    assert len(_rows(lance_env)) == 2

    assert ci.upsert_lancedb_chunks([], deleted_filepaths=[odd]) is True
    assert _paths(lance_env) == ["/repo/safe.py"]


def test_sql_quote_list_escapes_single_quotes():
    assert ci._sql_quote_list(["a'b"]) == "'a''b'"
    assert ci._sql_quote_list(["x", "y"]) == "'x', 'y'"


# ── (f) first-run degradation ────────────────────────────────────────────


def test_upsert_creates_the_table_when_missing(lance_env):
    """A first incremental run must not fail just because there's no table."""
    assert ci.upsert_lancedb_chunks([_chunk("/repo/first.py", "f")]) is True
    assert _paths(lance_env) == ["/repo/first.py"]


def test_upsert_noop_without_table_or_chunks_is_success(lance_env):
    assert ci.upsert_lancedb_chunks([], deleted_filepaths=[]) is True
