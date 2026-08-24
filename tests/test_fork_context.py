"""SP-5: tests for context/fork_context (was zero-coverage).

beagle-spotless-phase2, work package SP-5. ForkContext reuses parent static
prompt parts and memory pointers for subagent forks. These exercise
construction from a parent, the scratchpad, and prompt building.
"""

from __future__ import annotations

from beagle.context.fork_context import ForkContext
from beagle.context.prompt_cache import PromptCache
from beagle.core.orchestrator_types import AgentState


def test_fork_context_construction() -> None:
    """A ForkContext can be built directly with its fields."""
    fork = ForkContext(
        fork_id="f1",
        parent_static_cache={"a": "1"},
        parent_memory_pointers="mem",
        parent_completed_nodes=["planning"],
        parent_observations="obs",
    )
    assert fork.fork_id == "f1"
    assert fork.scratchpad == []


def test_from_parent_snapshot() -> None:
    """from_parent snapshots the parent cache and state."""
    cache = PromptCache()
    cache._static_cache = {"recipe": "researcher"}
    state = AgentState(
        query="q",
        completed_nodes=["planning", "execution"],
        raw_execution_context="lots of context",
    )
    fork = ForkContext.from_parent(cache, state, "mem", fork_id="fork-1")
    assert fork.parent_static_cache == {"recipe": "researcher"}
    assert fork.parent_completed_nodes == ["planning", "execution"]
    assert "lots of context" in fork.parent_observations


def test_add_observation_and_get_scratchpad() -> None:
    """Observations accumulate in the scratchpad."""
    fork = ForkContext("f", {}, None, [], "")
    fork.add_observation("first")
    fork.add_observation("second")
    assert fork.get_scratchpad() == ["first", "second"]


def test_get_scratchpad_returns_copy() -> None:
    """get_scratchpad returns a copy, not the internal list."""
    fork = ForkContext("f", {}, None, [], "")
    fork.add_observation("x")
    scratch = fork.get_scratchpad()
    scratch.append("mutated")
    assert fork.get_scratchpad() == ["x"]


def test_build_prompt_contains_parent_progress() -> None:
    """build_prompt includes the parent's completed nodes."""
    fork = ForkContext(
        fork_id="f",
        parent_static_cache={},
        parent_memory_pointers="mem",
        parent_completed_nodes=["planning"],
        parent_observations="obs",
    )
    prompt = fork.build_prompt("do the next step")
    assert "planning" in prompt
    assert "PARENT PROGRESS" in prompt


def test_build_prompt_root_when_no_nodes() -> None:
    """build_prompt falls back to 'root' node when no parent nodes exist."""
    fork = ForkContext("f", {}, "mem", [], "obs")
    prompt = fork.build_prompt("start")
    assert isinstance(prompt, str)
    assert len(prompt) > 0
