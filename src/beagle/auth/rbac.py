"""RBAC with domain-scoped tenant policies.

Lightweight built-in fallback; uses Casbin if available.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from .tenant import Role, User

logger = logging.getLogger("Beagle.auth.rbac")


class _BuiltinRBAC:
    """Hard-coded permission matrix. No external dependencies."""

    _perms: ClassVar[dict[str, dict[str, list[str]]]] = {
        "admin": {
            "*": ["get", "post", "put", "delete", "execute"],
        },
        "operator": {
            "*": ["get", "post", "execute"],
        },
        "viewer": {
            "*": ["get"],
        },
    }

    def enforce(self, user: User, domain: str, action: str) -> bool:
        perms = self._perms.get(user.role.value, {})
        domain_perms = perms.get(domain, perms.get("*", []))
        return action in domain_perms or "*" in domain_perms


class RBACEnforcer:
    """RBAC enforcer with Casbin fallback.

        Backward compatible: auth.enabled=false means
    tenant_id="default", role=admin, all actions permitted.
    """

    def __init__(self, enabled: bool = False) -> None:
        self._enabled = enabled
        self._casbin_enforcer: Any | None = None
        self._builtin = _BuiltinRBAC()
        if enabled:
            try:
                import casbin

                self._casbin_enforcer = casbin.Enforcer(
                    "beagle/auth/model.conf",
                    "beagle/auth/policy.csv",
                )
                logger.info("Casbin RBAC initialized")
            except (ImportError, OSError):
                logger.warning("Casbin unavailable, using built-in RBAC fallback")

    def enforce(self, user: User, domain: str, action: str) -> bool:
        """Check if user may perform action in domain.

        Security baseline: RBAC is fail-closed. When disabled the enforcer
        denies every action and logs a WARNING. Callers that need a
        permissive no-op must explicitly set enabled=True and configure
        an allow-all policy, not rely on the disabled state.
        """
        if not self._enabled:
            # Fail-closed: disabled RBAC denies all actions.
            logger.warning(
                "RBAC DENIED (fail-closed): user=%s domain=%s action=%s — "
                "RBAC is disabled. Enable RBAC in config.toml [auth] for production.",
                user.user_id,
                domain,
                action,
            )
            return False
        if self._casbin_enforcer is not None:
            result = self._casbin_enforcer.enforce(user.user_id, domain, action, user.tenant_id)
            return bool(result)
        return bool(self._builtin.enforce(user, domain, action))

    def check_jwt_and_extract_user(self, token: str, jwt_secret: str) -> User | None:
        """Decode a JWT token and extract user information.

        Uses the project's ``validate_jwt()`` for proper error
        discrimination (expired vs. forged vs. malformed). Returns None
        for invalid tokens but logs the failure category at WARNING.
        """
        from .jwt import validate_jwt

        try:
            payload = validate_jwt(token, jwt_secret)
        except ValueError as exc:
            # validate_jwt raises ValueError subclasses for specific failures:
            # - ExpiredSignatureError → "JWT token has expired"
            # - MissingRequiredClaimError → "JWT missing required claim: ..."
            # - InvalidTokenError/DecodeError → "JWT validation failed: ..."
            logger.warning("JWT validation failed: %s", exc)
            return None
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional — last-resort guard: any uncaught JWT lib failure must NOT 500 the caller
            logger.warning("JWT validation unexpected error: %s", exc)
            return None

        return User(
            user_id=payload.get("sub", ""),
            tenant_id=payload.get("tenant_id", "default"),
            role=Role(payload.get("role", "viewer")),
            email=payload.get("email", ""),
            claims=payload,
        )
