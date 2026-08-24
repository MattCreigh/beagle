"""
Guardian Approval System

Inspired by codex's guardian system for tool execution safety.
Provides automated safety checks, approval flows, and policy enforcement.
"""

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any


class RiskLevel(Enum):
    """Risk levels for guardian approval."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalDecision(Enum):
    """Decision from guardian review."""

    APPROVED = "approved"
    DENIED = "denied"
    NEEDS_HUMAN = "needs_human"
    TIMEOUT = "timeout"
    CACHED = "cached"


@dataclass
class GuardianAction:
    """Represents an action requiring guardian approval."""

    action_type: str  # file_write, file_delete, network, subprocess, etc.
    description: str
    details: dict[str, Any] = field(default_factory=dict)
    risk_level: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    approval_timeout: int = 60  # seconds
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # For caching
    action_hash: str = ""

    def __post_init__(self):
        if not self.action_hash:
            self.action_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute deterministic hash for caching using secure SHA-256.

        Includes risk_level to prevent a cached LOW-risk approval from
        being reused in a HIGH-risk context (Golden Master Audit v13.8.1).
        """
        content = f"{self.action_type}:{self.description}:{self.risk_level.value}"
        if self.details:
            # Sort keys for deterministic serialization
            content += ":" + json.dumps(self.details, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:48]


@dataclass
class ApprovalResult:
    """Result of an approval request."""

    decision: ApprovalDecision
    action: GuardianAction
    reason: str = ""
    approved_at: datetime | None = None
    approved_by: str = "guardian"  # guardian, human, cached
    expires_at: datetime | None = None

    def is_approved(self) -> bool:
        """Check if action is approved."""
        return (
            self.decision == ApprovalDecision.APPROVED or self.decision == ApprovalDecision.CACHED
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "decision": self.decision.value,
            "action_hash": self.action.action_hash,
            "reason": self.reason,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "approved_by": self.approved_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class ApprovalPolicy:
    """Policy for automatic approval decisions."""

    # Risk level thresholds
    auto_approve_low: bool = True
    auto_approve_medium: bool = False
    auto_approve_high: bool = False
    auto_approve_critical: bool = False

    # Action-specific policies
    auto_approve_actions: set[str] = field(
        default_factory=lambda: {
            "file_read",
            "safe_edit",
            "git_status",
            "git_log",
            "ls",
            "pwd",
            "which",
            "echo",
        }
    )

    # Action-specific denials
    auto_deny_actions: set[str] = field(
        default_factory=lambda: {
            "file_delete",
            "rm",
            "sudo",
            "chmod",
            "chown",
        }
    )

    # Path restrictions
    protected_paths: set[str] = field(
        default_factory=lambda: {
            "/etc/passwd",
            "/etc/shadow",
            "/root",
            "~/.ssh",
            "~/.gnupg",
        }
    )

    # Cache settings
    cache_ttl_seconds: int = 300  # 5 minutes

    def evaluate(self, action: GuardianAction) -> ApprovalDecision:
        """
        Evaluate an action against the policy.

        Returns APPROVED, DENIED, or NEEDS_HUMAN
        """
        # Check for auto-deny first
        if action.action_type in self.auto_deny_actions:
            return ApprovalDecision.DENIED

        # Check protected paths (v0.3.0: resolve symlinks + traversal)
        if "path" in action.details:
            path = os.path.realpath(os.path.expanduser(action.details["path"]))
            # D14 (Fable 5 DD 2026-06-11): the previous `path.startswith(p)`
            # check is separator-less, so /root matches /rootful-data. Append
            # os.sep to the protected path so the prefix match is a proper
            # directory boundary.
            if any(
                path == os.path.realpath(os.path.expanduser(p))
                or path.startswith(os.path.realpath(os.path.expanduser(p)) + os.sep)
                for p in self.protected_paths
            ):
                return ApprovalDecision.NEEDS_HUMAN

        # Check for auto-approve
        if action.action_type in self.auto_approve_actions:
            return ApprovalDecision.APPROVED

        # Check risk levels
        if action.risk_level == RiskLevel.LOW and self.auto_approve_low:
            return ApprovalDecision.APPROVED
        if action.risk_level == RiskLevel.MEDIUM and self.auto_approve_medium:
            return ApprovalDecision.APPROVED
        if action.risk_level == RiskLevel.HIGH and self.auto_approve_high:
            return ApprovalDecision.APPROVED
        if action.risk_level == RiskLevel.CRITICAL and self.auto_approve_critical:
            return ApprovalDecision.APPROVED

        # Default to needing human review
        return ApprovalDecision.NEEDS_HUMAN


class ApprovalCache:
    """Cache of recent approvals to avoid repeated reviews."""

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._cache: dict[str, ApprovalResult] = {}

    def get(self, action_hash: str) -> ApprovalResult | None:
        """Get cached approval if still valid.

        FIX: Proactively evict expired entries to prevent memory leaks (Audit Item #26).
        """
        now = datetime.now(UTC)

        # Proactive eviction sweep
        expired_keys = [k for k, v in self._cache.items() if v.expires_at and now >= v.expires_at]
        for k in expired_keys:
            del self._cache[k]

        if action_hash in self._cache:
            result = self._cache[action_hash]
            return ApprovalResult(
                decision=ApprovalDecision.CACHED,
                action=result.action,
                reason="Cached approval",
                approved_by="cached",
            )
        return None

    def set(self, result: ApprovalResult) -> None:
        """Cache an approval result."""
        result.expires_at = datetime.now(UTC) + timedelta(seconds=self._ttl)
        self._cache[result.action.action_hash] = result

    def clear(self) -> None:
        """Clear the cache."""
        self._cache.clear()


class Guardian:
    """
    Guardian approval system for safe tool execution.

    Provides:
    - Automatic policy-based approvals
    - Action caching to avoid repeated reviews
    - Risk assessment
    - Audit logging
    """

    def __init__(
        self,
        policy: ApprovalPolicy | None = None,
        cache_dir: Path | None = None,
        auto_handler: Callable[[GuardianAction], ApprovalDecision] | None = None,
    ):
        self.policy = policy or ApprovalPolicy()
        self._cache = ApprovalCache(ttl_seconds=self.policy.cache_ttl_seconds)
        # Use centralized cache directory if available
        try:
            from ..config.paths import get_file_cache_dir

            self._cache_dir = get_file_cache_dir() / "guardian"
        except ImportError:
            self._cache_dir = Path.home() / ".cache" / "beagle" / "guardian"

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._audit_log: list[dict[str, Any]] = []
        self._auto_handler = auto_handler

    def assess_risk(self, action: GuardianAction) -> RiskLevel:
        """
        Assess the risk level of an action.
        """
        # Already has a risk level set
        if action.risk_level != RiskLevel.LOW:
            return action.risk_level

        # Default risk assessment
        high_risk_actions = {"file_delete", "rm", "sudo", "chmod", "chown", "kill"}
        medium_risk_actions = {"file_write", "subprocess", "network", "curl", "wget"}

        if action.action_type in high_risk_actions:
            return RiskLevel.HIGH

        if action.action_type in medium_risk_actions:
            return RiskLevel.MEDIUM

        return RiskLevel.LOW

    def check_approval(self, action: GuardianAction) -> ApprovalResult:
        """
        Check if an action needs approval and process it.
        """
        # Check cache first
        cached = self._cache.get(action.action_hash)
        if cached:
            self._log_action(action, cached)
            return cached

        # Assess risk if not set
        if action.risk_level == RiskLevel.LOW:
            action.risk_level = self.assess_risk(action)

        # Evaluate against policy
        decision = self.policy.evaluate(action)

        # Call auto handler if available
        if self._auto_handler and decision == ApprovalDecision.NEEDS_HUMAN:
            decision = self._auto_handler(action)

        # Create result
        now = datetime.now(UTC)
        if decision == ApprovalDecision.APPROVED:
            result = ApprovalResult(
                decision=decision,
                action=action,
                reason="Auto-approved by policy",
                approved_at=now,
                approved_by="guardian",
            )
            # Cache the approval
            self._cache.set(result)
        elif decision == ApprovalDecision.DENIED:
            result = ApprovalResult(
                decision=decision,
                action=action,
                reason=f"Action '{action.action_type}' is auto-denied by policy",
            )
        else:
            result = ApprovalResult(
                decision=ApprovalDecision.NEEDS_HUMAN,
                action=action,
                reason="Action requires human review",
            )

        self._log_action(action, result)
        return result

    def approve_manually(
        self, action: GuardianAction, reason: str = "", approved_by: str = "human"
    ) -> ApprovalResult:
        """Manually approve an action."""
        result = ApprovalResult(
            decision=ApprovalDecision.APPROVED,
            action=action,
            reason=reason,
            approved_at=datetime.now(UTC),
            approved_by=approved_by,
        )
        self._cache.set(result)
        self._log_action(action, result)
        return result

    def deny_manually(self, action: GuardianAction, reason: str = "") -> ApprovalResult:
        """Manually deny an action."""
        result = ApprovalResult(
            decision=ApprovalDecision.DENIED,
            action=action,
            reason=reason,
            approved_by="human",
        )
        self._log_action(action, result)
        return result

    def can_proceed(self, action: GuardianAction) -> bool:
        """Quick check if an action can proceed."""
        result = self.check_approval(action)
        return result.is_approved()

    def _log_action(self, action: GuardianAction, result: ApprovalResult) -> None:
        """Log action to audit trail."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action_type": action.action_type,
            "action_hash": action.action_hash,
            "description": action.description,
            "risk_level": action.risk_level.value,
            "decision": result.decision.value,
            "reason": result.reason,
            "approved_by": result.approved_by,
        }
        self._audit_log.append(entry)
        # Cap audit log to prevent unbounded memory growth
        if len(self._audit_log) > 10000:
            self._audit_log = self._audit_log[-5000:]

    def get_audit_log(self) -> list[dict[str, Any]]:
        """Get the full audit log."""
        return self._audit_log.copy()

    def save_audit_log(self) -> Path:
        """Save audit log to disk."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        path = self._cache_dir / f"audit_{timestamp}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._audit_log, f, indent=2)
        return path

    def clear_cache(self) -> None:
        """Clear the approval cache."""
        self._cache.clear()


# Global guardian instance
_guardian: Guardian | None = None


def get_guardian() -> Guardian:
    """Get the global guardian instance."""
    global _guardian
    if _guardian is None:
        _guardian = Guardian()
    return _guardian


def set_guardian(guardian: Guardian) -> None:
    """Set the global guardian instance."""
    global _guardian
    _guardian = guardian


def check_approval(action: GuardianAction) -> ApprovalResult:
    """Convenience function to check approval with global guardian."""
    return get_guardian().check_approval(action)


def can_proceed(action: GuardianAction) -> bool:
    """Convenience function to check if action can proceed."""
    return get_guardian().can_proceed(action)
