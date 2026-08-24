"""SP-5: tests for bridges/crewai/converter (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The CrewAI <-> Beagle workflow
converter had no direct tests. These exercise both directions and the
sequential dependency wiring.
"""

from __future__ import annotations

from beagle.bridges.crewai.converter import (
    beagle_workflow_to_crewai,
    crewai_to_beagle_workflow,
)


def test_crewai_to_beagle_sequential() -> None:
    """CrewAI agents/tasks become Beagle phases with sequential deps."""
    agents = [{"role": "Security Auditor", "goal": "audit"}]
    tasks = [
        {
            "description": "review auth",
            "expected_output": "findings",
            "agent_role": "Security Auditor",
        },
        {"description": "report", "expected_output": "report", "agent_role": "Security Auditor"},
    ]
    wf = crewai_to_beagle_workflow(agents, tasks, process="sequential")
    assert wf["name"] == "crewai_imported_workflow"
    assert len(wf["phases"]) == 2
    assert wf["phases"][0]["agent"] == "security-auditor"
    # Sequential: phase 2 depends on phase 1.
    assert wf["phases"][1]["depends_on"] == ["phase_1"]


def test_crewai_to_beagle_hierarchical_no_deps() -> None:
    """Hierarchical process produces phases without sequential depends_on."""
    agents = [{"role": "Auditor"}]
    tasks = [
        {"description": "a", "expected_output": "x", "agent_role": "Auditor"},
        {"description": "b", "expected_output": "y", "agent_role": "Auditor"},
    ]
    wf = crewai_to_beagle_workflow(agents, tasks, process="hierarchical")
    assert "depends_on" not in wf["phases"][1]


def test_crewai_to_beagle_unknown_agent_defaults_researcher() -> None:
    """An agent_role with no matching agent maps to 'researcher'."""
    agents: list[dict] = []
    tasks = [{"description": "x", "expected_output": "y", "agent_role": "Ghost"}]
    wf = crewai_to_beagle_workflow(agents, tasks)
    assert wf["phases"][0]["agent"] == "researcher"


def test_beagle_to_crewai() -> None:
    """Beagle phases become CrewAI agents + tasks."""
    wf = {
        "phases": [
            {"name": "p1", "agent": "researcher", "prompt_template": "search"},
            {"name": "p2", "agent": "synthesis-writer", "prompt_template": "write"},
        ]
    }
    agents, tasks = beagle_workflow_to_crewai(wf)
    assert len(agents) == 2
    assert len(tasks) == 2
    assert tasks[0]["description"] == "search"
    assert tasks[0]["agent_role"] == "Researcher"


def test_beagle_to_crewai_round_trip_agent_dedup() -> None:
    """Multiple phases with the same agent produce one CrewAI agent."""
    wf = {
        "phases": [
            {"name": "p1", "agent": "researcher", "prompt_template": "a"},
            {"name": "p2", "agent": "researcher", "prompt_template": "b"},
        ]
    }
    agents, tasks = beagle_workflow_to_crewai(wf)
    assert len(agents) == 1
    assert len(tasks) == 2
