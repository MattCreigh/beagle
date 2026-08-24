"""Tests for auth package."""

from __future__ import annotations

from beagle.auth.jwt import create_jwt, validate_jwt
from beagle.auth.rbac import RBACEnforcer
from beagle.auth.tenant import Role, User


def test_user_permissions():
    admin = User(user_id="a", role=Role.ADMIN)
    assert admin.is_admin is True
    assert admin.can_read is True
    assert admin.can_write is True
    assert admin.can_configure is True

    viewer = User(user_id="v", role=Role.VIEWER)
    assert viewer.can_read is True
    assert viewer.can_write is False


def test_rbac_disabled():
    # Security baseline: RBAC is fail-closed even when disabled.
    rbac = RBACEnforcer(enabled=False)
    user = User(user_id="u1")
    assert rbac.enforce(user, "workflows", "execute") is False
    assert rbac.enforce(user, "config", "delete") is False


def test_rbac_builtin():
    rbac = RBACEnforcer(enabled=True)
    admin = User(user_id="a", role=Role.ADMIN)
    assert rbac.enforce(admin, "workflows", "execute") is True
    assert rbac.enforce(admin, "workflows", "delete") is True
    viewer = User(user_id="v", role=Role.VIEWER)
    assert rbac.enforce(viewer, "workflows", "execute") is False


def test_jwt_roundtrip():
    secret = "test-jwt-secret-32-bytes-long-ok"
    token = create_jwt({"sub": "u1", "role": "admin", "tenant_id": "t1"}, secret)
    payload = validate_jwt(token, secret)
    assert payload["sub"] == "u1"
    assert payload["role"] == "admin"


def test_rbac_jwt_extract_user():
    rbac = RBACEnforcer(enabled=True)
    secret = "test-jwt-secret-32-bytes-long-ok"
    token = create_jwt({"sub": "u2", "role": "operator", "tenant_id": "t2"}, secret)
    user = rbac.check_jwt_and_extract_user(token, secret)
    assert user is not None
    assert user.role == Role.OPERATOR
    assert user.tenant_id == "t2"

    bad = rbac.check_jwt_and_extract_user("bad.token.here", secret)
    assert bad is None
