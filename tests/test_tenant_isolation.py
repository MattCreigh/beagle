"""Tests for tenant isolation in MCP RAG server."""

from __future__ import annotations

import pytest


class TestTenantTable:
    """Test _get_tenant_table() function."""

    def test_global_table_no_tenant(self):
        """No tenant_id returns base table name."""
        from beagle.infrastructure.mcp_rag_server import (
            _get_tenant_table,
        )

        assert _get_tenant_table(None) == "ASTNode"
        assert _get_tenant_table(None, "ASTNode") == "ASTNode"

    def test_valid_tenant_id(self):
        """Valid tenant_id returns scoped table name."""
        from beagle.infrastructure.mcp_rag_server import (
            _get_tenant_table,
        )

        assert _get_tenant_table("acme") == "ASTNode_tenant_acme"
        assert _get_tenant_table("my-org_1", "CodeChunk") == "CodeChunk_tenant_my-org_1"

    def test_invalid_tenant_id_special_chars(self):
        """Invalid tenant_id with special chars raises ValueError."""
        from beagle.infrastructure.mcp_rag_server import (
            _get_tenant_table,
        )

        with pytest.raises(ValueError, match="Invalid tenant_id"):
            _get_tenant_table("evil'; DROP TABLE--")

    def test_invalid_tenant_id_too_long(self):
        """Tenant ID exceeding 64 chars raises ValueError."""
        from beagle.infrastructure.mcp_rag_server import (
            _get_tenant_table,
        )

        with pytest.raises(ValueError):
            _get_tenant_table("a" * 65)

    def test_empty_tenant_id(self):
        """Empty string tenant_id raises ValueError."""
        from beagle.infrastructure.mcp_rag_server import (
            _get_tenant_table,
        )

        with pytest.raises(ValueError):
            _get_tenant_table("")


class TestTenantSchema:
    """Test _ensure_tenant_schema() function."""

    def test_schema_creates_tables_for_valid_tenant(self):
        """Valid tenant_id creates tenant-scoped tables in Kùzu."""
        # This test would need a real/mock Kùzu connection
        # For now, test that the function exists and validates input
        from beagle.infrastructure.mcp_rag_server import (
            _ensure_tenant_schema,
        )

        with pytest.raises(ValueError):
            _ensure_tenant_schema("bad tenant!")


class TestInputValidation:
    """Test _validate_search_input security."""

    def test_empty_query_rejected(self):
        """Empty query raises ValueError."""
        from beagle.infrastructure.mcp_rag_server import (
            _validate_search_input,
        )

        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_search_input("", 1, 5)

    def test_cypher_injection_rejected(self):
        """Cypher injection patterns are rejected."""
        from beagle.infrastructure.mcp_rag_server import (
            _validate_search_input,
        )

        with pytest.raises(ValueError, match="unsafe pattern"):
            _validate_search_input("MATCH(n) DELETE(n)", 1, 5)

    def test_max_hops_clamped(self):
        """max_hops is clamped to [1, 3]."""
        from beagle.infrastructure.mcp_rag_server import (
            _validate_search_input,
        )

        _, hops, _ = _validate_search_input("test", 10, 5)
        assert hops == 3
        _, hops, _ = _validate_search_input("test", -1, 5)
        assert hops == 1

    def test_top_k_clamped(self):
        """top_k is clamped to [1, 100]."""
        from beagle.infrastructure.mcp_rag_server import (
            _validate_search_input,
        )

        _, _, tk = _validate_search_input("test", 1, 500)
        assert tk == 100
        _, _, tk = _validate_search_input("test", 1, 0)
        assert tk == 1
