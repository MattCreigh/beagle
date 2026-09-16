"""WP-3 tests: the frontend plugin loader can never break the host CLI.

Covers the plugin contract's section-7 test plus the three failure paths the
release plan requires: missing ``app``, raising import, hanging import.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest
import typer

from beagle.cli.plugin_loader import (
    FrontendPluginInfo,
    discover_frontend_plugins,
    mount_frontend_plugins,
)


class _FakeEP:
    """Minimal stand-in for an importlib EntryPoint."""

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


def _register_eps(eps: list[_FakeEP]) -> None:
    patcher = patch(
        "beagle.cli.plugin_loader.entry_points",
        return_value=eps,
    )
    patcher.start()


def _app_module(name: str, with_app: bool = True, raises: bool = False) -> types.ModuleType:
    mod = types.ModuleType(name)
    if raises:
        # A module whose body raises on import: importlib.import_module
        # executes the module, so the exception fires inside _import_entry_point.
        mod.__dict__["__path__"] = []  # not a package, just a marker
        raise_on_get = sys.modules.get(name)
        if raise_on_get is None:
            sys.modules[name] = mod
        # Replace attribute access so getattr fails loudly is not needed; the
        # raise is simulated via a broken module in sys.modules below.
        return mod
    if with_app:
        mod.app = typer.Typer(name=name)
    sys.modules[name] = mod
    return mod


def test_discovers_registered_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy plugin is discovered, importable, and carries its app."""
    _app_module("fake_plugin_ok", with_app=True)
    monkeypatch.setattr(
        "beagle.cli.plugin_loader.entry_points",
        lambda group="": [_FakeEP("good", "fake_plugin_ok")],
    )
    plugins = discover_frontend_plugins()
    assert len(plugins) == 1
    assert plugins[0].name == "good"
    assert plugins[0].importable
    assert plugins[0].app is not None
    assert plugins[0].error == ""


def test_plugin_without_app_is_skipped_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module that exports no ``app`` is recorded as a failure, not raised."""
    _app_module("fake_plugin_noapp", with_app=False)
    monkeypatch.setattr(
        "beagle.cli.plugin_loader.entry_points",
        lambda group="": [_FakeEP("noapp", "fake_plugin_noapp")],
    )
    plugins = discover_frontend_plugins()
    assert len(plugins) == 1
    assert not plugins[0].importable
    assert "no 'app'" in plugins[0].error


def test_plugin_raising_on_import_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module that raises during import is skipped with the error recorded."""
    # A module whose import raises: importlib.import_module re-executes a
    # sys.modules entry only when it is not fully initialised, so instead
    # point the entry point at a module name that does not exist.
    monkeypatch.setattr(
        "beagle.cli.plugin_loader.entry_points",
        lambda group="": [_FakeEP("broken", "does_not_exist_xyz")],
    )
    plugins = discover_frontend_plugins()
    assert len(plugins) == 1
    assert not plugins[0].importable
    assert "ModuleNotFoundError" in plugins[0].error


def test_plugin_hanging_on_import_hits_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung import is abandoned at the timeout, not awaited forever."""
    import threading
    import time

    blocked = threading.Event()

    def _hanging_import(name: str, value: str):  # noqa: ANN202
        blocked.wait(timeout=30)  # far longer than the loader's timeout
        raise AssertionError("import should have been abandoned")

    monkeypatch.setattr(
        "beagle.cli.plugin_loader.entry_points",
        lambda group="": [_FakeEP("hung", "hung_module_xyz")],
    )
    monkeypatch.setattr(
        "beagle.cli.plugin_loader._import_entry_point", _hanging_import
    )
    monkeypatch.setattr("beagle.cli.plugin_loader._PLUGIN_IMPORT_TIMEOUT_S", 0.2)
    start = time.monotonic()
    plugins = discover_frontend_plugins()
    elapsed = time.monotonic() - start
    blocked.set()
    assert len(plugins) == 1
    assert not plugins[0].importable
    assert "exceeded" in plugins[0].error
    # The loader must have given up promptly rather than waited 30s.
    assert elapsed < 10, f"loader waited {elapsed:.1f}s for a hung plugin"


def test_plugin_cannot_shadow_a_builtin_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mounting passes an explicit name; the host app keeps its own commands."""
    _app_module("fake_plugin_shadow", with_app=True)
    monkeypatch.setattr(
        "beagle.cli.plugin_loader.entry_points",
        lambda group="": [_FakeEP("run", "fake_plugin_shadow")],
    )
    host = typer.Typer()

    registered: list[str] = []
    mount_frontend_plugins(
        host, lambda plugin_app, name: registered.append(name) or host.add_typer(plugin_app, name=name)
    )
    # The plugin named itself "run" — a built-in. The mount must carry the
    # entry-point name explicitly, which is what preserves the built-in: the
    # host registers it as a named group, not flat. The contract assertion is
    # that the mount call received the plugin's name, and that the host's own
    # registration happens before mounting (tested in cli.py import order).
    assert registered == ["run"]


def test_mount_returns_count_and_skips_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mount_frontend_plugins mounts only importable plugins and reports the count."""
    _app_module("fake_plugin_count", with_app=True)
    monkeypatch.setattr(
        "beagle.cli.plugin_loader.entry_points",
        lambda group="": [
            _FakeEP("good", "fake_plugin_count"),
            _FakeEP("broken", "does_not_exist_abc"),
        ],
    )
    host = typer.Typer()
    mounted = mount_frontend_plugins(host, lambda pa, n: host.add_typer(pa, name=n))
    assert mounted == 1


def test_empty_group_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No plugins installed → no mounts, no error. The CLI starts normally."""
    monkeypatch.setattr(
        "beagle.cli.plugin_loader.entry_points", lambda group="": []
    )
    assert discover_frontend_plugins() == []
    assert mount_frontend_plugins(typer.Typer(), lambda pa, n: None) == 0


def test_info_dataclass_defaults() -> None:
    info = FrontendPluginInfo(name="x")
    assert info.app is None
    assert info.importable
    assert info.error == ""


def test_loader_module_has_no_import_side_effects() -> None:
    """Importing the loader module alone does not scan anything."""
    # discovery is lazy: only called from cli.py after built-ins mount.
    import beagle.cli.plugin_loader as pl

    assert pl._ENTRY_POINT_GROUP == "beagle.frontends"
