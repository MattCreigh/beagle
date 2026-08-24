"""Tests for LangGraph graph construction (Beagle v13.6.1 — circuit-breaker routers)."""

from __future__ import annotations

from langgraph.graph import StateGraph

from beagle.core.graph import (
    _check_circuit_breaker,
    build_research_graph,
    build_workflow_graph,
    error_router,
    executor_router,
    reviewer_router,
)
from beagle.core.state import create_initial_state


class TestBuildResearchGraph:
    """Tests for research graph construction."""

    def test_build_returns_state_graph(self):
        graph = build_research_graph()
        assert isinstance(graph, StateGraph)

    def test_graph_compiles(self):
        graph = build_research_graph()
        compiled = graph.compile()
        assert compiled is not None

    def test_graph_has_expected_nodes(self):
        graph = build_research_graph()
        node_names = set(graph.nodes.keys())
        assert "planning" in node_names
        assert "execution" in node_names
        assert "verification" in node_names
        assert "synthesis" in node_names


class TestBuildWorkflowGraph:
    """Tests for custom workflow graph construction."""

    def test_build_simple_workflow(self):
        nodes = [
            {
                "name": "step1",
                "skill_name": "research-planner",
                "prompt_template": "Plan: {query}",
                "output_key": "plan",
            },
            {
                "name": "step2",
                "skill_name": "search-executor",
                "prompt_template": "Execute: {plan}",
                "output_key": "execution",
            },
        ]
        transitions = [("step1", "step2", None)]

        graph = build_workflow_graph(nodes, transitions)
        assert isinstance(graph, StateGraph)
        compiled = graph.compile()
        assert compiled is not None

    def test_build_empty_workflow_compiles(self):
        graph = build_workflow_graph([], [])
        assert isinstance(graph, StateGraph)

    def test_build_single_node_workflow(self):
        nodes = [
            {
                "name": "only",
                "skill_name": "research-planner",
                "prompt_template": "{query}",
                "output_key": "result",
            },
        ]
        graph = build_workflow_graph(nodes, [])
        compiled = graph.compile()
        assert compiled is not None


class TestCircuitBreakerRouters:
    """Tests for circuit-breaker-aware conditional edge routing."""

    def test_executor_router_with_context(self):
        state = {
            "raw_execution_context": "some findings here",
            "operational": {"iteration": 0, "error_count": 0, "total_iterations": 0},
        }
        assert executor_router(state) == "verification"

    def test_executor_router_skip_verify_without_context(self):
        state = {
            "raw_execution_context": "",
            "operational": {"iteration": 0, "error_count": 0, "total_iterations": 0},
        }
        assert executor_router(state) == "synthesis"

    def test_executor_router_circuit_breaker_max_iterations(self):
        state = {
            "raw_execution_context": "findings",
            "operational": {
                "iteration": 25,
                "error_count": 0,
                "total_iterations": 25,
            },
        }
        assert executor_router(state) == "__end__"

    def test_reviewer_router_approved(self):
        state = {
            "review_feedback": "",
            "operational": {"iteration": 1, "error_count": 0, "total_iterations": 1},
        }
        assert reviewer_router(state) == "synthesis"

    def test_reviewer_router_circuit_breaker_max_iterations(self):
        state = {
            "review_feedback": "redo",
            "operational": {
                "iteration": 25,
                "error_count": 0,
                "total_iterations": 25,
            },
        }
        assert reviewer_router(state) == "__end__"

    def test_error_router_retries(self):
        state = {
            "errors": ["error1"],
            "operational": {"iteration": 1, "error_count": 1, "total_iterations": 1},
        }
        assert error_router(state, target_node="planning") == "planning"

    def test_error_router_max_errors(self):
        state = {
            "errors": ["e1", "e2", "e3"],
            "operational": {"iteration": 1, "error_count": 3, "total_iterations": 1},
        }
        assert error_router(state, target_node="planning") == "__end__"

    def test_check_circuit_breaker_none_under_limits(self):
        state = {
            "operational": {"iteration": 0, "error_count": 0, "total_iterations": 0},
        }
        assert _check_circuit_breaker(state) is None

    def test_check_circuit_breaker_max_iterations(self):
        state = {
            "operational": {
                "iteration": 25,
                "error_count": 0,
                "total_iterations": 25,
            },
        }
        assert _check_circuit_breaker(state) == "__end__"


class TestCreateInitialState:
    """Tests for initial state creation."""

    def test_creates_valid_state(self):
        state = create_initial_state("test query", "test_workflow")
        assert state["query"] == "test query"
        assert state["workflow_id"] == "test_workflow"

    def test_steering_prompt(self):
        state = create_initial_state("q", steering_prompt="focus on security")
        assert state["steering_prompt"] == "focus on security"
