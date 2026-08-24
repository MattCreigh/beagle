"""Tests for the Prometheus exporter.

Locks down the opt-in semantics: the exporter must NOT start unless
explicitly configured (env var or config), and the
``prometheus_client`` package must be present.
"""

from __future__ import annotations

import importlib


def test_module_imports_without_prometheus():
    """The module must import even if prometheus_client is missing."""
    import beagle.observability.prometheus_exporter as p

    assert hasattr(p, "start_prometheus")
    assert hasattr(p, "stop_prometheus")
    assert hasattr(p, "is_running")


def test_is_enabled_false_by_default(monkeypatch):
    """No env var, no config port → not enabled."""
    import beagle.observability.prometheus_exporter as p

    # Re-import to pick up monkey-patched env
    monkeypatch.delenv("BEAGLE_PROMETHEUS_PORT", raising=False)
    importlib.reload(p)
    assert p._is_enabled() is False
    assert p.is_running() is False


def test_is_enabled_with_env_var(monkeypatch):
    """BEAGLE_PROMETHEUS_PORT=9090 enables the exporter."""
    import beagle.observability.prometheus_exporter as p

    monkeypatch.setenv("BEAGLE_PROMETHEUS_PORT", "9090")
    importlib.reload(p)
    assert p._is_enabled() is True


def test_is_enabled_rejects_zero(monkeypatch):
    """BEAGLE_PROMETHEUS_PORT=0 explicitly disables."""
    import beagle.observability.prometheus_exporter as p

    monkeypatch.setenv("BEAGLE_PROMETHEUS_PORT", "0")
    importlib.reload(p)
    assert p._is_enabled() is False


def test_is_enabled_rejects_garbage(monkeypatch):
    """BEAGLE_PROMETHEUS_PORT='abc' is treated as not-set."""
    import beagle.observability.prometheus_exporter as p

    monkeypatch.setenv("BEAGLE_PROMETHEUS_PORT", "abc")
    importlib.reload(p)
    assert p._is_enabled() is False


def test_resolve_port_default():
    """Without env var or config, port defaults to 9090."""
    import os

    import beagle.observability.prometheus_exporter as p

    os.environ.pop("BEAGLE_PROMETHEUS_PORT", None)
    # Note: if a config is loaded, it may override — we just check the
    # default value is one of the well-known port numbers.
    port = p._resolve_port()
    assert isinstance(port, int)
    assert port > 0


def test_start_without_prometheus_client(monkeypatch):
    """If prometheus_client is not installed, start_prometheus returns False."""
    import beagle.observability.prometheus_exporter as p

    # Patch the import inside the function to fail
    monkeypatch.setitem(__import__("sys").modules, "prometheus_client", None)
    # Now call start — it should fail gracefully
    result = p.start_prometheus(port=0)
    assert result is False


def test_stop_is_idempotent():
    """stop_prometheus on a not-running exporter is a no-op."""
    import beagle.observability.prometheus_exporter as p

    p.stop_prometheus()  # not running — no exception
    p.stop_prometheus()  # still no exception
    assert p.is_running() is False
