"""Tests for Beagle v13.4 features: OIDC/RBAC, MicroVM, OTEL tracing."""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# OIDC Verifier Tests
# ---------------------------------------------------------------------------


class TestOIDCVerifier:
    """Test OIDC token verification for A2A zero-trust."""

    def test_import(self):
        from beagle.core.a2a_protocol import OIDCVerifier

        assert OIDCVerifier is not None

    def test_verify_empty_token(self):
        from beagle.core.a2a_protocol import OIDCVerifier

        verifier = OIDCVerifier(issuer="", audience="beagle-a2a")
        assert verifier.verify("") is None

    def test_verify_none_token(self):
        from beagle.core.a2a_protocol import OIDCVerifier

        verifier = OIDCVerifier()
        assert verifier.verify(None) is None

    def test_generate_and_verify_local_token(self):
        from beagle.core.a2a_protocol import OIDCVerifier

        verifier = OIDCVerifier(audience="beagle-a2a")
        token = verifier.generate_local_token("agent-001", {"role": "admin"})
        assert token is not None
        assert "." in token  # payload.signature format

        claims = verifier.verify(token)
        assert claims is not None
        assert claims["sub"] == "agent-001"
        assert claims["aud"] == "beagle-a2a"
        assert claims["role"] == "admin"
        assert "exp" in claims

    def test_reject_tampered_token(self):
        from beagle.core.a2a_protocol import OIDCVerifier

        verifier = OIDCVerifier(audience="beagle-a2a")
        token = verifier.generate_local_token("agent-001")
        # Tamper with the payload
        parts = token.split(".")
        tampered = parts[0] + "XX." + parts[1]
        assert verifier.verify(tampered) is None

    def test_reject_wrong_audience(self):
        from beagle.core.a2a_protocol import OIDCVerifier

        verifier = OIDCVerifier(audience="beagle-a2a")
        token = verifier.generate_local_token("agent-001")
        # Verify with wrong audience
        wrong_verifier = OIDCVerifier(audience="wrong-audience")
        assert wrong_verifier.verify(token) is None

    def test_reject_expired_token(self):
        import base64

        from beagle.core.a2a_protocol import OIDCVerifier, _compute_hmac

        verifier = OIDCVerifier(audience="beagle-a2a")

        # Craft an expired token manually
        payload = {
            "sub": "agent-001",
            "aud": "beagle-a2a",
            "iat": 1000000,
            "exp": 1000001,  # Expired long ago
        }
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        signature = _compute_hmac(payload_b64)
        expired_token = f"{payload_b64}.{signature}"

        assert verifier.verify(expired_token) is None


# ---------------------------------------------------------------------------
# RBAC Policy Tests
# ---------------------------------------------------------------------------


class TestRBACPolicy:
    """Test RBAC authorization for A2A protocol."""

    def test_import(self):
        from beagle.core.a2a_protocol import RBACPolicy

        assert RBACPolicy is not None

    def test_default_roles(self):
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        # Default 'observer' role should allow 'get' only
        assert policy.check("unknown-agent", "a2a:get", "some-agent") is True
        assert policy.check("unknown-agent", "a2a:register", "some-agent") is False
        assert policy.check("unknown-agent", "a2a:send", "some-agent") is False

    def test_admin_role_all_access(self):
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        policy.bind_role("admin-1", "admin")
        assert policy.check("admin-1", "a2a:register", "any-agent") is True
        assert policy.check("admin-1", "a2a:send", "any-agent") is True
        assert policy.check("admin-1", "anything", "everything") is True

    def test_no_implicit_admin_for_unbound_identity(self):
        """C02: deny-by-default — admin wildcard is NOT implicit.

        The shipped 'admin' role is bound to *:* but ONLY for identities that
        were explicitly bound to it. An unbound identity must default to the
        least-privileged 'observer' and can never reach admin privileges
        without an explicit binding. This guards against a deployment that
        relies on a never-declared default admin.
        """
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        # Unbound identity defaults to observer — NOT admin.
        assert policy.get_role("never-bound") == "observer"
        # An unbound identity must not reach any admin-only action.
        for privileged_action in ("a2a:register", "a2a:send", "a2a:cancel"):
            assert policy.check("never-bound", privileged_action, "any-agent") is False, (
                f"unbound identity must not perform {privileged_action}"
            )

    def test_admin_is_least_privileged_by_default_after_unbind(self):
        """C02: after unbinding an admin, the identity loses ALL privileges.

        Unbinding must return the identity to the deny-by-default observer
        posture — there is no residual admin capability retained from the
        prior binding.
        """
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        policy.bind_role("ex-admin", "admin")
        assert policy.check("ex-admin", "a2a:register", "any-agent") is True
        policy.unbind("ex-admin")
        assert policy.get_role("ex-admin") == "observer"
        assert policy.check("ex-admin", "a2a:register", "any-agent") is False
        assert policy.check("ex-admin", "a2a:send", "any-agent") is False

    def test_agent_role(self):
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        policy.bind_role("agent-1", "agent")
        assert policy.check("agent-1", "a2a:register", "target") is True
        assert policy.check("agent-1", "a2a:send", "target") is True
        assert policy.check("agent-1", "a2a:get", "target") is True
        assert policy.check("agent-1", "a2a:delete", "target") is False

    def test_bind_unbind(self):
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        policy.bind_role("user-1", "admin")
        assert policy.get_role("user-1") == "admin"
        policy.unbind("user-1")
        assert policy.get_role("user-1") == "observer"  # Default

    def test_custom_roles(self):
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy(
            role_permissions={
                "operator": [
                    {"resource": "*", "action": "a2a:send"},
                    {"resource": "*", "action": "a2a:get"},
                ],
            }
        )
        policy.bind_role("op-1", "operator")
        assert policy.check("op-1", "a2a:send", "target") is True
        assert policy.check("op-1", "a2a:register", "target") is False


# ---------------------------------------------------------------------------
# A2A Gateway OIDC/RBAC Integration Tests
# ---------------------------------------------------------------------------


class TestA2AGatewayRBAC:
    """Test A2AGateway with OIDC/RBAC enforcement."""

    @pytest.mark.asyncio
    async def test_register_with_oidc(self):
        from beagle.core.a2a_protocol import (
            A2AGateway,
            AgentCard,
            OIDCVerifier,
            RBACPolicy,
        )

        policy = RBACPolicy()
        policy.bind_role("agent-001", "agent")
        gateway = A2AGateway(rbac_policy=policy)
        verifier = OIDCVerifier(audience="beagle-a2a")
        token = verifier.generate_local_token("agent-001", {"role": "agent"})

        card = AgentCard(
            name="test-agent",
            version="1.0.0",
            description="Test",
            capabilities=["test"],
            skills=["test"],
            endpoint="http://localhost:8081",
        )
        await gateway.register_agent(card, oidc_token=token)
        assert "test-agent" in gateway.agents

    @pytest.mark.asyncio
    async def test_register_with_invalid_oidc(self):
        from beagle.core.a2a_protocol import (
            A2AGateway,
            AgentCard,
        )

        gateway = A2AGateway()
        card = AgentCard(
            name="bad-agent",
            version="1.0.0",
            description="Test",
            capabilities=["test"],
            skills=["test"],
            endpoint="http://localhost:8081",
        )
        with pytest.raises(PermissionError):
            await gateway.register_agent(card, oidc_token="invalid-token")

    @pytest.mark.asyncio
    async def test_rbac_denied_register(self):
        from beagle.core.a2a_protocol import (
            A2AGateway,
            AgentCard,
            OIDCVerifier,
            RBACPolicy,
        )

        policy = RBACPolicy()  # Default: observer role only
        gateway = A2AGateway(rbac_policy=policy)
        verifier = OIDCVerifier(audience="beagle-a2a")
        # "observer-1" has no binding → defaults to 'observer' → cannot register
        token = verifier.generate_local_token("observer-1")

        card = AgentCard(
            name="denied-agent",
            version="1.0.0",
            description="Test",
            capabilities=["test"],
            skills=["test"],
            endpoint="http://localhost:8081",
        )
        with pytest.raises(PermissionError, match="RBAC denied"):
            await gateway.register_agent(card, oidc_token=token)

    @pytest.mark.asyncio
    async def test_route_message_with_rbac(self):
        from beagle.core.a2a_protocol import (
            A2AGateway,
            A2AMessage,
            AgentCard,
            OIDCVerifier,
            RBACPolicy,
        )

        policy = RBACPolicy()
        policy.bind_role("admin-1", "admin")
        gateway = A2AGateway(rbac_policy=policy)
        verifier = OIDCVerifier(audience="beagle-a2a")
        admin_token = verifier.generate_local_token("admin-1")

        card = AgentCard(
            name="target-agent",
            version="1.0.0",
            description="Test",
            capabilities=["test"],
            skills=["test"],
            endpoint="http://localhost:8081",
        )
        # D2 (Fable 5 DD 2026-06-11): registration now requires auth — was
        # previously silently allowed with no token (the "audit theatre"
        # failure mode). Pass a valid OIDC token for an admin identity.
        await gateway.register_agent(card, oidc_token=admin_token)

        # Route with admin token
        msg = A2AMessage(method="tasks/send", params={"agent": "target-agent", "data": {}})
        response = await gateway.route_message(msg, oidc_token=admin_token)
        # Should not get RBAC denied error (may get other errors if agent not fully set up)
        if response.error:
            assert "RBAC denied" not in response.error.get("message", "")


# ---------------------------------------------------------------------------
# MicroVM Sandbox Tests
# ---------------------------------------------------------------------------


class TestMicroVMSandbox:
    """Test MicroVM sandbox configuration and fallback."""

    def test_import(self):
        from beagle.core.sandbox import MicroVMConfig, MicroVMSandbox

        assert MicroVMConfig is not None
        assert MicroVMSandbox is not None

    def test_default_config(self):
        from beagle.core.sandbox import MicroVMConfig

        config = MicroVMConfig()
        assert config.vcpu_count == 1
        assert config.mem_size_mib == 256
        assert config.network_enabled is False
        assert config.timeout_seconds == 60

    def test_sandbox_not_available_without_firecracker(self):
        from beagle.core.sandbox import MicroVMSandbox

        sandbox = MicroVMSandbox()
        # On most dev machines, firecracker won't be installed
        # Just check the availability check doesn't crash
        _ = sandbox.available  # Should be False on dev machines

    @pytest.mark.asyncio
    async def test_fallback_to_process_sandbox(self):
        from beagle.core.sandbox import MicroVMConfig, MicroVMSandbox

        # C01: fallback is now deny-by-default; opt in explicitly.
        config = MicroVMConfig(allow_fallback=True)
        sandbox = MicroVMSandbox(config)
        # Even without firecracker, should fall back gracefully when opted in
        stdout, _stderr, code = await sandbox.run(["echo", "hello"])
        assert code == 0
        assert b"hello" in stdout

    @pytest.mark.asyncio
    async def test_fallback_denied_without_optin(self):
        """C01: an unavailable MicroVM refuses to run without opt-in."""
        from beagle.core.sandbox import MicroVMConfig, MicroVMSandbox

        sandbox = MicroVMSandbox(MicroVMConfig())  # allow_fallback defaults False
        _stdout, _stderr, code = await sandbox.run(["echo", "hello"])
        assert code == 126, "deny-by-default must refuse to run the payload"

    def test_microvm_profile_exists(self):
        from beagle.core.sandbox import get_sandbox_profile

        profile = get_sandbox_profile("microvm")
        assert profile.memory_limit == 256 * 1024 * 1024
        assert profile.allow_network is False
        assert profile.readonly_filesystem is True


# ---------------------------------------------------------------------------
# OpenTelemetry Tracing Tests
# ---------------------------------------------------------------------------


class TestOTELTracing:
    """Test OpenTelemetry span integration in A2A and graph modules."""

    def test_a2a_tracer_import(self):
        from beagle.core.a2a_protocol import _tracer

        # Should not crash even with/without OTEL installed
        assert _tracer is not None

    def test_a2a_span_creation(self):
        from beagle.core.a2a_protocol import _tracer

        # Should work as a no-op context manager when OTEL not available
        with _tracer.start_as_current_span("test.span"):
            pass  # Should not raise

    def test_graph_tracer_import(self):
        # Verify the graph module can be imported without error
        from beagle.core import graph

        assert graph is not None


# ---------------------------------------------------------------------------
# Structural Sharing / Deepcopy Elimination Tests
# ---------------------------------------------------------------------------


class TestStructuralSharing:
    """Test that state forking avoids unnecessary deep copies."""

    def test_deep_fork_state(self):
        from beagle.core.graph import _deep_fork_state

        state = {
            "query": "test",
            "messages": [{"role": "user", "content": "hello"}],
            "completed_nodes": [],
        }
        forked = _deep_fork_state(state)
        # Forked state should be a separate dict
        assert forked is not state
        assert forked == state
        # Modifying forked should not affect original
        forked["query"] = "modified"
        assert state["query"] == "test"


# ---------------------------------------------------------------------------
# Regression tests for D2 (Fable 5 DD 2026-06-11) — A2A registration with
# no token at all was silently allowed. These tests lock in the fix.
# ---------------------------------------------------------------------------


class TestA2AGatewayAuthRegression_D2:
    """Regression tests for the no-auth-bypass defect (D2, 2026-06-11 DD)."""

    @pytest.mark.asyncio
    async def test_register_with_no_token_raises(self):
        """Registering an agent with neither OIDC nor HMAC must raise PermissionError.

        Prior to v13.21.7, the code path was `elif auth_token and not _verify_auth_token(...)`
        which short-circuited when auth_token was None, silently registering the agent.
        """
        from beagle.core.a2a_protocol import (
            A2AGateway,
            AgentCard,
        )

        gateway = A2AGateway()
        card = AgentCard(
            name="unauthed-agent",
            version="1.0.0",
            description="Test",
            capabilities=["test"],
            skills=["test"],
            endpoint="http://localhost:8081",
        )
        with pytest.raises(PermissionError, match="missing auth token"):
            await gateway.register_agent(card)
        # And the agent must NOT be in the registry
        assert "unauthed-agent" not in gateway.agents

    @pytest.mark.asyncio
    async def test_register_with_only_oidc_succeeds(self):
        """Registering with valid OIDC alone (no HMAC) must still succeed.

        The D2 fix preserved the OIDC-only happy path by using `elif` rather
        than an unconditional auth_token check.
        """
        from beagle.core.a2a_protocol import (
            A2AGateway,
            AgentCard,
            OIDCVerifier,
            RBACPolicy,
        )

        policy = RBACPolicy()
        policy.bind_role("agent-1", "agent")
        gateway = A2AGateway(rbac_policy=policy)
        verifier = OIDCVerifier(audience="beagle-a2a")
        token = verifier.generate_local_token("agent-1", {"role": "agent"})

        card = AgentCard(
            name="oidc-only-agent",
            version="1.0.0",
            description="Test",
            capabilities=["test"],
            skills=["test"],
            endpoint="http://localhost:8081",
        )
        await gateway.register_agent(card, oidc_token=token)
        assert "oidc-only-agent" in gateway.agents


# ---------------------------------------------------------------------------
# Regression tests for D4 (Fable 5 DD 2026-06-11) — RBAC namespace mismatch
# made the default agent/observer roles fail-closed for every A2A action
# because route_message built "a2a:tasks/send" but permissions granted
# "a2a:send" and the matcher did no cross-namespace matching.
# ---------------------------------------------------------------------------


class TestRBACNamespaceRegression_D4:
    """Regression tests for the RBAC namespace mismatch defect (D4)."""

    def test_agent_can_send_tasks(self):
        """Default 'agent' role can route 'tasks/send' via 'a2a:send' permission."""
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        # Bind identity to the agent role — unbound identities default to 'observer'
        policy.bind_role("agent-001", "agent")
        ok = policy.check("agent-001", "a2a:tasks/send", "target-agent")
        assert ok, "agent role must match a2a:tasks/send against a2a:send permission"

    def test_agent_can_get_tasks(self):
        """Default 'agent' role can route 'tasks/get' via 'a2a:get' permission."""
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        policy.bind_role("agent-001", "agent")
        ok = policy.check("agent-001", "a2a:tasks/get", "target-agent")
        assert ok, "agent role must match a2a:tasks/get against a2a:get permission"

    def test_observer_can_get_but_not_send(self):
        """Default 'observer' role can get but not send."""
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        # observer is the default for unbound identities
        assert policy.check("unbound-observer", "a2a:tasks/get", "target")
        assert not policy.check("unbound-observer", "a2a:tasks/send", "target")

    def test_admin_can_do_anything(self):
        """Default 'admin' role still matches everything (regression guard)."""
        from beagle.core.a2a_protocol import RBACPolicy

        policy = RBACPolicy()
        policy.bind_role("admin-1", "admin")
        assert policy.check("admin-1", "a2a:tasks/send", "any")
        assert policy.check("admin-1", "a2a:anything/else", "any")


# ---------------------------------------------------------------------------
# Regression tests for D5 (Fable 5 DD 2026-06-11) — proxy bind check was a
# blocklist of {0.0.0.0, ::, ""} that missed LAN IPs like 192.168.x.x.
# Replaced with an ipaddress.is_loopback allowlist.
# ---------------------------------------------------------------------------


class TestProxyBindAllowlistRegression_D5:
    """Regression tests for the LAN-IP bind bypass defect (D5)."""

    def test_is_loopback_bind_function_present(self):
        """The _is_loopback_bind helper must be importable from the proxy module."""
        from beagle.bridges.ollama_cloud_proxy import _is_loopback_bind

        assert callable(_is_loopback_bind)

    @pytest.mark.parametrize("loopback", ["127.0.0.1", "127.0.0.42", "::1", "localhost"])
    def test_loopback_addresses_accepted(self, loopback):
        from beagle.bridges.ollama_cloud_proxy import _is_loopback_bind

        assert _is_loopback_bind(loopback), f"{loopback} must be accepted as loopback"

    @pytest.mark.parametrize("lan", ["192.168.1.5", "10.0.0.1", "172.16.0.1", "8.8.8.8"])
    def test_lan_and_wan_addresses_rejected(self, lan):
        from beagle.bridges.ollama_cloud_proxy import _is_loopback_bind

        assert not _is_loopback_bind(lan), f"{lan} must be rejected (LAN/WAN)"

    @pytest.mark.parametrize("wild", ["0.0.0.0", "::", ""])
    def test_wildcard_binds_rejected(self, wild):
        from beagle.bridges.ollama_cloud_proxy import _is_loopback_bind

        assert not _is_loopback_bind(wild), f"{wild!r} must be rejected (wildcard)"

    def test_garbage_string_rejected(self):
        from beagle.bridges.ollama_cloud_proxy import _is_loopback_bind

        assert not _is_loopback_bind("not-an-ip-at-all")


# ---------------------------------------------------------------------------
# Regression tests for D9, D13, D14, D15 (Fable 5 DD 2026-06-11) — performance
# and concurrency defects. All four are small surgical fixes.
# ---------------------------------------------------------------------------


class TestProxyUsesThreadingHTTPServer_D9:
    """D9: HTTPServer → ThreadingHTTPServer prevents head-of-line blocking."""

    def test_httpserver_replaced_with_threadinghttpserver(self):
        from beagle.bridges import ollama_cloud_proxy

        # The module must import ThreadingHTTPServer, not the bare HTTPServer
        assert hasattr(ollama_cloud_proxy, "ThreadingHTTPServer"), (
            "ThreadingHTTPServer must be imported for the per-request "
            "threading model that prevents head-of-line blocking."
        )


class TestA2AClientTimeout_D13:
    """D13: A2AClient._ensure_session must apply a default timeout."""

    @pytest.mark.asyncio
    async def test_session_has_default_timeout(self, monkeypatch):
        """A freshly-created A2AClient session must carry a non-None timeout."""
        from beagle.core.a2a_protocol import A2AClient

        # Clear any cached env override
        monkeypatch.delenv("A2A_CLIENT_TIMEOUT", raising=False)
        client = A2AClient(endpoint="http://localhost:9999")
        await client._ensure_session()
        try:
            assert client._session is not None
            timeout = client._session.timeout
            assert timeout is not None, "Session must have a timeout configured"
            assert timeout.total == 30.0, f"Default timeout should be 30s, got {timeout.total}"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_env_var_overrides_timeout(self, monkeypatch):
        """A2A_CLIENT_TIMEOUT env var must be honoured."""
        from beagle.core.a2a_protocol import A2AClient

        monkeypatch.setenv("A2A_CLIENT_TIMEOUT", "5.5")
        client = A2AClient(endpoint="http://localhost:9999")
        await client._ensure_session()
        try:
            assert client._session.timeout.total == 5.5
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_invalid_env_var_falls_back_to_default(self, monkeypatch):
        """An unparseable A2A_CLIENT_TIMEOUT value must not crash — fall back to 30s."""
        from beagle.core.a2a_protocol import A2AClient

        monkeypatch.setenv("A2A_CLIENT_TIMEOUT", "not-a-float")
        client = A2AClient(endpoint="http://localhost:9999")
        await client._ensure_session()
        try:
            assert client._session.timeout.total == 30.0
        finally:
            await client.close()


class TestGuardianPathBoundary_D14:
    """D14: Guardian protected-path check must use os.sep as a directory boundary."""

    def test_root_protected_path_does_not_match_rootful_data(self, tmp_path, monkeypatch):
        """A protected /root path must NOT match a sibling like /rootful-data."""
        import os as _os

        from beagle.guardian import (
            ApprovalDecision,
            ApprovalPolicy,
            GuardianAction,
        )

        # Create a fake /root (must exist for realpath to resolve)
        fake_root = tmp_path / "root"
        fake_root.mkdir()
        # Create a sibling /rootful-data that starts with "root" but is NOT /root
        fake_sibling = tmp_path / "rootful-data"
        fake_sibling.mkdir()
        # Patch os.path.realpath to remap /root and /rootful-data to our tmp paths
        real_root = str(fake_root.resolve())
        real_sibling = str(fake_sibling.resolve())
        original_realpath = _os.path.realpath

        def _patched_realpath(p):
            if p == "/root" or p.endswith("/root"):
                return real_root
            if p == "/rootful-data" or p.endswith("/rootful-data"):
                return real_sibling
            return original_realpath(p)

        monkeypatch.setattr(_os.path, "realpath", _patched_realpath)

        policy = ApprovalPolicy(protected_paths={"/root"})
        # The boundary fix must let the sibling through (it's not actually under /root)
        action = GuardianAction(
            action_type="read",
            description="test",
            details={"path": str(fake_sibling / "secret.txt")},
        )
        decision = policy.evaluate(action)
        # With D14 fix: the sibling is NOT under /root, so decision must NOT be NEEDS_HUMAN
        assert decision != ApprovalDecision.NEEDS_HUMAN, (
            f"Sibling path {fake_sibling} must not match /root protected path"
        )

    def test_path_actually_under_protected_still_needs_human(self, tmp_path, monkeypatch):
        """A real file under /root must still trigger NEEDS_HUMAN."""
        import os as _os

        from beagle.guardian import (
            ApprovalDecision,
            ApprovalPolicy,
            GuardianAction,
        )

        fake_root = tmp_path / "root"
        fake_root.mkdir()
        fake_file = fake_root / "secret.txt"
        fake_file.write_text("x")
        real_root = str(fake_root.resolve())
        original_realpath = _os.path.realpath

        def _patched_realpath(p):
            if p.startswith("/root"):
                if p == "/root":
                    return real_root
                return real_root + p[len("/root") :]
            return original_realpath(p)

        monkeypatch.setattr(_os.path, "realpath", _patched_realpath)

        policy = ApprovalPolicy(protected_paths={"/root"})
        action = GuardianAction(
            action_type="read", description="test", details={"path": str(fake_file)}
        )
        decision = policy.evaluate(action)
        assert decision == ApprovalDecision.NEEDS_HUMAN


class TestSingletonRaceFix_D15:
    """D15: Singleton.get_instance must be thread-safe under concurrent first-call."""

    def test_concurrent_get_instance_returns_same_object(self):
        """100 threads racing on get_instance must all get the same instance."""
        import threading

        from beagle.core.state import Singleton

        class TestSingleton(Singleton[dict]):
            def _create(self) -> dict:
                return {"created_by": threading.get_ident()}

        # Clean any cached class state from prior test runs
        for attr in ("_singleton_instance", "_singleton_construction_lock"):
            if hasattr(TestSingleton, attr):
                delattr(TestSingleton, attr)

        results: list = []
        barrier = threading.Barrier(100)

        def worker():
            barrier.wait()  # All threads released at once
            instance = TestSingleton.get_instance()
            results.append(id(instance))

        threads = [threading.Thread(target=worker) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        unique_ids = set(results)
        assert len(unique_ids) == 1, (
            f"Expected 1 unique instance ID from 100 threads, got {len(unique_ids)}: "
            f"this is the D15 race — the old code could construct multiple instances."
        )

    def test_persistent_save_uses_atomic_write(self, tmp_path):
        """PersistentSingleton._save must write to a temp file then os.replace."""
        from beagle.core.state import PersistentSingleton

        class TestPersistent(PersistentSingleton[dict]):
            def _create(self) -> dict:
                return {"x": 1}

            def _serialize(self, instance: dict) -> bytes:
                import json

                return json.dumps(instance).encode()

            def _deserialize(self, data: bytes) -> dict:
                import json

                return json.loads(data.decode())

        # Clean any cached class state
        for attr in ("_singleton_instance", "_singleton_construction_lock"):
            if hasattr(TestPersistent, attr):
                delattr(TestPersistent, attr)

        path = tmp_path / "test_persist.bin"
        tp = TestPersistent(persist_path=path)
        tp.get()  # triggers creation + first _save
        assert path.exists()
        # No leftover .tmp.* files
        leftover = list(tmp_path.glob("*.tmp.*"))
        assert leftover == [], f"Atomic save must not leave temp files: {leftover}"
        # The data must be readable
        import json

        assert json.loads(path.read_bytes()) == {"x": 1}


class TestMaxTokensFloor_D16:
    """D16: max_tokens floor must be env-tunable, with opt-out via 0."""

    def test_floor_disabled_when_zero(self, monkeypatch):
        monkeypatch.setattr("beagle.bridges.ollama_cloud_proxy.MIN_MAX_TOKENS", 0)
        from beagle.bridges.ollama_cloud_proxy import (
            _ensure_max_tokens,
        )

        body = {"max_tokens": 100}  # small, intentional
        changed = _ensure_max_tokens(body)
        assert not changed, "Floor disabled — must not modify the body"
        assert body["max_tokens"] == 100, "Caller request must be preserved as-is"

    def test_floor_applies_when_below_minimum(self, monkeypatch):
        monkeypatch.setattr("beagle.bridges.ollama_cloud_proxy.MIN_MAX_TOKENS", 4096)
        from beagle.bridges.ollama_cloud_proxy import (
            _ensure_max_tokens,
        )

        body = {"max_tokens": 100}  # small, below floor
        changed = _ensure_max_tokens(body)
        assert changed
        assert body["max_tokens"] == 4096

    def test_floor_does_not_touch_above_minimum(self, monkeypatch):
        monkeypatch.setattr("beagle.bridges.ollama_cloud_proxy.MIN_MAX_TOKENS", 4096)
        from beagle.bridges.ollama_cloud_proxy import (
            _ensure_max_tokens,
        )

        body = {"max_tokens": 16384}  # well above floor
        changed = _ensure_max_tokens(body)
        assert not changed
        assert body["max_tokens"] == 16384, "Caller request above floor must be preserved"

    def test_floor_applies_to_max_completion_tokens(self, monkeypatch):
        monkeypatch.setattr("beagle.bridges.ollama_cloud_proxy.MIN_MAX_TOKENS", 4096)
        from beagle.bridges.ollama_cloud_proxy import (
            _ensure_max_tokens,
        )

        body = {"max_completion_tokens": 100}  # small, below floor
        changed = _ensure_max_tokens(body)
        assert changed
        assert body["max_completion_tokens"] == 4096
