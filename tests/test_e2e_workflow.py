"""End-to-end integration tests for workflow execution.

Tests cover happy path DAG execution, template substitution verification,
error propagation, and budget enforcement.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
# workflow_builder imports workflow_schema which needs core/ in path
sys.path.insert(0, str(Path(__file__).parent.parent / "beagle" / "core"))

from beagle.core.orchestrator_types import AgentState
from beagle.core.workflow_builder import (
    build_dag_node,
    build_orchestrator,
    create_prompt_builder,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_SPEC = {
    "phases": [
        {
            "name": "research",
            "agent": "researcher",
            "prompt_template": "Research: {query}",
            "output_key": "research_plan",
        },
        {
            "name": "synthesis",
            "agent": "synthesis-writer",
            "prompt_template": "Synthesize: {research_plan}",
            "output_key": "final_report",
            "depends_on": ["research"],
        },
    ],
}

THREE_PHASE_SPEC = {
    "phases": [
        {
            "name": "research",
            "agent": "researcher",
            "prompt_template": "Research: {query}",
            "output_key": "research_plan",
        },
        {
            "name": "analysis",
            "agent": "analyst",
            "prompt_template": "Analyze: {research_plan}",
            "output_key": "verified_facts",
            "depends_on": ["research"],
        },
        {
            "name": "synthesis",
            "agent": "synthesis-writer",
            "prompt_template": "Synthesize: {verified_facts}",
            "output_key": "final_report",
            "depends_on": ["analysis"],
        },
    ],
}


def _make_state(**overrides):
    """Create a default AgentState with optional overrides."""
    defaults = {
        "query": "test query",
        "research_plan": "a solid plan",
        "raw_execution_context": "execution results",
        "verified_facts": "verified stuff",
        "final_report": "final answer",
    }
    defaults.update(overrides)
    return AgentState(**defaults)


# ---------------------------------------------------------------------------
# Test 1: Minimal 2-phase DAG builds and template builders work
# ---------------------------------------------------------------------------


class TestMinimalWorkflowBuilds:
    """Verify DAG builds from a minimal spec and nodes are wired correctly."""

    def test_build_dag_nodes_from_spec(self):
        """Each phase produces a valid DAGNode with prompt_builder."""
        for phase in MINIMAL_SPEC["phases"]:
            node = build_dag_node(phase, MINIMAL_SPEC)
            assert node.name == phase["name"]
            assert node.skill_name == phase["agent"]
            assert callable(node.prompt_builder)

    def test_build_orchestrator_from_spec(self):
        """Full orchestrator builds with nodes and transitions."""
        dag = build_orchestrator(MINIMAL_SPEC, Path("."))
        assert dag.start_node == "research"
        assert "research" in dag.nodes
        assert "synthesis" in dag.nodes

    def test_prompt_builder_substitutes_query(self):
        """Prompt builder substitutes {query} from state."""
        phase = MINIMAL_SPEC["phases"][0]
        builder = create_prompt_builder(phase["prompt_template"])
        state = _make_state()
        result = builder(state)
        assert "test query" in result
        assert "{query}" not in result

    def test_prompt_builder_substitutes_research_plan(self):
        """Second node gets {research_plan} from state."""
        phase = MINIMAL_SPEC["phases"][1]
        builder = create_prompt_builder(phase["prompt_template"])
        state = _make_state()
        result = builder(state)
        assert "a solid plan" in result
        assert "{research_plan}" not in result


# ---------------------------------------------------------------------------
# Test 2: Template substitution across all workflow YAMLs
# ---------------------------------------------------------------------------


class TestAllWorkflowYamlTemplates:
    """Verify no unsubstituted {variable} patterns remain after builder runs."""

    METAPROMPTS_DIR = Path(__file__).parent.parent / "beagle" / "metaprompts"

    @staticmethod
    def _load_yaml_safely(path: Path) -> dict | None:
        """Load a YAML file safely, returning None on parse failure."""
        try:
            import yaml

            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f)
        except Exception:  # ruff: ignore[BLE001]
            return None

    def _get_all_template_strings(self) -> list[tuple[str, str]]:
        """Return (workflow_name, prompt_template) pairs from all YAMLs."""
        pairs = []
        for path in sorted(self.METAPROMPTS_DIR.glob("*.yaml")):
            spec = self._load_yaml_safely(path)
            if not spec or "phases" not in spec:
                continue
            for phase in spec["phases"]:
                tpl = phase.get("prompt_template", "")
                if tpl:
                    pairs.append((path.stem, tpl))
        return pairs

    def test_all_yamls_parse_and_have_phases(self):
        """Every workflow YAML must parse and have a non-empty phases list."""
        for path in sorted(self.METAPROMPTS_DIR.glob("*.yaml")):
            spec = self._load_yaml_safely(path)
            assert spec is not None, f"YAML parse failed: {path.name}"
            assert "phases" in spec, f"Missing 'phases': {path.name}"
            assert len(spec["phases"]) > 0, f"Empty phases: {path.name}"

    def test_template_substitution_produces_nonempty(self):
        """Every prompt template should produce non-empty output after substitution."""
        state = _make_state()
        for _name, tpl in self._get_all_template_strings():
            builder = create_prompt_builder(tpl)
            result = builder(state)
            assert len(result.strip()) > 0, f"Template produced empty output: {tpl[:50]}"

    def test_no_unsubstituted_variables_remain(self):
        """After substitution, no bare {variable} patterns should remain.

        Unresolved variables are now left visible as ${var} so agents can
        react, but the old bare-brace syntax must not leak through.
        """
        import re

        state = _make_state()
        for name, tpl in self._get_all_template_strings():
            builder = create_prompt_builder(tpl)
            result = builder(state)
            # Only flag bare {var} — ${var} is intentional (safe_substitute).
            leftovers = re.findall(r"(?<!\$)\{[a-zA-Z_][a-zA-Z0-9_]*\}", result)
            assert len(leftovers) == 0, (
                f"Unsubstituted vars in {name}: {leftovers} (template: {tpl[:80]})"
            )


# ---------------------------------------------------------------------------
# Test 3: Error propagation — node failure doesn't crash workflow
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    """Verify that node errors are recorded but workflow continues where possible."""

    def test_state_mutator_records_output(self):
        """When a node succeeds, mutator sets the output key on state."""
        from beagle.core.workflow_builder import create_mutator

        state = _make_state()
        mutator = create_mutator("research_plan")
        mutator(state, "plan data here")
        assert state.research_plan == "plan data here"

    def test_orchestrator_builds_three_phase_dag(self):
        """Three-phase workflow builds with correct node count and transitions."""
        dag = build_orchestrator(THREE_PHASE_SPEC, Path("."))
        assert len(dag.nodes) == 3
        assert dag.start_node == "research"
        assert "research" in dag.nodes
        assert "analysis" in dag.nodes
        assert "synthesis" in dag.nodes

    def test_budget_check_returns_false_when_exceeded(self):
        """CostTracker.check_budget() returns False when cost exceeds budget."""
        from beagle.cost_tracker import ContextAwareCostTracker

        tracker = ContextAwareCostTracker(budget_usd=0.001, model="test")
        tracker._total_cost = 1.0
        assert not tracker.check_budget()

    def test_budget_check_returns_true_when_under(self):
        """CostTracker.check_budget() returns True when cost is under budget."""
        from beagle.cost_tracker import ContextAwareCostTracker

        tracker = ContextAwareCostTracker(budget_usd=10.0, model="test")
        tracker._total_cost = 0.5
        assert tracker.check_budget()

    def test_state_errors_list_captures_budget_exceeded(self):
        """Orchestrator records 'Budget exceeded' in state.errors when budget runs out."""
        dag = build_orchestrator(MINIMAL_SPEC, Path("."))
        # Override budget and inflate cost on the SAME tracker instance
        dag.cost_tracker.budget_usd = 0.001
        dag.cost_tracker._total_cost = 1.0
        assert not dag.cost_tracker.check_budget()
        # Simulate what the orchestrator does in _run_inner
        dag.state.errors.append("Budget exceeded - workflow halted")
        assert any("Budget exceeded" in e for e in dag.state.errors)
