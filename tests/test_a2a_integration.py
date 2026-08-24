"""Tests for A2A integration with the DAG orchestrator.

Validates message signing, verification, strict mode rejection,
delegation creation, and orchestrator integration.
"""

import time

from beagle.core.a2a_integration import (
    configure_a2a,
    is_a2a_enabled,
    sign_delegation,
    verify_agent_result,
)


class TestA2AConfiguration:
    """Tests for A2A configuration."""

    def test_enable_a2a(self):
        configure_a2a(enabled=True)
        assert is_a2a_enabled() is True

    def test_disable_a2a(self):
        configure_a2a(enabled=False)
        assert is_a2a_enabled() is False

    def test_strict_mode(self):
        configure_a2a(enabled=True, require_signatures=True)
        assert is_a2a_enabled() is True

    def test_non_strict_mode(self):
        configure_a2a(enabled=True, require_signatures=False)
        assert is_a2a_enabled() is True


class TestDelegationSigning:
    """Tests for delegation message signing."""

    def setup_method(self):
        configure_a2a(enabled=True, require_signatures=False)

    def test_sign_delegation_returns_dict(self):
        result = sign_delegation(
            workflow_id="wf-123",
            agent_id="spawn-abc",
            task_description="Research topic X",
        )
        assert isinstance(result, dict)
        assert result["workflow_id"] == "wf-123"
        assert result["agent_id"] == "spawn-abc"
        assert result["task"] == "Research topic X"

    def test_sign_delegation_includes_signature(self):
        result = sign_delegation(
            workflow_id="wf-123",
            agent_id="spawn-abc",
            task_description="Research topic X",
        )
        assert "signature" in result
        assert isinstance(result["signature"], str)
        assert len(result["signature"]) == 64  # HMAC-SHA256 hex

    def test_sign_delegation_includes_timestamp(self):
        result = sign_delegation(
            workflow_id="wf-123",
            agent_id="spawn-abc",
            task_description="Research topic X",
        )
        assert "timestamp" in result
        # Timestamp should be close to now
        assert abs(int(result["timestamp"]) - int(time.time())) < 5

    def test_sign_delegation_includes_permissions(self):
        result = sign_delegation(
            workflow_id="wf-123",
            agent_id="spawn-abc",
            task_description="Research topic X",
            permissions=["read", "search"],
        )
        assert result["permissions"] == ["read", "search"]

    def test_sign_delegation_a2a_version(self):
        result = sign_delegation(
            workflow_id="wf-123",
            agent_id="spawn-abc",
            task_description="Research topic X",
        )
        assert result.get("a2a_version") == "1.0"

    def test_different_messages_different_signatures(self):
        result1 = sign_delegation("wf-1", "a1", "Task A")
        result2 = sign_delegation("wf-2", "a2", "Task B")
        assert result1["signature"] != result2["signature"]


class TestResultVerification:
    """Tests for agent result verification."""

    def setup_method(self):
        configure_a2a(enabled=True, require_signatures=False)

    def test_verify_signed_result(self):
        delegation = sign_delegation("wf-123", "spawn-abc", "Research topic X")
        # Use the signed result for verification
        assert verify_agent_result(delegation) is True

    def test_accept_unsigned_result_when_not_strict(self):
        configure_a2a(enabled=True, require_signatures=False)
        result = {
            "workflow_id": "wf-123",
            "agent_id": "spawn-abc",
            "result": "Done",
        }
        assert verify_agent_result(result) is True

    def test_reject_unsigned_result_in_strict_mode(self):
        configure_a2a(enabled=True, require_signatures=True)
        result = {
            "workflow_id": "wf-123",
            "agent_id": "spawn-abc",
            "result": "Done",
        }
        assert verify_agent_result(result) is False

    def test_accept_signed_result_in_strict_mode(self):
        configure_a2a(enabled=True, require_signatures=True)
        delegation = sign_delegation("wf-123", "spawn-abc", "Research topic X")
        assert verify_agent_result(delegation) is True

    def test_reject_tampered_result(self):
        delegation = sign_delegation("wf-123", "spawn-abc", "Research topic X")
        # Tamper with the result
        delegation["task"] = "DIFFERENT TASK"
        assert verify_agent_result(delegation) is False

    def test_verify_with_strict_override(self):
        configure_a2a(enabled=True, require_signatures=False)
        unsigned_result = {"agent_id": "a1", "result": "Done"}
        # Override strict for this one check
        assert verify_agent_result(unsigned_result, strict=True) is False


class TestA2ADisabled:
    """Tests for behavior when A2A is disabled."""

    def setup_method(self):
        configure_a2a(enabled=False)

    def test_unsigned_result_accepted_when_disabled(self):
        result = {"agent_id": "a1", "result": "Done"}
        assert verify_agent_result(result) is True

    def test_delegation_not_signed_when_disabled(self):
        result = sign_delegation("wf-123", "spawn-abc", "Research topic X")
        # When disabled, no signature is added
        assert "signature" not in result


class TestDelegationRoundtrip:
    """Integration test: sign delegation, verify result."""

    def setup_method(self):
        configure_a2a(enabled=True, require_signatures=True)

    def test_full_sign_verify_roundtrip(self):
        # Sign a delegation
        delegation = sign_delegation(
            workflow_id="wf-roundtrip",
            agent_id="spawn-test",
            task_description="Perform analysis",
            permissions=["read", "search"],
        )

        # The signed delegation includes all original data plus signature
        assert delegation["workflow_id"] == "wf-roundtrip"
        assert delegation["agent_id"] == "spawn-test"
        assert delegation["task"] == "Perform analysis"
        assert delegation["permissions"] == ["read", "search"]
        assert "signature" in delegation

        # Verify the signed result
        assert verify_agent_result(delegation) is True

        # Tamper detection
        tampered = dict(delegation)
        tampered["task"] = "DIFFERENT TASK"
        assert verify_agent_result(tampered) is False
