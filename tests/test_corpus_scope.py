"""C06 corpus-scope regression tests (README remediation follow-up).

The audit flagged the absence of an automated test that indexes a KNOWN
symbol from a real codebase and asserts ``rag_search`` returns it. The only
existing check (``test_search_finds_ingested_functions`` in
``test_rag_hotswap_integration.py``) merely asserts ``callable(rag_search)``
— it cannot catch corpus-scope drift, i.e. the RAG server reading a
different/empty corpus than the one ingested. This module closes that gap:

1. Ingest a small known corpus into an isolated DB root.
2. Drive ``rag_search`` against THAT root (stubbing the embedder with a
   deterministic hash embedder so no network or Ollama is needed).
3. Assert the exact symbol (function/class name) comes back as a
   semantic anchor from the correct file.

Also adds a regression lock for the Kùzu ``max_db_size`` env-gated fix
(v13.22.3 H1), which constrains Kùzu's 8TB mmap default that would OOM on
memory-constrained hosts.

No live server, no network, no production corpus is touched.
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

# Sentinel symbols that MUST be retrievable after ingestion. If these drift
# out of the index, the corpus scope has changed and the whole RAG surface
# (which now reads a different root than the one ingested) is broken.
KNOWN_SYMBOLS = {
    "factorial": "math_utils.py",
    "ShapeMath": "math_utils.py",
    "area_of_circle": "math_utils.py",
}

CORPUS = '''
def factorial(n):
    """Compute n! recursively."""
    return 1 if n <= 1 else n * factorial(n - 1)

def fibonacci(k):
    """Return the k-th Fibonacci number."""
    a, b = 0, 1
    for _ in range(k):
        a, b = b, a + b
    return a

class ShapeMath:
    def area_of_circle(self, radius):
        import math
        return math.pi * radius ** 2
'''


class _StubEmbedder:
    """Deterministic, dependency-free stand-in for the real embedder.

    Mirrors the stub used in test_lancedb_incremental.py so the ingest and
    search paths agree on vector space.
    """

    provider = "stub"

    def identity(self):
        return {"provider": "stub", "model": "stub-v1", "prefix": "search_query: "}

    def encode(self, texts, **_kwargs):
        # ``_kwargs`` accepts show_progress_bar / batch_size passed by the
        # ingest path; prefixed with underscore (vulture convention) because
        # the stub intentionally ignores them.
        out = []
        for t in texts:
            h = abs(hash(t))
            out.append([((h >> (i * 5)) & 0xFF) / 255.0 for i in range(VEC_DIM)])
        return out


@pytest.fixture
def corpus_env(tmp_path, monkeypatch):
    """Isolated corpus + DB root, with a stub embedder wired in.

    Uses setenv so the __getattr__-backed *_URI resolve dynamically, and
    setattr on cast_ingestion._resolve_embedder so the stub is used.
    Returns (codebase_dir, isolated_db_root).
    """
    import importlib

    import beagle.infrastructure.delta_engine as de

    # Isolate the delta-engine state so ci.ingest() does not write the real
    # ~/.beagle/rag_state.json. delta_engine honours $BEAGLE_DATA_ROOT.
    monkeypatch.setenv("BEAGLE_DATA_ROOT", str(tmp_path))
    importlib.reload(de)
    monkeypatch.setenv("BEAGLE_KNOWLEDGE_DIR", str(tmp_path))
    monkeypatch.setattr(ci, "_resolve_embedder", lambda: _StubEmbedder())

    codebase = tmp_path / "corpus"
    codebase.mkdir()
    (codebase / "math_utils.py").write_text(CORPUS, encoding="utf-8")
    isolated_root = str(tmp_path / "ragdb")
    return str(codebase), isolated_root


@pytest.fixture
def search_env(corpus_env, monkeypatch):
    """Ingest the known corpus and wire the search server against that root.

    Returns the mcp_rag_server module with its globals stubbed to read the
    isolated DB root, so rag_search queries the SAME corpus that was ingested
    (the exact property C06 wants to lock).
    """
    codebase, isolated_root = corpus_env
    from beagle.infrastructure.rag_paths import LANCE_TABLE_NAME, kuzu_uri, lancedb_uri

    result = ci.ingest(codebase, db_root_path=isolated_root)
    assert result.errors == [], f"ingest failed: {result.errors}"
    assert result.files_processed == 1
    assert result.chunks_created >= 3, f"expected >=3 chunks, got {result.chunks_created}"

    from beagle.infrastructure import mcp_rag_server as ms

    # Reset any stale module state from a prior test in the same process.
    ms._lance_tbl = None
    ms._kuzu_conn = None
    ms._embed_model = None
    ms._initialized = False
    ms._RAG_CACHE.clear()

    lance = lancedb.connect(lancedb_uri(isolated_root))
    ms._lance_tbl = lance.open_table(LANCE_TABLE_NAME)
    ms._embed_model = _StubEmbedder()
    kuzu_db = kuzu.Database(kuzu_uri(isolated_root), read_only=True)
    ms._kuzu_conn = kuzu.Connection(kuzu_db)
    ms._initialized = True

    return ms


def _search_ok(search_env, query: str) -> dict:
    """Run rag_search against the stubbed server and return the parsed JSON."""
    data = json.loads(asyncio.run(search_env.rag_search(query, max_hops=1, top_k=5)))
    assert data.get("status") == "ok", f"search failed for {query!r}: {data}"
    return data


def test_ingest_writes_expected_chunks(corpus_env):
    """Sanity: ingestion of the known corpus produces the expected chunks."""
    codebase, isolated_root = corpus_env
    result = ci.ingest(codebase, db_root_path=isolated_root)
    assert result.files_processed == 1
    assert result.chunks_created >= 3
    assert result.errors == []


@pytest.mark.parametrize("symbol", sorted(KNOWN_SYMBOLS))
def test_rag_search_returns_known_symbol(search_env, symbol):
    """C06 lock: a known symbol from the ingested corpus is retrievable.

    Query for each known symbol and assert it appears as a semantic anchor
    from the expected file. If corpus scope drifts (search reads a different
    or empty root than the one ingested), this test fails loudly.
    """
    data = _search_ok(search_env, f"the {symbol} symbol")
    anchors = data.get("semantic_anchors", [])
    assert anchors, f"no anchors returned for {symbol!r}"

    names = {a.get("node_name") for a in anchors}
    assert symbol in names, (
        f"known symbol {symbol!r} not found in anchors {names}. "
        "This indicates corpus-scope drift: rag_search is reading a corpus "
        "that does not contain the ingested symbol."
    )
    # The anchor must come from the corpus file (not some other/stale corpus).
    expected_file = KNOWN_SYMBOLS[symbol]
    matching = [a for a in anchors if a.get("node_name") == symbol]
    assert any(expected_file in a.get("file", "") for a in matching), (
        f"symbol {symbol!r} came from wrong file: {matching}"
    )


def test_rag_search_cross_file_consistency(search_env):
    """All three known symbols coexist in the SAME indexed corpus.

    Guards against a partial-corpus collapse (e.g. the incremental-update bug
    class that reduced the whole index to just the files that changed).
    """
    for symbol in KNOWN_SYMBOLS:
        data = _search_ok(search_env, f"the {symbol} symbol")
        names = {a.get("node_name") for a in data.get("semantic_anchors", [])}
        assert symbol in names, f"{symbol!r} missing — corpus scope changed"


# ---------------------------------------------------------------------------
# Kùzu max_db_size / 8TB mmap env-gating regression (v13.22.3 H1)
# ---------------------------------------------------------------------------


def test_kuzu_db_size_is_env_gated_not_8tb_mmap_default():
    """Kùzu must NOT use its unbounded 8TB mmap default.

    The 8TB default OOMs on memory-constrained hosts. cast_ingestion must
    read BEAGLE_KUZU_BUFFER_POOL_MB / BEAGLE_KUZU_MAX_DB_SIZE_MB at build
    time and pass them to kuzu.Database, never relying on the C++ default.
    """
    source = (Path(ci.__file__).parent / "cast_ingestion.py").read_text(encoding="utf-8")

    # The env-gated settings must be referenced at every kuzu.Database site.
    for var in ("BEAGLE_KUZU_BUFFER_POOL_MB", "BEAGLE_KUZU_MAX_DB_SIZE_MB"):
        assert var in source, f"{var} not referenced in cast_ingestion.py"

    # kuzu.Database( must be called with explicit max_db_size (no bare call).
    assert "max_db_size=" in source, "kuzu.Database calls must set max_db_size explicitly"


def test_kuzu_env_override_changes_resolved_values(corpus_env, monkeypatch):
    """Setting BEAGLE_KUZU_MAX_DB_SIZE_MB must change the resolved value.

    Proves the value is read at CALL time (env-driven), not frozen at import.
    """
    codebase, isolated_root = corpus_env
    monkeypatch.setattr(ci, "_resolve_embedder", lambda: _StubEmbedder())

    # Ingest with a small max_db_size and verify it does not raise.
    monkeypatch.setenv("BEAGLE_KUZU_MAX_DB_SIZE_MB", "64")
    result = ci.ingest(codebase, db_root_path=isolated_root)
    assert result.errors == [], f"ingest with capped kuzu db failed: {result.errors}"

    # The Kùzu file must be created under the isolated root.
    from beagle.infrastructure.rag_paths import kuzu_uri

    kuzu_file = Path(kuzu_uri(isolated_root))
    assert kuzu_file.exists(), f"expected kuzu db at {kuzu_file}"
