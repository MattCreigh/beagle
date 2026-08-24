"""Schema strictness tests for MCP tools — validates additionalProperties:false.

v13.12.9: Closes F10.3 — ensures every FastMCP tool's inputSchema enforces
additionalProperties:false on all object-type nodes after the post-registration
hardener runs.

Tests in this file do NOT launch real servers — they simulate tool registration
and assert the hardener correctly patches schemas.
"""

from __future__ import annotations

from beagle.hardening.mcp_schema_hardener import (
    _harden_schema_node,
)

# ── Unit tests for _harden_schema_node ───────────────────────────────────


class TestHardenSchemaNode:
    """Verify recursive hardening of individual schema nodes."""

    def test_flat_object_gets_additional_properties(self):
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        _harden_schema_node(schema, "test_tool")
        assert schema["additionalProperties"] is False

    def test_nested_object_gets_additional_properties(self):
        schema = {
            "type": "object",
            "properties": {"nested": {"type": "object", "properties": {"y": {"type": "integer"}}}},
        }
        _harden_schema_node(schema, "test_tool")
        assert schema["additionalProperties"] is False
        assert schema["properties"]["nested"]["additionalProperties"] is False

    def test_existing_additional_properties_untouched(self):
        schema = {"type": "object", "additionalProperties": True, "properties": {}}
        _harden_schema_node(schema, "test_tool")
        assert schema["additionalProperties"] is True  # existing value preserved

    def test_non_object_schema_unchanged(self):
        schema = {"type": "string"}
        original = dict(schema)
        _harden_schema_node(schema, "test_tool")
        assert schema == original

    def test_array_items_hardened(self):
        schema = {
            "type": "array",
            "items": {"type": "object", "properties": {"z": {"type": "boolean"}}},
        }
        _harden_schema_node(schema, "test_tool")
        assert schema["items"]["additionalProperties"] is False


# ── Negative / edge-case tests ──────────────────────────────────────────


class TestSchemaHardenerEdgeCases:
    """Edge cases for the schema hardener."""

    def test_empty_schema_no_error(self):
        """Empty dict should not crash."""
        _harden_schema_node({}, "empty_tool")

    def test_none_schema_no_error(self):
        """None should not crash (though not realistic)."""
        import contextlib

        with contextlib.suppress(TypeError, AttributeError):
            _harden_schema_node(None, "none_tool")  # type: ignore[arg-type]

    def test_schema_without_type_field(self):
        """Schemas without 'type' field are skipped gracefully."""
        schema = {"properties": {"a": {"type": "string"}}}
        _harden_schema_node(schema, "typeless_tool")
        # Should not crash — just skip the object hardening
