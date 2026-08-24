"""SP-5: tests for utils/prompt_builder (was zero-coverage).

beagle-spotless-phase2, work package SP-5 (I7: raise zero-coverage modules to
non-zero). This module was extracted from core/nodes.py in SP-7 and had no
direct test coverage. These tests exercise the substitution, metadata, and
unresolved-variable-warning behaviour.
"""

from __future__ import annotations

import logging

import pytest

from beagle.utils.prompt_builder import make_prompt_builder


def test_substitutes_known_state_keys() -> None:
    """Known state keys are substituted into the template."""
    builder = make_prompt_builder("Query: {query} | Plan: {research_plan} | Report: {final_report}")
    out = builder({"query": "audit the auth", "research_plan": "step 1", "final_report": "done"})
    assert "audit the auth" in out
    assert "step 1" in out
    assert "done" in out


def test_metadata_keys_are_injected() -> None:
    """String metadata entries are exposed as substitution tokens."""
    builder = make_prompt_builder("Project: {project_name}")
    out = builder({"query": "x", "metadata": {"project_name": "beagle"}})
    assert "beagle" in out


def test_non_string_metadata_ignored() -> None:
    """Non-string metadata values are not substituted (avoids str() coercion)."""
    builder = make_prompt_builder("Score: {score}")
    out = builder({"query": "x", "metadata": {"score": 5}})
    # Unresolved {score} is stripped.
    assert "Score:" in out
    assert "{score}" not in out


def test_unknown_placeholder_is_stripped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Unknown placeholders are stripped and a WARNING is logged (drift signal)."""
    builder = make_prompt_builder("Hello {query} unknown {typo_token}")
    with caplog.at_level(logging.WARNING, logger="Beagle.utils.prompt_builder"):
        out = builder({"query": "world"})
    assert out == "Hello world unknown"
    assert any("typo_token" in r.message for r in caplog.records)


def test_node_name_in_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The node_name is included in the unresolved-variable warning."""
    builder = make_prompt_builder("{missing}", node_name="planning")
    with caplog.at_level(logging.WARNING, logger="Beagle.utils.prompt_builder"):
        builder({"query": "q"})
    assert any("planning" in r.message for r in caplog.records)


def test_returns_stripped_output() -> None:
    """The rendered prompt is stripped of leading/trailing whitespace."""
    builder = make_prompt_builder("  {query}  ")
    assert builder({"query": "x"}) == "x"
