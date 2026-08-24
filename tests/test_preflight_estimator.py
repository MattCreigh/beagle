"""SP-5: tests for preflight/estimator (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The PreFlightEstimator forecasts
cost/token/runtime for a workflow's DAG nodes. These exercise dict-based and
object-based node specs, budget sufficiency, and warning collection.
"""

from __future__ import annotations

from beagle.preflight.estimator import (
    NodeEstimate,
    PreFlightEstimate,
    PreFlightEstimator,
)


def _dict_node(name: str, skill: str = "researcher", model: str | None = None) -> dict:
    node: dict = {"name": name, "skill_name": skill}
    if model:
        node["model"] = model
    return node


def test_estimator_with_dict_nodes() -> None:
    """Estimate from dict-based node specs."""
    est = PreFlightEstimator(budget_usd=10.0)
    result = est.estimate("audit", [_dict_node("n1"), _dict_node("n2")])
    assert result.workflow_name == "audit"
    assert result.node_count == 2
    assert len(result.nodes) == 2
    assert result.total_estimated_cost_usd >= 0.0
    assert result.total_estimated_tokens > 0
    assert result.total_estimated_runtime_seconds > 0
    assert result.budget_usd == 10.0
    assert result.budget_sufficient is True


def test_node_estimate_fields() -> None:
    """Each node estimate carries resolved model/provider + metrics."""
    est = PreFlightEstimator().estimate("w", [_dict_node("n1")])
    node = est.nodes[0]
    assert node.node_name == "n1"
    assert node.skill_name == "researcher"
    assert isinstance(node.model, str) and node.model
    assert isinstance(node.provider, str) and node.provider
    assert node.estimated_input_tokens > 0
    assert node.estimated_cost_usd > 0
    assert node.context_window > 0
    assert 0.0 <= node.estimated_utilisation_percent <= 100.0


def test_budget_insufficient_when_cost_exceeds() -> None:
    """budget_sufficient is False when the forecast exceeds the budget."""
    est = PreFlightEstimator(budget_usd=0.0)
    result = est.estimate("w", [_dict_node("n1")])
    assert result.total_estimated_cost_usd > 0.0
    assert result.budget_sufficient is False


def test_empty_dag() -> None:
    """An empty node list yields a zeroed estimate."""
    result = PreFlightEstimator().estimate("empty", [])
    assert result.node_count == 0
    assert result.nodes == []
    assert result.total_estimated_cost_usd == 0.0
    assert result.budget_sufficient is True


def test_object_node_spec() -> None:
    """Estimator handles object-based node specs (with skill_name attr)."""

    class _Node:
        skill_name = "researcher"
        name = "obj-node"
        model_override = None

    result = PreFlightEstimator().estimate("w", [_Node()])
    assert result.node_count == 1
    assert result.nodes[0].node_name == "obj-node"


def test_preflight_estimate_defaults() -> None:
    """PreFlightEstimate warns list defaults to empty."""
    est = PreFlightEstimate(
        workflow_name="w",
        node_count=0,
        nodes=[],
        total_estimated_cost_usd=0.0,
        total_estimated_tokens=0,
        total_estimated_runtime_seconds=0.0,
        budget_usd=10.0,
        budget_sufficient=True,
    )
    assert est.warnings == []


def test_node_estimate_is_plain_dataclass() -> None:
    """NodeEstimate is a dataclass (repr + equality)."""
    a = NodeEstimate(
        node_name="n",
        skill_name="s",
        model="m",
        provider="p",
        estimated_input_tokens=1,
        estimated_output_tokens=1,
        estimated_cost_usd=0.1,
        estimated_runtime_seconds=1.0,
        context_window=100,
        estimated_utilisation_percent=2.0,
    )
    assert "NodeEstimate" in repr(a)
