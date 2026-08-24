"""SP-5: tests for auth/tenant (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The tenant/user/role models had no
direct tests. These exercise Role values, User permission properties, and
Tenant construction.
"""

from __future__ import annotations

import pytest

from beagle.auth.tenant import Role, Tenant, User


def test_role_values() -> None:
    """Role StrEnum values are stable strings."""
    assert Role.ADMIN == "admin"
    assert Role.OPERATOR == "operator"
    assert Role.VIEWER == "viewer"


def test_tenant_defaults() -> None:
    """Tenant has a name, enabled=True, and zero default budget."""
    t = Tenant(tenant_id="t1")
    assert t.name == ""
    assert t.enabled is True
    assert t.default_budget_usd == 0.0


def test_tenant_full() -> None:
    """Tenant accepts all fields."""
    t = Tenant(tenant_id="t1", name="Acme", enabled=False, default_budget_usd=5.0)
    assert t.name == "Acme"
    assert t.enabled is False
    assert t.default_budget_usd == 5.0


def test_user_default_role_is_viewer() -> None:
    """User defaults to VIEWER role and default tenant."""
    u = User(user_id="u1")
    assert u.role == Role.VIEWER
    assert u.tenant_id == "default"


def test_user_permission_properties() -> None:
    """Permission properties follow the role matrix."""
    admin = User(user_id="a", role=Role.ADMIN)
    operator = User(user_id="o", role=Role.OPERATOR)
    viewer = User(user_id="v", role=Role.VIEWER)

    assert admin.is_admin is True
    assert operator.is_admin is False
    assert admin.can_read and admin.can_write and admin.can_configure
    assert operator.can_read and operator.can_write
    assert operator.can_configure is False
    assert viewer.can_read
    assert viewer.can_write is False
    assert viewer.can_configure is False


def test_user_frozen_role_and_claims() -> None:
    """User carries email and claims dict."""
    u = User(user_id="u1", email="a@b", claims={"scopes": ["read"]})
    assert u.email == "a@b"
    assert u.claims == {"scopes": ["read"]}


def test_tenant_is_frozen() -> None:
    """Tenant is a frozen dataclass (immutable)."""
    from dataclasses import FrozenInstanceError

    t = Tenant(tenant_id="t1")
    with pytest.raises(FrozenInstanceError):
        t.name = "changed"
