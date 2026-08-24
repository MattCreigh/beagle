"""Post-registration hardening hooks for Beagle runtime components."""

from beagle.hardening.mcp_schema_hardener import harden_mcp_tool_schemas

__all__ = ["harden_mcp_tool_schemas"]
