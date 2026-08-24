"""Regression tests for workflow_builder template engine (B3/B5)."""

from __future__ import annotations

from beagle.core.workflow_builder import create_prompt_builder


# A minimal AgentState-like object for prompt builder tests
class _FakeAgentState:
    """Minimal stand-in for AgentState in unit tests."""

    def __init__(
        self,
        query: str = "",
        research_plan: str = "",
        raw_execution_context: str = "",
        verified_facts: str = "",
        final_report: str = "",
        metadata: dict | None = None,
    ) -> None:
        self.query = query
        self.research_plan = research_plan
        self.raw_execution_context = raw_execution_context
        self.verified_facts = verified_facts
        self.final_report = final_report
        self.metadata = metadata or {}

    def __getattr__(self, name: str) -> str:
        if name in ("search_results",):
            return self.raw_execution_context
        return ""


class TestBeagleTemplateOrdering:
    """Verify {{literal}} de-escaping happens BEFORE {var} → ${var} conversion."""

    def test_double_braces_stripped_before_single_braces(self) -> None:
        """{{name}} must become 'name', not '$name' or corrupt output."""
        template = "Hello {{name}}, plan: {research_plan}"
        ctx = _FakeAgentState(research_plan="find bugs")
        builder = create_prompt_builder(template)
        result = builder(ctx)
        assert "Hello name" in result
        assert "$name" not in result
        assert "find bugs" in result

    def test_single_braces_converted_to_dollar_braces(self) -> None:
        """{query} must become ${query} and resolve correctly."""
        template = "Query: {query}"
        ctx = _FakeAgentState(query="test query")
        builder = create_prompt_builder(template)
        result = builder(ctx)
        assert "test query" in result
        assert "{query}" not in result

    def test_only_literal_double_braces(self) -> None:
        """Only literal braces with no state vars should work."""
        template = "{{name}}"
        ctx = _FakeAgentState()
        builder = create_prompt_builder(template)
        result = builder(ctx)
        assert result == "name"

    def test_no_corruption_with_multiple_doubles(self) -> None:
        """Multiple {{x}} {{y}} should all de-escape correctly."""
        template = "{{a}} and {{b}} and {query}"
        ctx = _FakeAgentState(query="Q")
        builder = create_prompt_builder(template)
        result = builder(ctx)
        assert "a and b" in result
        assert "Q" in result
        assert "$a" not in result
        assert "$b" not in result

    def test_unresolved_variable_left_visible(self) -> None:
        """Unresolved {missing_var} must be left visible with a WARNING, not stripped."""
        template = "See {missing_var}"
        ctx = _FakeAgentState()
        builder = create_prompt_builder(template)
        # The module logs a warning; we just verify the var isn't silently stripped
        result = builder(ctx)
        assert "missing_var" in result or "${missing_var}" in result
