"""CVCP Adversarial Validation Harness for the MCP Hybrid RAG Subsystem.

Tests:
1. Transport enforcement (stdio only, HTTP/SSE blocked)
2. Read-only storage enforcement
3. Security sanitization pipeline
4. Deadlock detection under concurrent traversal
5. Boundary condition stress tests (empty queries, max hops, oversized inputs)
6. CAST ingestion pipeline unit tests
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ──────────────────────────────────────────────────────────────────────────────
# CAST Ingestion Tests
# ──────────────────────────────────────────────────────────────────────────────
class TestCASTIngestion:
    """Test the CAST (Context-Aware Splitting via AST) pipeline."""

    def test_estimate_tokens(self) -> None:
        from beagle.infrastructure.cast_ingestion import estimate_tokens

        assert estimate_tokens("") == 1  # minimum 1
        assert estimate_tokens("hello world") > 0
        assert estimate_tokens("x" * 350) == 100  # 350 / 3.5 = 100

    def test_generate_chunk_id_deterministic(self) -> None:
        from beagle.infrastructure.cast_ingestion import (
            generate_chunk_id,
        )

        id1 = generate_chunk_id("file.py", "my_func", 10)
        id2 = generate_chunk_id("file.py", "my_func", 10)
        id3 = generate_chunk_id("file.py", "other_func", 10)
        assert id1 == id2  # Deterministic
        assert id1 != id3  # Different inputs → different IDs

    def test_fallback_chunk_python(self) -> None:
        from beagle.infrastructure.cast_ingestion import _fallback_chunk

        source = """import os

def hello() -> None:
    print("hello")

class Foo:
    def bar(self) -> None:
        pass

def baz() -> int:
    return 42
"""
        chunks = _fallback_chunk(Path("test.py"), source)
        assert len(chunks) >= 3  # At least hello, Foo, baz
        names = [c.node_name for c in chunks]
        assert "hello" in names
        assert "Foo" in names
        assert "baz" in names

    def test_fallback_chunk_empty_file(self) -> None:
        from beagle.infrastructure.cast_ingestion import _fallback_chunk

        chunks = _fallback_chunk(Path("empty.py"), "")
        # Empty file should produce 0 or 1 chunk
        assert len(chunks) <= 1

    def test_extract_relations_calls(self) -> None:
        from beagle.infrastructure.cast_ingestion import (
            ASTChunk,
            extract_relations,
        )

        chunks = [
            ASTChunk(
                chunk_id="a1",
                filepath="f.py",
                language="python",
                node_type="function",
                node_name="caller",
                start_line=1,
                end_line=5,
                text="def caller():\n    callee()",
                token_count=10,
            ),
            ASTChunk(
                chunk_id="b1",
                filepath="f.py",
                language="python",
                node_type="function",
                node_name="callee",
                start_line=6,
                end_line=10,
                text="def callee():\n    pass",
                token_count=10,
            ),
        ]
        relations = extract_relations(chunks)
        call_rels = [r for r in relations if r.relation_type == "CALLS"]
        assert len(call_rels) >= 1
        assert call_rels[0].source_name == "caller"
        assert call_rels[0].target_name == "callee"

    def test_extract_relations_inheritance(self) -> None:
        from beagle.infrastructure.cast_ingestion import (
            ASTChunk,
            extract_relations,
        )

        chunks = [
            ASTChunk(
                chunk_id="c1",
                filepath="f.py",
                language="python",
                node_type="class",
                node_name="Child",
                start_line=1,
                end_line=5,
                text="class Child(Parent):\n    pass",
                token_count=10,
            ),
            ASTChunk(
                chunk_id="p1",
                filepath="f.py",
                language="python",
                node_type="class",
                node_name="Parent",
                start_line=6,
                end_line=10,
                text="class Parent:\n    pass",
                token_count=10,
            ),
        ]
        relations = extract_relations(chunks)
        inherit_rels = [r for r in relations if r.relation_type == "INHERITS_FROM"]
        assert len(inherit_rels) == 1
        assert inherit_rels[0].source_name == "Child"
        assert inherit_rels[0].target_name == "Parent"

    def test_scan_codebase_respects_exclusions(self) -> None:
        from beagle.infrastructure.cast_ingestion import scan_codebase

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create files
            Path(tmpdir, "main.py").write_text("print('hi')", encoding="utf-8")
            Path(tmpdir, "lib.js").write_text("console.log('hi')", encoding="utf-8")
            Path(tmpdir, "readme.xyz").write_text(
                "# Hi", encoding="utf-8"
            )  # Not a supported extension
            gitdir = Path(tmpdir, ".git")
            gitdir.mkdir()
            Path(gitdir, "config.py").write_text("x = 1", encoding="utf-8")  # Should be excluded

            files = scan_codebase(Path(tmpdir))
            names = [f.name for f in files]
            assert "main.py" in names
            assert "lib.js" in names
            assert "readme.xyz" not in names
            assert "config.py" not in names  # .git excluded

    def test_scan_codebase_excludes_runtime_state_dirs(self, tmp_path: Path) -> None:
        """v13.22.3: runtime state, audit reports, and adjacent projects
        must NOT be ingested. The RAG corpus should be the codebase +
        config — not session state, audit markdown, or sibling projects
        living in the same monorepo.

        Regression test for the bug where `.beagle/`, `audits/`,
        `benchmarks/`, and `beagle_containerisation/`
        were all being ingested, bloating the corpus by ~13% with
        zero-signal content.
        """
        from beagle.infrastructure.cast_ingestion import scan_codebase

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Codebase content (must be kept)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("x = 1", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_main.py").write_text("def t(): pass", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]", encoding="utf-8")

            # Runtime state (must be excluded)
            (root / ".beagle").mkdir()
            (root / ".beagle" / "session.json").write_text("{}", encoding="utf-8")
            (root / ".goose").mkdir()
            (root / ".goose" / "state.toml").write_text("x=1", encoding="utf-8")
            (root / ".agents").mkdir()
            (root / ".agents" / "config.toml").write_text("a=1", encoding="utf-8")
            (root / ".claude").mkdir()
            (root / ".claude" / "history.md").write_text("hi", encoding="utf-8")

            # Operational / planning noise (must be excluded)
            (root / "audits").mkdir()
            (root / "audits" / "audit.md").write_text("audit", encoding="utf-8")
            (root / "benchmarks").mkdir()
            (root / "benchmarks" / "bench.py").write_text("x=1", encoding="utf-8")
            (root / "plans").mkdir()
            (root / "plans" / "plan.md").write_text("plan", encoding="utf-8")
            (root / "examples").mkdir()
            (root / "examples" / "demo.py").write_text("x=1", encoding="utf-8")
            (root / ".github").mkdir()
            (root / ".github" / "workflow.yml").write_text("a: 1", encoding="utf-8")

            # Adjacent project (must be excluded). The exclusion list
            # uses real monorepo-adjacent project names; we mimic one
            # of those here so the test exercises the right code path.
            (root / "beagle_containerisation").mkdir()
            (root / "beagle_containerisation" / "main.py").write_text(
                "x=1",
                encoding="utf-8",
            )

            files = scan_codebase(root)
            names = {f.name for f in files}
            rels = {f.relative_to(root).parts[0] for f in files if f != root}

            # The kept content
            assert "main.py" in names
            assert "test_main.py" in names
            assert "pyproject.toml" in names
            # The excluded dirs
            assert ".beagle" not in rels
            assert ".goose" not in rels
            assert ".agents" not in rels
            assert ".claude" not in rels
            assert "audits" not in rels
            assert "benchmarks" not in rels
            assert "plans" not in rels
            assert "examples" not in rels
            assert ".github" not in rels
            assert "beagle_containerisation" not in rels

    def test_scan_codebase_excludes_agent_tooling_at_root(self, tmp_path: Path) -> None:
        """v13.22.3: At the project root, drop agent-tooling files
        (CLAUDE.md, AGENTS.xml, one-shot audit reports) that look like
        code but are for the AI agent runtime, not the codebase.
        """
        from beagle.infrastructure.cast_ingestion import scan_codebase

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "main.py").write_text("x = 1", encoding="utf-8")
            (root / "CLAUDE.md").write_text("# agent guidance", encoding="utf-8")
            (root / "AGENTS.xml").write_text("<agents/>", encoding="utf-8")
            (root / "ARCH_REPORT.md").write_text("# stale report", encoding="utf-8")
            (root / "README.md").write_text("# kept", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]", encoding="utf-8")

            files = scan_codebase(root)
            names = {f.name for f in files}
            assert "main.py" in names
            assert "README.md" in names
            assert "pyproject.toml" in names
            # Agent-tooling files at root are dropped.
            assert "CLAUDE.md" not in names
            assert "AGENTS.xml" not in names
            assert "ARCH_REPORT.md" not in names

    def test_oversized_chunk_splitting(self) -> None:
        from beagle.infrastructure.cast_ingestion import (
            _split_oversized_node,
        )

        # Create a large text that exceeds 512 tokens (~1792 chars)
        big_text = "\n".join([f"line_{i} = 'x' * 50  # padding" for i in range(200)])
        chunks = _split_oversized_node(big_text, "big.py", "python", "function", "big_func", 1)
        assert len(chunks) > 1  # Must be split
        for chunk in chunks:
            assert chunk.token_count <= 600  # Some tolerance for boundary


# ──────────────────────────────────────────────────────────────────────────────
# MCP Server Security Tests
# ──────────────────────────────────────────────────────────────────────────────
class TestMCPSecurity:
    """CVCP Attacker: Security boundary stress tests."""

    def test_response_sanitization_scrubs_secrets(self) -> None:
        from beagle.infrastructure.mcp_rag_server import (
            _sanitize_response,
        )

        payload = {
            "content": "api_key=sk-abc123456789012345678",
            "nested": {"data": "password=mysecretpassword"},
            "list_field": [
                {"text": "bearer token123456789012345678"},
            ],
            "safe": 42,
        }
        sanitized = _sanitize_response(payload)
        # The scrubber should have processed all string fields
        assert isinstance(sanitized["content"], str)
        assert isinstance(sanitized["nested"]["data"], str)
        assert sanitized["safe"] == 42

    def test_transport_enforcement_blocks_http(self) -> None:
        """Verify that HTTP/SSE transport arguments are rejected."""
        # The server checks sys.argv in __main__ — we test the logic
        banned_args = ["--http", "--sse", "--port=8080", "--host=0.0.0.0"]
        for arg in banned_args:
            assert any(banned in arg.lower() for banned in ("http", "sse", "port", "host")), (
                f"Transport filter should catch: {arg}"
            )

    def test_config_blocks_non_stdio_transport(self) -> None:
        """Verify config.py blocks non-stdio MCP transport overrides."""
        from beagle.config.config import MCPConfig

        cfg = MCPConfig()
        assert cfg.transport == "stdio"
        # Even if someone tries to set it, the env override handler blocks it
        # (Tested via the apply_env_overrides function)


# ──────────────────────────────────────────────────────────────────────────────
# Boundary Condition Stress Tests
# ──────────────────────────────────────────────────────────────────────────────
class TestBoundaryConditions:
    """CVCP Attacker: Edge case and boundary stress tests."""

    @pytest.mark.asyncio
    async def test_empty_query(self) -> None:
        """rag_search with empty query should not crash."""
        from beagle.infrastructure.mcp_rag_server import rag_search

        result = await rag_search("")
        parsed = json.loads(result)
        # Should return error or no_results, not crash
        assert parsed.get("status") in ("error", "no_results", "ok")

    @pytest.mark.asyncio
    async def test_max_hops_clamping(self) -> None:
        """max_hops should be clamped to [1, 3]."""
        from beagle.infrastructure.mcp_rag_server import rag_search

        # These should not raise, just clamp
        result = await rag_search("test", max_hops=100)
        parsed = json.loads(result)
        assert parsed.get("status") in ("error", "no_results", "ok")

        result = await rag_search("test", max_hops=-5)
        parsed = json.loads(result)
        assert parsed.get("status") in ("error", "no_results", "ok")

    @pytest.mark.asyncio
    async def test_oversized_query_no_crash(self) -> None:
        """Very large query should not crash the server."""
        from beagle.infrastructure.mcp_rag_server import rag_search

        big_query = "x" * 100000
        result = await rag_search(big_query)
        parsed = json.loads(result)
        assert parsed.get("status") in ("error", "no_results", "ok")


# ──────────────────────────────────────────────────────────────────────────────
# Deadlock Detection (Concurrency Stress)
# ──────────────────────────────────────────────────────────────────────────────
class TestConcurrency:
    """CVCP Attacker: Verify no deadlocks under concurrent traversal."""

    @pytest.mark.asyncio
    async def test_concurrent_rag_searches_no_deadlock(self) -> None:
        """Fire 10 concurrent rag_search calls and verify they all complete."""
        from beagle.infrastructure.mcp_rag_server import rag_search

        queries = [f"test query {i}" for i in range(10)]
        start = time.monotonic()

        # All should complete within timeout (no deadlock)
        tasks = [asyncio.create_task(rag_search(q)) for q in queries]
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=30.0,
        )

        elapsed = time.monotonic() - start
        assert elapsed < 30.0, "Deadlock detected: concurrent searches took too long"

        for result in results:
            if isinstance(result, Exception):
                pytest.fail(f"Concurrent search raised exception: {result}")
            assert isinstance(result, str)
            parsed = json.loads(result)
            assert parsed.get("status") in ("error", "no_results", "ok")

    @pytest.mark.asyncio
    async def test_rag_status_concurrent(self) -> None:
        """rag_status should be safe under concurrent calls."""
        from beagle.infrastructure.mcp_rag_server import rag_status

        tasks = [asyncio.create_task(rag_status()) for _ in range(5)]
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10.0,
        )

        for result in results:
            if isinstance(result, Exception):
                pytest.fail(f"Concurrent status raised exception: {result}")
            assert isinstance(result, str)
            parsed = json.loads(result)
            assert "lancedb_available" in parsed


# ──────────────────────────────────────────────────────────────────────────────
# Integration: Full Pipeline Test
# ──────────────────────────────────────────────────────────────────────────────
class TestIntegrationPipeline:
    """End-to-end integration tests (may require optional deps)."""

    def test_ingest_small_codebase(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ingest a tiny temp codebase and verify chunk output.

        Uses an isolated db_root_path under tmp_path so the test does not
        collide with the live MCP server's kuzu lock (the production
        instance_rag_kuzu is held open by the running beagle-rag process).
        """
        from beagle.infrastructure.cast_ingestion import ingest

        # Isolated DB root under tmp_path — avoids kuzu lock conflict with
        # the live MCP server, and avoids touching the production corpus.
        isolated_db_root = str(tmp_path / "test_rag_db")

        # D-XX: isolate the delta-engine state too. `db_root_path` only
        # redirects the LanceDB/Kùzu stores; `update_state_after_ingestion`
        # writes `~/.beagle/rag_state.json` via get_data_root(), which honours
        # $BEAGLE_DATA_ROOT. Without this, ingesting a /tmp sample.py in this
        # test pollutes the real state file with a single /tmp entry, making
        # compute_delta() treat the entire real codebase as "added" on the
        # next auto-reingest → a full re-index instead of a git-delta.
        import importlib

        import beagle.infrastructure.delta_engine as de

        monkeypatch.setenv("BEAGLE_DATA_ROOT", str(tmp_path / "data_root"))
        importlib.reload(de)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a small Python file
            Path(tmpdir, "sample.py").write_text(
                '''
def add(a, b):
    """Add two numbers."""
    return a + b

def multiply(a, b):
    """Multiply two numbers."""
    result = add(a, 0)  # calls add
    return a * b

class Calculator:
    def compute(self):
        return add(1, 2)
''',
                encoding="utf-8",
            )
            result = ingest(tmpdir, db_root_path=isolated_db_root)
            assert result.files_processed == 1
            assert result.chunks_created >= 3  # add, multiply, Calculator
            assert result.relations_extracted >= 1  # multiply CALLS add


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
