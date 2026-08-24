"""Tests for enriched NodeFailed event and error classification.

SP0-1A/1B/1C: Verifies that NodeFailed events carry structured debugging
context (model, error_category, stderr_snippet, duration, node_phase)
and that _classify_error / _infer_phase helpers work correctly.
"""

import pytest

from beagle.core.agent_spawner import _classify_error, _infer_phase
from beagle.core.nodes import _infer_phase_from_skill
from beagle.events.events import NodeFailed

# ── NodeFailed Event Tests ────────────────────────────────────────────────────


class TestNodeFailedEvent:
    """Test NodeFailed event creation and field handling."""

    def test_minimal_event_required_fields(self):
        """NodeFailed with only required fields works (backward compatibility)."""
        event = NodeFailed(
            workflow_id="wf-123",
            node_name="research-planner",
            error="Connection refused",
            attempt=2,
        )
        assert event.node_name == "research-planner"
        assert event.error == "Connection refused"
        assert event.attempt == 2
        assert event.model is None
        assert event.error_category is None
        assert event.stderr_snippet is None
        assert event.duration_seconds is None
        assert event.node_phase is None

    def test_full_event_with_all_fields(self):
        """NodeFailed with all enriched fields populated."""
        event = NodeFailed(
            workflow_id="wf-456",
            node_name="fact-checker",
            error="Model rate limit exceeded",
            attempt=3,
            model="kimi-k2-thinking",
            error_category="ratelimit",
            stderr_snippet="HTTP 429: Too Many Requests\nRate limit: 100/min",
            duration_seconds=45.2,
            node_phase="verification",
        )
        assert event.model == "kimi-k2-thinking"
        assert event.error_category == "ratelimit"
        assert event.stderr_snippet.startswith("HTTP 429")
        assert event.duration_seconds == 45.2
        assert event.node_phase == "verification"

    def test_event_is_frozen(self):
        """NodeFailed events are immutable (frozen dataclass)."""
        event = NodeFailed(
            workflow_id="wf-789",
            node_name="synthesis-writer",
            error="timeout",
            attempt=1,
        )
        with pytest.raises(AttributeError):
            event.error = "changed"

    def test_event_serialization(self):
        """NodeFailed events can be serialized to JSON via to_json()."""
        event = NodeFailed(
            workflow_id="wf-001",
            node_name="search-executor",
            error="OOM",
            attempt=2,
            error_category="system",
            node_phase="execution",
        )
        json_str = event.to_json()
        assert "search-executor" in json_str
        assert "system" in json_str
        assert "execution" in json_str

    def test_event_from_dict_filters_unknown_keys(self):
        """from_dict() ignores unknown keys for backward compatibility."""
        event = NodeFailed.from_dict(
            {
                "workflow_id": "wf-002",
                "node_name": "planner",
                "error": "fail",
                "attempt": 1,
                "future_unknown_field": "should be ignored",
            }
        )
        assert event.node_name == "planner"


# ── Error Classification Tests ────────────────────────────────────────────────


class TestClassifyError:
    """Test _classify_error helper function."""

    def test_timeout_error(self):
        assert _classify_error(TimeoutError("timed out")) == "timeout"

    def test_async_timeout_error(self):
        assert _classify_error(TimeoutError()) == "timeout"

    def test_file_not_found_error(self):
        assert _classify_error(FileNotFoundError("missing.py")) == "system"

    def test_permission_error(self):
        assert _classify_error(PermissionError("denied")) == "system"

    def test_os_error(self):
        assert _classify_error(OSError("disk full")) == "system"

    def test_value_error(self):
        assert _classify_error(ValueError("bad config")) == "validation"

    def test_type_error(self):
        assert _classify_error(TypeError("wrong type")) == "validation"

    def test_connection_error(self):
        assert _classify_error(ConnectionError("refused")) == "connection"

    def test_runtime_error(self):
        assert _classify_error(RuntimeError("failed")) == "runtime"

    def test_rate_limit_in_message(self):
        """Rate limit patterns in error message are classified correctly."""
        assert _classify_error(Exception("rate limit exceeded")) == "ratelimit"
        assert _classify_error(Exception("HTTP 429 Too Many Requests")) == "ratelimit"
        assert _classify_error(Exception("quota exceeded for model")) == "ratelimit"

    def test_unknown_exception(self):
        """Unclassified exceptions default to 'unknown'."""
        assert _classify_error(Exception("something weird")) == "unknown"

    def test_key_error_is_unknown(self):
        """KeyError is not in the classification map."""
        assert _classify_error(KeyError("missing_key")) == "unknown"

    def test_attribute_error_is_unknown(self):
        assert _classify_error(AttributeError("no attr")) == "unknown"


# ── Phase Inference Tests ─────────────────────────────────────────────────────


class TestInferPhase:
    """Test _infer_phase and _infer_phase_from_skill helper functions."""

    def test_planning_phases(self):
        assert _infer_phase("research-planner") == "planning"
        assert _infer_phase("deep-planner") == "planning"
        assert _infer_phase("architecture-planner") == "planning"

    def test_execution_phases(self):
        assert _infer_phase("search-executor") == "execution"
        assert _infer_phase("code-implementer") == "execution"
        assert _infer_phase("bug-fixer") == "execution"

    def test_verification_phases(self):
        assert _infer_phase("fact-checker") == "verification"
        assert _infer_phase("security-auditor") == "verification"
        assert _infer_phase("code-validator") == "verification"
        assert _infer_phase("cvcp-attacker") == "verification"

    def test_synthesis_phases(self):
        assert _infer_phase("synthesis-writer") == "synthesis"
        assert _infer_phase("report-writer") == "synthesis"
        assert _infer_phase("summarizer") == "synthesis"

    def test_unknown_phase(self):
        assert _infer_phase("custom-agent") == "unknown"
        assert _infer_phase("random-name") == "unknown"

    def test_infer_phase_from_skill_matches(self):
        """_infer_phase_from_skill in nodes.py should match _infer_phase in agent_spawner.py."""
        for skill_name in [
            "research-planner",
            "search-executor",
            "fact-checker",
            "synthesis-writer",
            "deep-planner",
            "security-auditor",
        ]:
            assert _infer_phase(skill_name) == _infer_phase_from_skill(skill_name)
