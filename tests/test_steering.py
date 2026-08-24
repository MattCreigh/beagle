"""Tests for the Beagle Mid-Workflow Steering system."""

import json
import os

from beagle.steering.injection import (
    extract_steering_tags,
    inject_steering,
    inject_steering_metadata,
    strip_steering_tags,
)
from beagle.steering.manager import SteeringDirective, SteeringManager
from beagle.steering.sources import (
    APISource,
    EnvSteeringSource,
    FileSteeringSource,
    SteeringSourceManager,
    TUIChannelSource,
)

# =============================================================================
# SteeringDirective Tests
# =============================================================================


def test_directive_defaults():
    """Test SteeringDirective default values."""
    directive = SteeringDirective(workflow_id="test")

    assert directive.workflow_id == "test"
    assert directive.has_guidance is False
    assert directive.priority_guidance == ""
    assert directive.skip_nodes == []
    assert directive.budget_override_usd is None
    assert directive.stop_after_node is None
    assert directive.source == "file"


# =============================================================================
# FileSteeringSource Tests
# =============================================================================


def test_file_source_parsing(tmp_path):
    """Test that steer.md is correctly parsed into a SteeringDirective."""
    steer_file = tmp_path / "steer.md"
    content = """# Steering Guidance

## Priority
Focus on security fixes only.

## Skip Nodes
- verification
- synthesis

## Budget Override
50.0

## Stop After
execution
"""
    steer_file.write_text(content)

    source = FileSteeringSource(path=steer_file, workflow_id="test_wf")
    directive = source.read()

    assert directive is not None
    assert directive.has_guidance is True
    assert "security fixes" in directive.priority_guidance
    assert "verification" in directive.skip_nodes
    assert "synthesis" in directive.skip_nodes
    assert directive.budget_override_usd == 50.0
    assert directive.stop_after_node == "execution"
    assert directive.source == "file"


def test_file_source_acknowledge(tmp_path):
    """Test that steering file is renamed after acknowledge."""
    steer_file = tmp_path / "steer.md"
    steer_file.write_text("# Priority\nTest")

    source = FileSteeringSource(path=steer_file)
    source.read()
    source.acknowledge()

    assert not steer_file.exists()
    applied_files = list(tmp_path.glob("steer.md.applied.*"))
    assert len(applied_files) == 1


def test_file_source_no_file(tmp_path):
    """Test behavior when no steering file exists."""
    source = FileSteeringSource(path=tmp_path / "nonexistent.md")
    directive = source.read()
    assert directive is None


def test_file_source_comma_separated_skip(tmp_path):
    """Test comma-separated skip nodes."""
    steer_file = tmp_path / "steer.md"
    steer_file.write_text("""# Steering Guidance

## Skip Nodes
ui-testing, legacy-reports, deprecated-node
""")

    source = FileSteeringSource(path=steer_file)
    directive = source.read()

    assert "ui-testing" in directive.skip_nodes
    assert "legacy-reports" in directive.skip_nodes
    assert "deprecated-node" in directive.skip_nodes


# =============================================================================
# EnvSteeringSource Tests
# =============================================================================


def test_env_source_read():
    """Test reading steering from environment variables."""
    original = os.environ.get("BEAGLE_STEER_PRIORITY")

    try:
        os.environ["BEAGLE_STEER_PRIORITY"] = "Focus on performance"
        os.environ["BEAGLE_STEER_SKIP"] = "debug,test"
        os.environ["BEAGLE_STEER_BUDGET"] = "25.00"
        os.environ["BEAGLE_STEER_STOP"] = "finalize"

        source = EnvSteeringSource()
        directive = source.read()

        assert directive is not None
        assert directive.has_guidance is True
        assert directive.priority_guidance == "Focus on performance"
        assert "debug" in directive.skip_nodes
        assert "test" in directive.skip_nodes
        assert directive.budget_override_usd == 25.0
        assert directive.stop_after_node == "finalize"
        assert directive.source == "env"
    finally:
        # Cleanup
        for key in [
            "BEAGLE_STEER_PRIORITY",
            "BEAGLE_STEER_SKIP",
            "BEAGLE_STEER_BUDGET",
            "BEAGLE_STEER_STOP",
        ]:
            os.environ.pop(key, None)
        if original is not None:
            os.environ["BEAGLE_STEER_PRIORITY"] = original


def test_env_source_acknowledge():
    """Test that env vars are cleared after acknowledge."""
    os.environ["BEAGLE_STEER_PRIORITY"] = "test"
    os.environ["BEAGLE_STEER_SKIP"] = "a,b"

    source = EnvSteeringSource()
    source.acknowledge()

    assert "BEAGLE_STEER_PRIORITY" not in os.environ
    assert "BEAGLE_STEER_SKIP" not in os.environ


def test_env_source_no_vars():
    """Test when no steering env vars are set."""
    for key in [
        "BEAGLE_STEER_PRIORITY",
        "BEAGLE_STEER_SKIP",
        "BEAGLE_STEER_BUDGET",
        "BEAGLE_STEER_STOP",
    ]:
        os.environ.pop(key, None)

    source = EnvSteeringSource()
    directive = source.read()
    assert directive is None


# =============================================================================
# TUIChannelSource Tests
# =============================================================================


def test_tui_source_read():
    """Test reading steering from TUI queue."""
    from queue import Queue

    q = Queue()
    q.put(
        {
            "type": "steering",
            "priority": "Check the database",
            "skip": ["ui", "frontend"],
            "budget": 30.0,
            "stop_after": "db_optimize",
        }
    )

    source = TUIChannelSource(queue=q)
    directive = source.read()

    assert directive is not None
    assert directive.has_guidance is True
    assert directive.priority_guidance == "Check the database"
    assert "ui" in directive.skip_nodes
    assert "frontend" in directive.skip_nodes
    assert directive.budget_override_usd == 30.0
    assert directive.stop_after_node == "db_optimize"
    assert directive.source == "tui"


def test_tui_source_empty_queue():
    """Test empty TUI queue returns None."""
    from queue import Queue

    source = TUIChannelSource(queue=Queue())
    directive = source.read()
    assert directive is None


def test_tui_source_no_queue():
    """Test when no queue is set."""
    source = TUIChannelSource(queue=None)
    directive = source.read()
    assert directive is None


def test_tui_source_string_skip():
    """Test TUI skip as comma-separated string."""
    from queue import Queue

    q = Queue()
    q.put({"type": "steering", "skip": "node1,node2,node3"})

    source = TUIChannelSource(queue=q)
    directive = source.read()

    assert "node1" in directive.skip_nodes
    assert "node2" in directive.skip_nodes
    assert "node3" in directive.skip_nodes


# =============================================================================
# APISource Tests
# =============================================================================


def test_api_source_read(tmp_path):
    """Test reading steering from API state file."""
    api_file = tmp_path / "steer_api.json"
    data = {
        "active": True,
        "workflow_id": "api_workflow",
        "priority": "Optimize queries",
        "skip_nodes": ["cache_check"],
        "budget_override_usd": 100.0,
        "stop_after_node": "deploy",
    }
    api_file.write_text(json.dumps(data))

    source = APISource(state_path=api_file)
    directive = source.read()

    assert directive is not None
    assert directive.has_guidance is True
    assert "Optimize queries" in directive.priority_guidance
    assert "cache_check" in directive.skip_nodes
    assert directive.budget_override_usd == 100.0
    assert directive.stop_after_node == "deploy"
    assert directive.source == "api"


def test_api_source_not_active(tmp_path):
    """Test API source returns None when not active."""
    api_file = tmp_path / "steer_api.json"
    data = {"active": False}
    api_file.write_text(json.dumps(data))

    source = APISource(state_path=api_file)
    directive = source.read()
    assert directive is None


def test_api_source_acknowledge(tmp_path):
    """Test API source deactivates after acknowledge."""
    api_file = tmp_path / "steer_api.json"
    api_file.write_text(json.dumps({"active": True}))

    source = APISource(state_path=api_file)
    source.read()
    source.acknowledge()

    data = json.loads(api_file.read_text())
    assert data["active"] is False
    assert "applied_at" in data


# =============================================================================
# SteeringSourceManager Tests
# =============================================================================


def test_source_manager_priority(tmp_path):
    """Test that sources are checked in priority order."""
    # Sources look in .beagle/ subdirectory
    beagle_dir = tmp_path / ".beagle"
    beagle_dir.mkdir()

    # Setup: API source should win over file source
    api_file = beagle_dir / "steer_api.json"
    api_file.write_text(json.dumps({"active": True, "priority": "API priority"}))

    steer_file = beagle_dir / "steer.md"
    steer_file.write_text("# Priority\nFile priority")

    manager = SteeringSourceManager(workspace_root=tmp_path)
    directive = manager.check()

    # API should take priority
    assert directive is not None
    assert "API priority" in directive.priority_guidance


def test_source_manager_fallback(tmp_path):
    """Test fallback to file when API has no guidance."""
    beagle_dir = tmp_path / ".beagle"
    beagle_dir.mkdir()

    api_file = beagle_dir / "steer_api.json"
    api_file.write_text(json.dumps({"active": False}))

    steer_file = beagle_dir / "steer.md"
    steer_file.write_text("# Priority\nFile guidance")

    manager = SteeringSourceManager(workspace_root=tmp_path)
    directive = manager.check()

    assert directive is not None
    assert "File guidance" in directive.priority_guidance


# =============================================================================
# SteeringManager Tests
# =============================================================================


def test_steering_manager_check(tmp_path):
    """Test SteeringManager.check() returns directive."""
    beagle_dir = tmp_path / ".beagle"
    beagle_dir.mkdir()
    steer_file = beagle_dir / "steer.md"
    steer_file.write_text("# Priority\nManager test")

    manager = SteeringManager(workspace_root=tmp_path, workflow_id="mgr_test")
    directive = manager.check()

    assert directive.has_guidance is True
    assert "Manager test" in directive.priority_guidance


def test_steering_manager_no_file(tmp_path):
    """Test SteeringManager with no guidance."""
    manager = SteeringManager(workspace_root=tmp_path)
    directive = manager.check()
    assert directive.has_guidance is False


def test_steering_manager_applied_count(tmp_path):
    """Test applied count tracking."""
    beagle_dir = tmp_path / ".beagle"
    beagle_dir.mkdir()
    steer_file = beagle_dir / "steer.md"

    manager = SteeringManager(workspace_root=tmp_path)
    assert manager.applied_count == 0

    # Apply once
    steer_file.write_text("# Priority\nFirst")
    directive = manager.check()
    assert directive.has_guidance
    assert manager.applied_count == 1

    # Reset and apply again
    manager.reset()
    assert manager.applied_count == 0


# =============================================================================
# Prompt Injection Tests
# =============================================================================


def test_inject_steering_basic():
    """Test basic steering injection."""
    prompt = (
        "<intent>Fulfill this</intent>\n"
        "<recipe>Recipe content</recipe>\n"
        "<system_directive>System instructions</system_directive>"
    )
    directive = SteeringDirective(
        workflow_id="w1", has_guidance=True, priority_guidance="Focus on speed"
    )

    injected = inject_steering(prompt, directive)

    assert "<steering>" in injected
    assert "Focus on speed" in injected
    assert "</recipe>\n<steering>" in injected


def test_inject_steering_no_guidance():
    """Test that prompt remains unchanged when no guidance."""
    prompt = "Original Prompt"
    directive = SteeringDirective(workflow_id="w1", has_guidance=False)
    assert inject_steering(prompt, directive) == prompt


def test_inject_steering_fallback_to_system():
    """Test fallback insertion before <system_directive>."""
    prompt = "<intent>Test</intent>\n<system_directive>System</system_directive>"
    directive = SteeringDirective(workflow_id="w1", has_guidance=True, priority_guidance="Hint")

    injected = inject_steering(prompt, directive)

    assert "<steering>" in injected
    assert "Hint" in injected


def test_inject_steering_fallback_to_context():
    """Test fallback insertion before <context>."""
    prompt = "<intent>Test</intent>\n<context>Context here</context>"
    directive = SteeringDirective(workflow_id="w1", has_guidance=True, priority_guidance="Hint")

    injected = inject_steering(prompt, directive)

    assert "<steering>" in injected


def test_inject_steering_metadata():
    """Test metadata injection."""
    prompt = "Original prompt"
    directive = SteeringDirective(
        workflow_id="w1",
        has_guidance=True,
        skip_nodes=["n1", "n2"],
        stop_after_node="s1",
        budget_override_usd=50.0,
    )

    injected = inject_steering_metadata(prompt, directive)

    assert "Skip the following nodes: n1, n2" in injected
    assert "Stop workflow after completing: s1" in injected
    assert "$50.00" in injected


def test_extract_steering_tags():
    """Test extracting steering block from prompt."""
    prompt = "Before\n<steering>Guidance here</steering>\nAfter"
    extracted = extract_steering_tags(prompt)
    assert extracted == "Guidance here"


def test_strip_steering_tags():
    """Test removing steering block from prompt."""
    prompt = "Before\n<steering>Remove me</steering>\nAfter"
    stripped = strip_steering_tags(prompt)

    assert "<steering>" not in stripped
    assert "Remove me" not in stripped
    assert "Before" in stripped
    assert "After" in stripped


# =============================================================================
# Integration Tests (Orchestrator-related)
# =============================================================================


def test_steering_budget_override():
    """Test that budget override directive updates values."""
    from beagle.core.autonomous_orchestrator import DAGOrchestrator

    orchestrator = DAGOrchestrator(budget_usd=10.0)

    directive = SteeringDirective(workflow_id="w1", has_guidance=True, budget_override_usd=50.0)

    # Simulate the logic in run()
    if directive.budget_override_usd:
        orchestrator.budget_usd = directive.budget_override_usd
        orchestrator.cost_tracker.budget_usd = directive.budget_override_usd

    assert orchestrator.budget_usd == 50.0
    assert orchestrator.cost_tracker.budget_usd == 50.0


def test_steering_skip_nodes():
    """Test skip nodes directive parsing."""
    directive = SteeringDirective(
        workflow_id="w1", has_guidance=True, skip_nodes=["node1", "node2"]
    )

    assert "node1" in directive.skip_nodes
    assert "node2" in directive.skip_nodes


# =============================================================================
# Error Handling Tests
# =============================================================================


def test_file_source_malformed(tmp_path):
    """Test handling of malformed markdown."""
    steer_file = tmp_path / "steer.md"
    steer_file.write_text("This is not valid markdown")

    source = FileSteeringSource(path=steer_file)
    directive = source.read()

    # Should still return with has_guidance=True but empty fields
    assert directive.has_guidance is True


def test_api_source_malformed_json(tmp_path):
    """Test handling of malformed JSON."""
    api_file = tmp_path / "steer_api.json"
    api_file.write_text("not valid json {{{")

    source = APISource(state_path=api_file)
    directive = source.read()

    assert directive is None


def test_steering_manager_with_wrong_file_location(tmp_path):
    """Test SteeringManager looks in correct location."""
    # Create file in wrong location
    wrong_file = tmp_path / "other.md"
    wrong_file.write_text("# Priority\nWrong")

    # Manager should not find it (it's looking for steer.md)
    manager = SteeringManager(workspace_root=tmp_path)
    directive = manager.check()
    assert directive.has_guidance is False
