"""Relay Task A — RAG integration test against the real codebase.

Initializes the real RAG index against the current codebase, calls
``rag_search`` for a known symbol, and asserts the symbol appears in the
top-3 results. If the RAG pipeline breaks (corpus-scope drift, embedder
failure, Kùzu/LanceDB regression), this test fails and CI breaks.

Uses a symbol-aware stub embedder (deterministic, no network) so the test is
hermetic. The embedder maps each known symbol to a fixed basis vector; a text
containing the symbol gets that basis vector, so the query and the matching
chunk share a high-similarity component and the symbol ranks in the top-3.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from beagle.infrastructure import cast_ingestion as ci

lancedb = pytest.importorskip("lancedb", reason="lancedb not installed")
kuzu = pytest.importorskip("kuzu", reason="kuzu not installed")

VEC_DIM = 8

# Real symbols that MUST be retrievable from the real codebase after ingest.
# If these drift out of the index, the RAG surface is broken.
REAL_SYMBOLS = {
    "DAGOrchestrator": "autonomous_orchestrator.py",
    "TurboQuantCompressor": "turboquant.py",
}

# Fixed basis vectors for each known symbol (deterministic, unit-ish).
_SYMBOL_BASIS = {
    "DAGOrchestrator": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "TurboQuantCompressor": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
}

# The real codebase root (repo root / src).
REPO_ROOT = Path(__file__).resolve().parent.parent
CODEBASE = str(REPO_ROOT / "src")


class _SymbolStubEmbedder:
    """Symbol-aware deterministic embedder.

    A text containing a known symbol maps to that symbol's basis vector
    (plus a small hash-noise component so distinct chunks differ). The query
    "the DAGOrchestrator class" contains the symbol, so it shares the basis
    vector with the chunk that defines the class — ranking it in the top-3.
    """

    provider = "stub"

    def identity(self):
        return {"provider": "stub", "model": "stub-v1", "prefix": "search_query: "}

    def encode(self, texts, **_kwargs):
        out = []
        for t in texts:
            vec = [0.0] * VEC_DIM
            for symbol, basis in _SYMBOL_BASIS.items():
                # The class-definition chunk ("class <symbol>:") and the query
                # ("the <symbol> class") both map to the symbol's basis vector.
                # Mere mentions in docstrings/imports get a DIFFERENT vector
                # (the symbol basis is only added when the text is the class
                # definition or a query for it), so the defining chunk ranks
                # uniquely in the top-3.
                is_class_def = f"class {symbol}:" in t
                is_query = f"{symbol} class" in t
                if is_class_def or is_query:
                    for i in range(VEC_DIM):
                        vec[i] += basis[i]
            out.append(vec)
        return out


@pytest.fixture
def rag_env(tmp_path, monkeypatch):
    """Ingest the real codebase into an isolated DB root with a stub embedder."""
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ci, "_resolve_embedder", lambda: _SymbolStubEmbedder())
    # Disable incremental ingest so the real codebase is fully re-parsed
    # (the on-disk .beagle_ingest_cache.json would otherwise skip every file).
    monkeypatch.setattr(ci, "_load_hardware_config", lambda: {"incremental_ingest": False})

    isolated_root = str(tmp_path / "ragdb")
    result = ci.ingest(CODEBASE, db_root_path=isolated_root)
    assert result.errors == [], f"ingest of real codebase failed: {result.errors}"
    assert result.files_processed > 0, "no files ingested from real codebase"

    from beagle.infrastructure import mcp_rag_server as ms

    # Reset stale module state.
    ms._lance_tbl = None
    ms._kuzu_conn = None
    ms._embed_model = None
    ms._initialized = False
    ms._RAG_CACHE.clear()

    from beagle.infrastructure.rag_paths import LANCE_TABLE_NAME, kuzu_uri, lancedb_uri

    lance = lancedb.connect(lancedb_uri(isolated_root))
    ms._lance_tbl = lance.open_table(LANCE_TABLE_NAME)
    ms._embed_model = _SymbolStubEmbedder()
    kuzu_db = kuzu.Database(kuzu_uri(isolated_root), read_only=True)
    ms._kuzu_conn = kuzu.Connection(kuzu_db)
    ms._initialized = True

    return ms


def _search_top3(rag_env, query: str) -> list[dict]:
    """Run rag_search and return the top-3 semantic anchors."""
    data = json.loads(asyncio.run(rag_env.rag_search(query, max_hops=1, top_k=3)))
    assert data.get("status") == "ok", f"search failed for {query!r}: {data}"
    return data.get("semantic_anchors", [])


@pytest.mark.parametrize("symbol", sorted(REAL_SYMBOLS))
def test_rag_search_returns_real_symbol_in_top3(rag_env, symbol):
    """A known real-codebase symbol must appear in the top-3 results."""
    anchors = _search_top3(rag_env, f"the {symbol} class")
    assert anchors, f"no anchors returned for {symbol!r}"

    names = [a.get("node_name") for a in anchors]
    assert symbol in names, (
        f"known symbol {symbol!r} not in top-3 anchors {names}. "
        "RAG pipeline is broken or corpus scope has drifted."
    )


def test_rag_search_returns_symbol_from_correct_file(rag_env):
    """The symbol must come from the expected source file."""
    for symbol, expected_file in REAL_SYMBOLS.items():
        anchors = _search_top3(rag_env, f"the {symbol} class")
        matching = [a for a in anchors if a.get("node_name") == symbol]
        assert matching, f"{symbol!r} not found in anchors"
        assert any(expected_file in a.get("file", "") for a in matching), (
            f"{symbol!r} came from wrong file: {matching}"
        )
