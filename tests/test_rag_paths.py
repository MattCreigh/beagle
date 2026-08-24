"""Unit tests for RAG path resolution and B-5/B-6 lock invariants.

Tests:
1. Trailing slash normalization.
2. B-6 Lock: cast_ingestion and mcp_rag_server path parity for instance and main tiers.
3. Symlink preservation.
4. Dynamic call-time env resolution.
5. B-5 Lock: stage_ingest routes writes to staging even if cast_ingestion was imported first.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from beagle.infrastructure import cast_ingestion, mcp_rag_server, rag_paths
from beagle.infrastructure.hotswap_ingest import stage_ingest


def test_trailing_slash_normalization():
    p1 = "/tmp/test_rag_dir"
    p2 = "/tmp/test_rag_dir/"

    with patch.dict(os.environ, {"BEAGLE_KNOWLEDGE_DIR": p1}):
        root1 = rag_paths.db_root()
        lance1 = rag_paths.lancedb_uri()
        kuzu1 = rag_paths.kuzu_uri()

    with patch.dict(os.environ, {"BEAGLE_KNOWLEDGE_DIR": p2}):
        root2 = rag_paths.db_root()
        lance2 = rag_paths.lancedb_uri()
        kuzu2 = rag_paths.kuzu_uri()

    assert root1 == root2 == "/tmp/test_rag_dir"
    assert lance1 == lance2 == "/tmp/test_rag_dir/lancedb"
    assert kuzu1 == kuzu2 == "/tmp/test_rag_dir_kuzu"


def test_b6_lock_path_parity():
    # Instance tier
    with patch.dict(
        os.environ, {"BEAGLE_KNOWLEDGE_DIR": "", "BEAGLE_RAG_TIER": "instance"}, clear=True
    ):
        assert cast_ingestion.DB_PATH == mcp_rag_server.DB_PATH
        assert cast_ingestion.LANCEDB_URI == mcp_rag_server.LANCEDB_URI
        assert cast_ingestion.KUZU_URI == mcp_rag_server.KUZU_URI

    # Main tier
    with patch.dict(
        os.environ, {"BEAGLE_KNOWLEDGE_DIR": "", "BEAGLE_RAG_TIER": "main"}, clear=True
    ):
        assert cast_ingestion.DB_PATH == mcp_rag_server.DB_PATH
        assert cast_ingestion.LANCEDB_URI == mcp_rag_server.LANCEDB_URI
        assert cast_ingestion.KUZU_URI == mcp_rag_server.KUZU_URI


def test_symlink_preservation(tmp_path: Path):
    real_dir = tmp_path / "real_rag"
    real_dir.mkdir()
    symlink_dir = tmp_path / "sym_rag"
    symlink_dir.symlink_to(real_dir)

    with patch.dict(os.environ, {"BEAGLE_KNOWLEDGE_DIR": str(symlink_dir)}):
        resolved_root = rag_paths.db_root()
        assert resolved_root == str(symlink_dir)
        assert os.path.islink(resolved_root)


def test_call_time_resolution():
    with patch.dict(os.environ, {"BEAGLE_KNOWLEDGE_DIR": "/tmp/first_path"}):
        first_lance = rag_paths.lancedb_uri()
        assert first_lance == "/tmp/first_path/lancedb"

    with patch.dict(os.environ, {"BEAGLE_KNOWLEDGE_DIR": "/tmp/second_path"}):
        second_lance = rag_paths.lancedb_uri()
        assert second_lance == "/tmp/second_path/lancedb"


def test_b5_lock_stage_ingest_routing(tmp_path: Path):
    # Import cast_ingestion FIRST
    _ = cast_ingestion.ingest

    codebase_dir = tmp_path / "codebase"
    codebase_dir.mkdir()
    (codebase_dir / "example.py").write_text("def hello(): pass\n")

    live_dir = tmp_path / "live_rag"
    staging_dir_path = tmp_path / "staging_rag"

    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [[0.1, 0.2, 0.3, 0.4]]

    with (
        patch.dict(os.environ, {"BEAGLE_KNOWLEDGE_DIR": str(live_dir)}),
        patch(
            "beagle.infrastructure.cast_ingestion._resolve_embedder",
            return_value=mock_embedder,
        ),
    ):
        # v13.22.3: pre-existing typo fix — the kwarg is ``staging_dir``
        # (see hotswap_ingest.stage_ingest signature), not
        # ``staging_dir_path``. The local variable was named
        # ``staging_dir_path`` (verbose), and the test previously
        # called the function with that same name as the kwarg,
        # raising TypeError on every run. The test was written to
        # verify the B-5 lock contract: stage_ingest must route the
        # CAST pipeline to the STAGING root, not the LIVE root, so
        # the live LanceDB must NOT receive the new chunks.
        res = stage_ingest(str(codebase_dir), staging_dir=str(staging_dir_path))

        assert res["status"] == "ok"
        # Assert staging root received files and live root did not
        assert (staging_dir_path / "lancedb").exists()
        assert not (live_dir / "lancedb").exists()
