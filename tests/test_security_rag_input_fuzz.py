"""Fuzz tests for the RAG search input validator.

Generates random adversarial strings and feeds them to
``_validate_search_input``. The contract is:

  - The function must NEVER raise an unhandled exception.
  - Empty / whitespace-only input must raise ValueError.
  - Cypher-like input must raise ValueError.
  - All other input must return a sanitized (query, max_hops, top_k)
    tuple without raising.
  - The returned query must have no control characters and must be
    at most 50_000 characters.

If a fuzz case produces a crash, the validator is brittle and must be
hardened.
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
settings = hypothesis.settings
HealthCheck = hypothesis.HealthCheck
st = hypothesis.strategies

from beagle.infrastructure.mcp_rag_server import (  # ruff: ignore[E402]
    _validate_search_input,
)

# A strategy that produces adversarial strings: long inputs, control
# chars, cypher keywords, mixed case, unicode.
_adversarial = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # exclude surrogates
        blacklist_characters="\r\n\t",  # exclude whitespace that would make it "empty" so we can test other things
    ),
    min_size=1,
    max_size=200,
)


@given(text=_adversarial)
@settings(
    max_examples=200,
    deadline=5_000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_validator_never_crashes_on_random_input(text):
    """Random text input must not crash the validator.

    It may raise ValueError (for cypher injection or empty-after-strip),
    but it must not raise any other exception type and must not hang.
    """
    try:
        sanitized, max_hops, top_k = _validate_search_input(text, 2, 5)
    except ValueError:
        # Expected for cypher-like input
        return
    # If it succeeded, validate the contract
    assert isinstance(sanitized, str)
    assert isinstance(max_hops, int)
    assert isinstance(top_k, int)
    assert 1 <= max_hops <= 3
    assert 1 <= top_k <= 100
    assert len(sanitized) <= 50_000
    # No control characters (except \t, \n, \r which are preserved)
    for ch in sanitized:
        if ch in "\t\n\r":
            continue
        assert ord(ch) >= 0x20, f"control char {ch!r} in output"


@given(length=st.integers(min_value=1, max_value=100_000))
@settings(
    max_examples=30,
    deadline=10_000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_long_input_never_crashes(length):
    """Very long inputs are truncated, not crashed on.

    Note: ``length=0`` is a separate case (empty query) handled by
    ``TestEmptyAndWhitespace`` in ``test_security_rag_input_validation``.
    """
    text = "x" * length
    sanitized, _, _ = _validate_search_input(text, 1, 5)
    assert len(sanitized) <= 50_000


# ── Known crash candidates — pre-recorded adversarial inputs ──────────────


@pytest.mark.parametrize(
    "text",
    [
        "MATCH(n) RETURN n",  # cypher
        "MATCH ( n )",  # cypher with spaces (gets removed, becomes MATCH(n))
        "CYPHER: MATCH (n) DETACH",  # comment-prefixed
        "CRAETE(",  # typo of CREATE
        "\x00\x00\x00",  # null bytes
        "A" * 100 + "\x00" + "B" * 100,  # embedded null
        "MATCH" + "(" * 100,  # parenthesis flood
        "MERGE( " + "A" * 1000 + " )",  # long cypher
        # Unicode tricks
        "M\u0041TCH(n)",  # 'A' is \u0041, fullwidth parens
        "\u0001\u0002\u0003MATCH(n)",  # control char prefix
    ],
)
def test_known_adversarial_inputs(text):
    """Specific adversarial inputs from the threat model must not crash."""
    try:
        _validate_search_input(text, 1, 5)
    except ValueError:
        # Acceptable — rejection is the right answer for some of these
        return
