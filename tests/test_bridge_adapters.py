"""SP-5: tests for bridges/crewai/task + bridges/autogen/assistant.

beagle-spotless-phase2, work package SP-5. These bridge adapter types had no
direct tests.
"""

from __future__ import annotations

from beagle.bridges.autogen.assistant import BeagleAutoGenAssistant
from beagle.bridges.crewai.task import BeagleCrewAITask

# ── BeagleCrewAITask ────────────────────────────────────────────────────────


def test_task_defaults() -> None:
    """A task defaults to a helpful expected-output and empty tools/context."""
    t = BeagleCrewAITask(description="do the work")
    assert t.description == "do the work"
    assert t.expected_output == "Complete the task successfully."
    assert t.tools == []
    assert t.context == []
    assert t.async_execution is False


def test_task_expected_output_override() -> None:
    """A provided expected_output is preserved."""
    t = BeagleCrewAITask(description="d", expected_output="a report")
    assert t.expected_output == "a report"


def test_task_set_context_results() -> None:
    """set_context_results stores prerequisite-task output."""
    t = BeagleCrewAITask()
    t.set_context_results(["res1", "res2"])
    assert t._context_results == ["res1", "res2"]


def test_task_from_workflow_phase() -> None:
    """from_workflow_phase maps a Beagle phase to a Task."""
    phase = {"prompt_template": "search the repo", "expected_output": "findings"}
    t = BeagleCrewAITask.from_workflow_phase(phase)
    assert t.description == "search the repo"
    assert t.expected_output == "findings"


# ── BeagleAutoGenAssistant ──────────────────────────────────────────────────


def test_assistant_defaults() -> None:
    """Assistant has a default name and system message."""
    a = BeagleAutoGenAssistant()
    assert a.name == "assistant"
    assert a.system_message == "You are a helpful AI assistant."
    assert a._tools == []


def test_assistant_with_tools() -> None:
    """Assistant accepts a tool list."""
    a = BeagleAutoGenAssistant(name="coder", tools=["read_file", "write_file"])
    assert a.name == "coder"
    assert a._tools == ["read_file", "write_file"]


def test_assistant_system_message() -> None:
    """Assistant preserves a custom system message."""
    a = BeagleAutoGenAssistant(name="auditor", system_message="be critical")
    assert a.system_message == "be critical"
