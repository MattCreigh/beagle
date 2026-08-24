"""Tests for beagle.security.validation.validate_cypher_identifier.

Verifies the allowlist + pattern gate that guards every identifier
interpolated into a Kùzu Cypher query. doctrine-floored: a valid
identifier must pass, and every injection-attempt class must raise.
"""

from __future__ import annotations

import pytest

from beagle.security.validation import validate_cypher_identifier


def test_valid_identifier_returns_unchanged() -> None:
    """A plain identifier passes through unchanged."""
    assert validate_cypher_identifier("CALLS") == "CALLS"


def test_valid_tenant_table_name() -> None:
    """A tenant-scoped table name with underscore passes."""
    assert validate_cypher_identifier("ASTNode_tenant_acme") == "ASTNode_tenant_acme"


def test_valid_underscore_relation() -> None:
    """An underscore-separated relation type passes."""
    assert validate_cypher_identifier("INHERITS_FROM") == "INHERITS_FROM"


def test_empty_rejected() -> None:
    """An empty identifier is rejected."""
    with pytest.raises(ValueError, match="empty"):
        validate_cypher_identifier("")


def test_semicolon_injection_rejected() -> None:
    """A semicolon-terminated injection string is rejected by the pattern."""
    with pytest.raises(ValueError, match="forbidden characters"):
        validate_cypher_identifier("CALLS; DROP MATCH ()")


def test_reserved_keyword_rejected() -> None:
    """A reserved Cypher keyword is rejected regardless of case."""
    for kw in ("DROP", "DELETE", "merge", "Create", "UNWIND", "SLEEP"):
        with pytest.raises(ValueError, match="reserved Cypher keyword"):
            validate_cypher_identifier(kw)


def test_space_rejected() -> None:
    """Whitespace that would split the identifier is rejected."""
    with pytest.raises(ValueError, match="forbidden characters"):
        validate_cypher_identifier("CALLS FROM")


def test_leading_digit_rejected() -> None:
    """An identifier leading with a digit is rejected by the pattern."""
    with pytest.raises(ValueError, match="forbidden characters"):
        validate_cypher_identifier("1CALLS")
