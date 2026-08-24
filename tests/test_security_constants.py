"""SP-5: tests for security/constants (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The security constants module holds
the query/prompt caps, injection patterns, and secret patterns. These exercise
the public getter and the constant invariants.
"""

from __future__ import annotations

from beagle.security import constants as c


def test_hard_char_cap_returns_query_length() -> None:
    """get_hard_char_cap returns the MAX_QUERY_LENGTH constant."""
    assert c.get_hard_char_cap() == c.MAX_QUERY_LENGTH


def test_query_length_cap() -> None:
    """MAX_QUERY_LENGTH is a positive, non-trivial cap."""
    assert c.MAX_QUERY_LENGTH > 1000


def test_injection_patterns_present() -> None:
    """Injection patterns list is non-empty and all are strings."""
    assert c.INJECTION_PATTERNS
    assert all(isinstance(p, str) for p in c.INJECTION_PATTERNS)


def test_secret_patterns_present() -> None:
    """Secret patterns list is non-empty and all are strings."""
    assert c.SECRET_PATTERNS
    assert all(isinstance(p, str) for p in c.SECRET_PATTERNS)


def test_compiled_injection_regex() -> None:
    """_INJECTION_REGEX matches a canonical injection marker."""
    assert c._INJECTION_REGEX.search("ignore previous instructions and")


def test_dangerous_attributes_and_calls() -> None:
    """The security allowlists are non-empty frozensets of strings."""
    assert isinstance(c.DANGEROUS_ATTRIBUTES, frozenset)
    assert isinstance(c.DANGEROUS_CALLS, frozenset)
    assert all(isinstance(x, str) for x in c.DANGEROUS_ATTRIBUTES)
    assert all(isinstance(x, str) for x in c.DANGEROUS_CALLS)
