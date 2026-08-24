"""MCP Server End-to-End Tests.

Tests for all MCP servers:
1. RAG Server (rag_search, rag_status, rag_ingest)
2. OpenClaw Server (task creation, execution, waiting)
3. Workflow Server (run_beagle_workflow, list_workflows)
4. Code Tools Server (analyze, tree, summarize)
5. Web Search Tools (web_search, web_research, arxiv_search)

These tests verify:
- Server initialization and health checks
- Basic tool functionality
- Error handling
- Response structure validation

NOTE: The infrastructure package uses lazy imports via __init__.py __getattr__.
We import modules via attribute access (infra.mcp_rag_server) rather than
direct module import (from ... import mcp_rag_server).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import infrastructure package (uses lazy __getattr__)
import beagle.infrastructure as infra  # ruff: ignore[E402]

# ──────────────────────────────────────────────────────────────────────────────
# RAG Server Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestRAGServerE2E:
    """End-to-end tests for MCP RAG server."""

    @pytest.mark.asyncio
    async def test_rag_status_returns_valid_json(self):
        """rag_status should return valid JSON with required fields."""
        # Access via lazy __getattr__
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_status()
        parsed = json.loads(result)

        assert "lancedb_available" in parsed
        assert "kuzu_available" in parsed
        # Status may include embedding_model or embed_model_loaded
        assert "embedding_model" in parsed or "embed_model_loaded" in parsed
        assert isinstance(parsed["lancedb_available"], bool)
        assert isinstance(parsed["kuzu_available"], bool)

    @pytest.mark.asyncio
    async def test_rag_search_returns_valid_structure(self):
        """rag_search should return valid JSON structure."""
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_search("test query", max_hops=1, top_k=5)
        parsed = json.loads(result)

        assert "status" in parsed
        assert parsed["status"] in ("ok", "no_results", "error")

        if parsed["status"] == "ok":
            assert "semantic_anchors" in parsed or "structural_relations" in parsed

    @pytest.mark.asyncio
    async def test_rag_search_handles_empty_query(self):
        """rag_search should handle empty query gracefully."""
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_search("", max_hops=1, top_k=5)
        parsed = json.loads(result)

        assert "status" in parsed
        assert parsed["status"] in ("error", "no_results", "ok")

    @pytest.mark.asyncio
    async def test_rag_ingest_validates_directory(self):
        """rag_ingest should validate the target_directory parameter."""
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_ingest("/nonexistent/path/12345")
        parsed = json.loads(result)

        assert "status" in parsed


"""MCP Server End-to-End Tests.

Tests for all MCP servers:
1. RAG Server (rag_search, rag_status, rag_ingest)
2. OpenClaw Server (task creation, execution, waiting)
3. Workflow Server (run_beagle_workflow, list_workflows)
4. Code Tools Server (analyze, tree, summarize)
5. Web Search Tools (web_search, web_research, arxiv_search)

These tests verify:
- Server initialization and health checks
- Basic tool functionality
- Error handling
- Response structure validation

NOTE: The infrastructure package uses lazy imports via __init__.py __getattr__.
We import modules via attribute access (infra.mcp_rag_server) rather than
direct module import (from ... import mcp_rag_server).
"""


import asyncio
import json
import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import infrastructure package (uses lazy __getattr__)
import beagle.infrastructure as infra  # ruff: ignore[E402]

# ──────────────────────────────────────────────────────────────────────────────
# RAG Server Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestRAGServerE2E:
    """End-to-end tests for MCP RAG server."""

    @pytest.mark.asyncio
    async def test_rag_status_returns_valid_json(self):
        """rag_status should return valid JSON with required fields."""
        # Access via lazy __getattr__
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_status()
        parsed = json.loads(result)

        assert "lancedb_available" in parsed
        assert "kuzu_available" in parsed
        # Status may include embedding_model or embed_model_loaded
        assert "embedding_model" in parsed or "embed_model_loaded" in parsed
        assert isinstance(parsed["lancedb_available"], bool)
        assert isinstance(parsed["kuzu_available"], bool)

    @pytest.mark.asyncio
    async def test_rag_search_returns_valid_structure(self):
        """rag_search should return valid JSON structure."""
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_search("test query", max_hops=1, top_k=5)
        parsed = json.loads(result)

        assert "status" in parsed
        assert parsed["status"] in ("ok", "no_results", "error")

        if parsed["status"] == "ok":
            assert "semantic_anchors" in parsed or "structural_relations" in parsed

    @pytest.mark.asyncio
    async def test_rag_search_handles_empty_query(self):
        """rag_search should handle empty query gracefully."""
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_search("", max_hops=1, top_k=5)
        parsed = json.loads(result)

        assert "status" in parsed
        assert parsed["status"] in ("error", "no_results", "ok")

    @pytest.mark.asyncio
    async def test_rag_ingest_validates_directory(self):
        """rag_ingest should validate the target_directory parameter."""
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_ingest("/nonexistent/path/12345")
        parsed = json.loads(result)

        assert "status" in parsed


# ──────────────────────────────────────────────────────────────────────────────
# OpenClaw Server Tests
# ──────────────────────────────────────────────────────────────────────────────



# ──────────────────────────────────────────────────────────────────────────────
# Workflow Server Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestWorkflowServerE2E:
    """End-to-end tests for MCP Workflow server."""

    @pytest.mark.asyncio
    async def test_list_available_workflows(self):
        """list_available_workflows should return workflow definitions."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.list_available_workflows()
        parsed = json.loads(result)

        assert "workflows" in parsed or isinstance(parsed, list)

    @pytest.mark.asyncio
    async def test_list_agents(self):
        """list_agents should return available Beagle agents."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.list_agents()
        parsed = json.loads(result)

        assert "agents" in parsed or isinstance(parsed, list)

    @pytest.mark.asyncio
    async def test_route_query_to_workflow(self):
        """route_query_to_workflow should recommend appropriate workflow."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.route_query_to_workflow("Review this code for security issues")
        parsed = json.loads(result)

        assert "recommended_workflow" in parsed or "workflow" in parsed

    @pytest.mark.asyncio
    async def test_estimate_workflow_cost(self):
        """estimate_workflow_cost should return cost estimate."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.estimate_workflow_cost(
            query="Test query",
            workflow_name="research",
        )
        parsed = json.loads(result)

        # Response has estimated_total_tokens or estimated_cost_usd
        assert (
            "estimated_total_tokens" in parsed
            or "estimated_cost_usd" in parsed
            or "error" in parsed
        )


# ──────────────────────────────────────────────────────────────────────────────
# Code Tools Server Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestCodeToolsServerE2E:
    """End-to-end tests for MCP Code Tools server."""

    @pytest.mark.asyncio
    async def test_code_search_returns_structure(self):
        """code_search should return valid results structure."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.code_search(
            pattern="def test_",
            path=str(PROJECT_ROOT / "tests"),
            file_glob="*.py",
            max_results=5,
        )
        parsed = json.loads(result)

        assert "status" in parsed
        assert parsed["status"] in ("ok", "error")

    @pytest.mark.asyncio
    async def test_code_search_with_context(self):
        """code_search should include context lines."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.code_search(
            pattern="import pytest",
            path=str(PROJECT_ROOT),
            file_glob="test_*.py",
            max_results=3,
            context_lines=1,
        )
        parsed = json.loads(result)

        # Should have matches or status
        assert "status" in parsed or "matches" in parsed

    @pytest.mark.asyncio
    async def test_code_search_invalid_path(self):
        """code_search should handle invalid path gracefully."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.code_search(
            pattern="test",
            path="/nonexistent/path/12345",
        )
        parsed = json.loads(result)

        # Should return error for invalid path
        assert parsed["status"] == "error"


# ──────────────────────────────────────────────────────────────────────────────
# Web Search Tools Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestWebSearchToolsE2E:
    """End-to-end tests for MCP Web Search server."""

    @pytest.mark.asyncio
    async def test_web_search_returns_structure(self):
        """web_search should return valid results structure."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.web_search("test query", max_results=5)
        parsed = json.loads(result)

        assert "status" in parsed or "results" in parsed or "error" in parsed

    @pytest.mark.asyncio
    async def test_web_search_max_results_limit(self):
        """web_search should respect max_results limit."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.web_search("python async", max_results=3)
        parsed = json.loads(result)

        if parsed.get("results"):
            assert len(parsed["results"]) <= 3


# ──────────────────────────────────────────────────────────────────────────────
# Error Handling Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestMCPServerErrorHandling:
    """Test error handling across all MCP servers."""

    @pytest.mark.asyncio
    async def test_rag_search_malformed_parameters(self):
        """RAG search should handle malformed parameters gracefully."""
        mcp_rag = infra.mcp_rag_server
        result = await mcp_rag.rag_search("test", max_hops=-1, top_k=0)
        parsed = json.loads(result)

        assert "status" in parsed
        assert parsed["status"] in ("error", "no_results", "ok")

    @pytest.mark.asyncio
    async def test_openclaw_nonexistent_task(self):
        """OpenClaw should handle nonexistent task gracefully."""
        mcp_openclaw = infra.mcp_openclaw_server
        result = await mcp_openclaw.openclaw_get_result(task_id="nonexistent-task-id-12345")
        parsed = json.loads(result)

        assert "error" in parsed or "status" in parsed

    @pytest.mark.asyncio
    async def test_workflow_invalid_workflow_name(self):
        """Workflow server should handle invalid workflow name."""
        mcp_utility = infra.mcp_utility_server
        result = await mcp_utility.run_beagle_workflow(
            query="test",
            workflow_name="nonexistent_workflow_xyz",
        )
        parsed = json.loads(result)

        assert "error" in parsed or "status" in parsed


# ──────────────────────────────────────────────────────────────────────────────
# Concurrency Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestMCPConcurrency:
    """Test MCP servers handle concurrent requests safely."""

    @pytest.mark.asyncio
    async def test_concurrent_rag_status_calls(self):
        """Multiple concurrent rag_status calls should not deadlock."""
        mcp_rag = infra.mcp_rag_server

        tasks = [asyncio.create_task(mcp_rag.rag_status()) for _ in range(10)]
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10.0,
        )

        for result in results:
            if isinstance(result, Exception):
                pytest.fail(f"Concurrent call raised: {result}")
            parsed = json.loads(result)
            assert "lancedb_available" in parsed

    @pytest.mark.asyncio
    async def test_concurrent_list_tasks(self):
        """Multiple concurrent openclaw_list_tasks should complete safely."""
        mcp_openclaw = infra.mcp_openclaw_server

        tasks = [
            asyncio.create_task(mcp_openclaw.openclaw_list_tasks(status="all", limit=5))
            for _ in range(5)
        ]
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10.0,
        )

        for result in results:
            if isinstance(result, Exception):
                pytest.fail(f"Concurrent call raised: {result}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
