"""Tests for beagle.guardian — Guardian types, enums, policies, and approval logic."""

from __future__ import annotations

from beagle.guardian import (
    ApprovalCache,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalResult,
    Guardian,
    GuardianAction,
    RiskLevel,
    can_proceed,
    check_approval,
    get_guardian,
    set_guardian,
)

# ── Enum and type definitions ──────────────────────────────────────────────


class TestRiskLevel:
    """RiskLevel enum values."""

    def test_risk_levels_exist(self):
        assert RiskLevel.LOW.value == "low"
        assert RiskLevel.MEDIUM.value == "medium"
        assert RiskLevel.HIGH.value == "high"
        assert RiskLevel.CRITICAL.value == "critical"

    def test_risk_level_members_count(self):
        assert len(RiskLevel) == 4


class TestApprovalDecision:
    """ApprovalDecision enum values."""

    def test_approval_decisions_exist(self):
        assert ApprovalDecision.APPROVED.value == "approved"
        assert ApprovalDecision.DENIED.value == "denied"
        assert ApprovalDecision.NEEDS_HUMAN.value == "needs_human"
        assert ApprovalDecision.TIMEOUT.value == "timeout"
        assert ApprovalDecision.CACHED.value == "cached"

    def test_approval_decision_members_count(self):
        assert len(ApprovalDecision) == 5


# ── GuardianAction ─────────────────────────────────────────────────────────


class TestGuardianAction:
    """GuardianAction dataclass and hashing."""

    def test_action_creation_defaults(self):
        action = GuardianAction(action_type="file_read", description="Read a config file")
        assert action.action_type == "file_read"
        assert action.description == "Read a config file"
        assert action.risk_level == RiskLevel.LOW
        assert action.requires_approval is False
        assert action.approval_timeout == 60

    def test_action_hash_computed_automatically(self):
        action = GuardianAction(action_type="file_read", description="Read a config file")
        assert action.action_hash != ""
        assert len(action.action_hash) == 48  # SHA-256 truncated to 48 hex chars

    def test_action_hash_is_deterministic(self):
        action1 = GuardianAction(action_type="file_write", description="Write code")
        action2 = GuardianAction(action_type="file_write", description="Write code")
        assert action1.action_hash == action2.action_hash

    def test_action_hash_differs_for_different_actions(self):
        action1 = GuardianAction(action_type="file_write", description="Write code")
        action2 = GuardianAction(action_type="file_read", description="Read code")
        assert action1.action_hash != action2.action_hash

    def test_action_details_included_in_hash(self):
        action1 = GuardianAction(
            action_type="file_write", description="Write", details={"path": "/a"}
        )
        action2 = GuardianAction(
            action_type="file_write", description="Write", details={"path": "/b"}
        )
        assert action1.action_hash != action2.action_hash

    def test_action_high_risk(self):
        action = GuardianAction(
            action_type="file_delete",
            description="Delete file",
            risk_level=RiskLevel.HIGH,
        )
        assert action.risk_level == RiskLevel.HIGH


# ── ApprovalResult ─────────────────────────────────────────────────────────


class TestApprovalResult:
    """ApprovalResult dataclass and methods."""

    def test_approved_is_approved(self):
        action = GuardianAction(action_type="file_read", description="test")
        result = ApprovalResult(decision=ApprovalDecision.APPROVED, action=action)
        assert result.is_approved() is True

    def test_cached_is_approved(self):
        action = GuardianAction(action_type="file_read", description="test")
        result = ApprovalResult(decision=ApprovalDecision.CACHED, action=action)
        assert result.is_approved() is True

    def test_denied_not_approved(self):
        action = GuardianAction(action_type="file_delete", description="test")
        result = ApprovalResult(decision=ApprovalDecision.DENIED, action=action)
        assert result.is_approved() is False

    def test_needs_human_not_approved(self):
        action = GuardianAction(action_type="subprocess", description="test")
        result = ApprovalResult(decision=ApprovalDecision.NEEDS_HUMAN, action=action)
        assert result.is_approved() is False

    def test_to_dict(self):
        action = GuardianAction(action_type="file_read", description="test")
        result = ApprovalResult(
            decision=ApprovalDecision.APPROVED,
            action=action,
            reason="Auto-approved",
            approved_by="guardian",
        )
        d = result.to_dict()
        assert d["decision"] == "approved"
        assert d["reason"] == "Auto-approved"
        assert d["approved_by"] == "guardian"


# ── ApprovalPolicy ─────────────────────────────────────────────────────────


class TestApprovalPolicy:
    """ApprovalPolicy evaluation logic."""

    def test_default_policy_auto_approve_low(self):
        policy = ApprovalPolicy()
        action = GuardianAction(
            action_type="custom_action", description="test", risk_level=RiskLevel.LOW
        )
        assert policy.evaluate(action) == ApprovalDecision.APPROVED

    def test_default_policy_needs_human_medium(self):
        policy = ApprovalPolicy()
        action = GuardianAction(
            action_type="custom_action", description="test", risk_level=RiskLevel.MEDIUM
        )
        assert policy.evaluate(action) == ApprovalDecision.NEEDS_HUMAN

    def test_default_policy_deny_dangerous_actions(self):
        policy = ApprovalPolicy()
        action = GuardianAction(action_type="file_delete", description="Delete")
        assert policy.evaluate(action) == ApprovalDecision.DENIED

    def test_default_policy_approve_safe_actions(self):
        policy = ApprovalPolicy()
        action = GuardianAction(action_type="file_read", description="Read")
        assert policy.evaluate(action) == ApprovalDecision.APPROVED

    def test_auto_approve_actions_set(self):
        policy = ApprovalPolicy()
        assert "file_read" in policy.auto_approve_actions
        assert "safe_edit" in policy.auto_approve_actions

    def test_auto_deny_actions_set(self):
        policy = ApprovalPolicy()
        assert "file_delete" in policy.auto_deny_actions
        assert "sudo" in policy.auto_deny_actions

    def test_protected_paths(self):
        policy = ApprovalPolicy()
        action = GuardianAction(
            action_type="file_read",
            description="Read",
            details={"path": "/etc/passwd"},
        )
        assert policy.evaluate(action) == ApprovalDecision.NEEDS_HUMAN

    def test_custom_policy_medium_approval(self):
        policy = ApprovalPolicy(auto_approve_medium=True)
        action = GuardianAction(
            action_type="custom", description="test", risk_level=RiskLevel.MEDIUM
        )
        assert policy.evaluate(action) == ApprovalDecision.APPROVED


# ── ApprovalCache ──────────────────────────────────────────────────────────


class TestApprovalCache:
    """ApprovalCache set, get, and eviction."""

    def test_cache_miss_returns_none(self):
        cache = ApprovalCache()
        assert cache.get("nonexistent") is None

    def test_cache_set_and_get(self):
        cache = ApprovalCache()
        action = GuardianAction(action_type="file_read", description="test")
        result = ApprovalResult(decision=ApprovalDecision.APPROVED, action=action, reason="ok")
        cache.set(result)
        cached = cache.get(action.action_hash)
        assert cached is not None
        assert cached.decision == ApprovalDecision.CACHED

    def test_cache_clear(self):
        cache = ApprovalCache()
        action = GuardianAction(action_type="file_read", description="test")
        result = ApprovalResult(decision=ApprovalDecision.APPROVED, action=action, reason="ok")
        cache.set(result)
        cache.clear()
        assert cache.get(action.action_hash) is None


# ── Guardian class ─────────────────────────────────────────────────────────


class TestGuardian:
    """Guardian approval system end-to-end."""

    def test_guardian_creation(self):
        guardian = Guardian()
        assert guardian.policy is not None

    def test_guardian_auto_approve_safe_action(self):
        guardian = Guardian()
        action = GuardianAction(action_type="file_read", description="Read config")
        result = guardian.check_approval(action)
        assert result.is_approved()

    def test_guardian_auto_deny_dangerous_action(self):
        guardian = Guardian()
        action = GuardianAction(action_type="rm", description="Remove files")
        result = guardian.check_approval(action)
        assert result.decision == ApprovalDecision.DENIED

    def test_guardian_needs_human_for_medium_risk(self):
        guardian = Guardian()
        action = GuardianAction(
            action_type="subprocess", description="Run command", risk_level=RiskLevel.MEDIUM
        )
        result = guardian.check_approval(action)
        assert result.decision == ApprovalDecision.NEEDS_HUMAN

    def test_guardian_manual_approve(self):
        guardian = Guardian()
        action = GuardianAction(action_type="file_write", description="Write code")
        result = guardian.approve_manually(action, reason="Approved by admin")
        assert result.is_approved()
        assert result.approved_by == "human"

    def test_guardian_manual_deny(self):
        guardian = Guardian()
        action = GuardianAction(action_type="file_write", description="Write code")
        result = guardian.deny_manually(action, reason="Blocked")
        assert result.decision == ApprovalDecision.DENIED

    def test_guardian_can_proceed(self):
        guardian = Guardian()
        action = GuardianAction(action_type="file_read", description="Read")
        assert guardian.can_proceed(action) is True

    def test_guardian_can_proceed_denied(self):
        guardian = Guardian()
        action = GuardianAction(action_type="sudo", description="Run as root")
        assert guardian.can_proceed(action) is False

    def test_guardian_assess_risk_high(self):
        guardian = Guardian()
        action = GuardianAction(action_type="file_delete", description="Delete")
        risk = guardian.assess_risk(action)
        assert risk == RiskLevel.HIGH

    def test_guardian_assess_risk_medium(self):
        guardian = Guardian()
        action = GuardianAction(action_type="network", description="HTTP request")
        risk = guardian.assess_risk(action)
        assert risk == RiskLevel.MEDIUM

    def test_guardian_assess_risk_low(self):
        guardian = Guardian()
        action = GuardianAction(action_type="ls", description="List directory")
        risk = guardian.assess_risk(action)
        assert risk == RiskLevel.LOW

    def test_guardian_audit_log(self):
        guardian = Guardian()
        action = GuardianAction(action_type="file_read", description="Read")
        guardian.check_approval(action)
        log = guardian.get_audit_log()
        assert len(log) >= 1
        assert log[0]["action_type"] == "file_read"

    def test_guardian_caching(self):
        guardian = Guardian()
        action = GuardianAction(action_type="file_read", description="Read")
        _result1 = guardian.check_approval(action)
        result2 = guardian.check_approval(action)
        assert result2.decision == ApprovalDecision.CACHED


# ── Module-level convenience functions ──────────────────────────────────────


class TestGuardianGlobals:
    """Module-level get_guardian, set_guardian, check_approval, can_proceed."""

    def test_get_guardian_returns_guardian(self):
        guardian = get_guardian()
        assert isinstance(guardian, Guardian)

    def test_set_guardian(self):
        custom = Guardian(policy=ApprovalPolicy(auto_approve_medium=True))
        set_guardian(custom)
        assert get_guardian() is custom
        # Reset
        set_guardian(Guardian())

    def test_check_approval_convenience(self):
        action = GuardianAction(action_type=" pwd", description="Show path")
        result = check_approval(action)
        assert result.decision in (
            ApprovalDecision.APPROVED,
            ApprovalDecision.NEEDS_HUMAN,
            ApprovalDecision.DENIED,
        )

    def test_can_proceed_convenience(self):
        action = GuardianAction(action_type="ls", description="List")
        result = can_proceed(action)
        assert isinstance(result, bool)
