"""Multi-tenancy + RBAC package for Beagle.

Lightweight tenant-aware auth with optional Casbin fallback.
Defaults to disabled (enabled=false in config.toml [auth] section).
"""

from .jwt import validate_jwt
from .rbac import RBACEnforcer, Role
from .tenant import Tenant, User

__all__ = ["RBACEnforcer", "Role", "Tenant", "User", "validate_jwt"]
