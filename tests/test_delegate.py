"""Tests for subagent delegation executor (Phase 8.6).

Covers:
- YAML parsing of delegate_to / delegate_mode fields
- Graph construction with executor: delegate

v1.0.0: a module-level ``pytest.mark.xfail`` used to blanket this file with
"execute_delegate_node removed — delegate now via bridges". Only the
``TestDelegateNodeDispatch`` tests actually called that removed function; the
YAML and graph-integration tests below pass on their own and were reported as
XPASS. A blanket xfail on passing tests hides real regressions — they would
have silently flipped to xfail instead of failing — so the marker is gone and
the obsolete dispatch tests (plus test_delegate_parallel.py, which existed
solely to exercise the removed function's parallel path) were deleted.
"""

from __future__ import annotations

from beagle.core.graph import build_workflow_graph
from beagle.core.workflow_loader import _build_graph_from_spec

DAGORCH_PATH = "beagle.core.autonomous_orchestrator.DAGOrchestrator"


class MockState:
    """Minimal state mock for delegate tests."""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        if "metadata" not in self.__dict__:
            self.__dict__["metadata"] = {}

    def __getattr__(self, name: str):
        return self.__dict__.get(name, "")


class TestDelegateYAML:
    """delegate fields are parsed from YAML specs."""

    def test_build_graph_from_spec_with_delegate(self):
        spec = {
            "name": "audit-with-delegate",
            "phases": [
                {
                    "name": "audit",
                    "agent": "audit-reviewer",
                    "prompt_template": "Deep audit: {raw_execution_context}",
                    "executor": "delegate",
                    "delegate_to": "security",
                    "delegate_mode": "sequential",
                    "output_key": "audit_result",
                }
            ],
        }
        graph = _build_graph_from_spec(spec, workflow_query="test")
        assert graph is not None

    def test_executor_field_is_delegate(self):
        spec = {
            "name": "simple",
            "phases": [
                {
                    "name": "deep",
                    "agent": "planner",
                    "prompt_template": "p",
                    "executor": "delegate",
                    "delegate_to": "research",
                }
            ],
        }
        graph = _build_graph_from_spec(spec)
        # Compilation should succeed without raising
        compiled = graph.compile()
        assert compiled is not None


class TestDelegateGraphIntegration:
    """build_workflow_graph wires delegate executor correctly."""

    def test_delegate_executor_route(self):
        """build_workflow_graph wires delegate executor correctly."""
        nodes = [
            {
                "name": "audit",
                "skill_name": "audit-reviewer",
                "prompt_template": "Audit: {query}",
                "output_key": "audit",
                "executor": "delegate",
                "delegate_to": "security",
                "delegate_mode": "sequential",
            }
        ]
        graph = build_workflow_graph(nodes, [])
        # Compilation should succeed with delegate executor
        compiled = graph.compile()
        assert compiled is not None
        # Node is present
        assert "audit" in graph.nodes
