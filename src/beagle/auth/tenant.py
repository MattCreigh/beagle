"""Tenant, User, and Role models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


@dataclass(frozen=True)
class Tenant:
    """A tenant/organization boundary."""

    tenant_id: str
    name: str = ""
    enabled: bool = True
    default_budget_usd: float = 0.0


@dataclass
class User:
    """Authenticated user with tenant-scoped role."""

    user_id: str
    tenant_id: str = "default"
    role: Role = Role.VIEWER
    email: str = ""
    claims: dict = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def can_read(self) -> bool:
        return self.role in (Role.ADMIN, Role.OPERATOR, Role.VIEWER)

    @property
    def can_write(self) -> bool:
        return self.role in (Role.ADMIN, Role.OPERATOR)

    @property
    def can_configure(self) -> bool:
        return self.role == Role.ADMIN
