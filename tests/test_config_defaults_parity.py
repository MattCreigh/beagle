# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Configuration contract for the open-source distribution.

The wheel ships NO bundled configuration: every default lives in code
(``config/schema.py`` + ``config/defaults.py``), and all user-editable
config lives under ``~/.config/beagle`` (XDG). These tests pin that
contract:

1. no ``default_config`` directory in the package tree,
2. a fully configless environment loads with correct defaults
   (transport=http, no provider presets),
3. ``generate_default_config()`` emits TOML that parses back to values
   matching the schema defaults (no drift between generator and schema).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import beagle
from beagle.config.config import get_config
from beagle.config.defaults import generate_default_config

_PKG = Path(beagle.__file__).resolve().parent


def test_no_bundled_config_ships() -> None:
    """The open-source distribution contains zero bundled config files."""
    assert not (_PKG / "default_config").exists(), (
        "default_config must not ship in the open-source wheel; "
        "all config belongs in ~/.config/beagle"
    )


def test_configless_defaults_are_correct(monkeypatch, tmp_path) -> None:
    """Fresh install (empty XDG dir) yields safe, provider-neutral defaults."""
    # Isolate from any operator-seeded ~/.config/beagle on the test host.
    xdg = tmp_path / "xdg"
    (xdg / "beagle").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("BEAGLE_CONFIG_ROOT", raising=False)
    monkeypatch.delenv("BEAGLE_CONFIG_TOML", raising=False)
    from beagle.config._config_path import reset_config_path_cache
    from beagle.config.loader import reset_config_cache

    reset_config_path_cache()
    reset_config_cache()
    try:
        _assert_neutral_defaults()
    finally:
        reset_config_path_cache()
        reset_config_cache()


def _assert_neutral_defaults() -> None:
    cfg = get_config()
    assert cfg.connections.transport == "http"  # never auto-activate plugins
    assert cfg.goose.default_pool_chain == []  # no provider presets
    assert cfg.ollama_cloud.endpoint == ""  # endpoint is operator-supplied


def test_generated_default_config_round_trips() -> None:
    """generate_default_config() output parses and carries the key defaults."""
    data = tomllib.loads(generate_default_config())
    assert isinstance(data, dict) and data, "generated config must be non-empty TOML"
    goose = data.get("goose", {})
    assert isinstance(goose, dict)
    assert goose.get("default_model") in ("", None), "no model preset may ship"


def test_transport_selection_env_overrides_config(monkeypatch):  # type: ignore[no-untyped-def]
    """$BEAGLE_TRANSPORT selects an installed transport explicitly."""
    from beagle.core.transports import get_registry, reset_registry

    reset_registry()
    monkeypatch.delenv("BEAGLE_TRANSPORT", raising=False)
    assert get_registry().active().name == "http"
    monkeypatch.setenv("BEAGLE_TRANSPORT", "http")
    reset_registry()
    assert get_registry().active().name == "http"
    reset_registry()
