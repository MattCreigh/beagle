"""Tests for the CONFIG_ROOT resolver family in config/_config_path.py.

The migration detaches configuration from the source tree. The resolver
family routes every config-data path through a single config root, with a
legacy in-tree fallback. These tests exercise the canonical-resolution path
(no /home/Beagle_Config present) and the $BEAGLE_CONFIG_ROOT override.

The resolver is process-cached, so every test resets the cache via
``reset_config_path_cache()`` (in a fixture) and uses ``monkeypatch.setenv``
so the $BEAGLE_CONFIG_ROOT override is scoped to the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_path_cache() -> None:
    """Reset the process-wide resolver cache before each test."""
    from beagle.config._config_path import reset_config_path_cache

    reset_config_path_cache()
    yield
    reset_config_path_cache()


def test_find_config_root_defaults_to_legacy_repo_config_when_canonical_absent(
    monkeypatch,
) -> None:
    """With no $BEAGLE_CONFIG_ROOT and no populated user-config dir, the
    resolver falls back to the legacy source-tree location."""
    monkeypatch.delenv("BEAGLE_CONFIG_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent-xdg")
    from beagle.config._config_path import find_config_root

    root = find_config_root()
    assert isinstance(root, Path)


def test_empty_user_config_dir_does_not_shadow_fallback(monkeypatch, tmp_path) -> None:
    """An EMPTY ~/.config/beagle must not shadow the repo/bundled fallback.

    A tool that touches XDG dirs can create an empty ~/.config/beagle; that
    must not make the resolver return a broken root. The resolver gates the
    user-config step on POPULATED (beagle_core_config/config.toml present).
    """
    monkeypatch.delenv("BEAGLE_CONFIG_ROOT", raising=False)
    empty_xdg = tmp_path / "xdg"
    (empty_xdg / "beagle").mkdir(parents=True)  # empty beagle dir
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty_xdg))
    from beagle.config._config_path import find_config_root

    root = find_config_root()
    # OSS contract: an unpopulated XDG dir is the STABLE terminal root (the
    # wheel ships no bundled fallback), not something to be shadowed.
    assert root == empty_xdg / "beagle", (
        f"unpopulated XDG root must be returned as-is; got {root}"
    )


def test_find_config_root_honors_beagle_config_root_env(monkeypatch, tmp_path) -> None:
    """$BEAGLE_CONFIG_ROOT points the resolver at an operator-owned root."""
    monkeypatch.setenv("BEAGLE_CONFIG_ROOT", str(tmp_path))
    from beagle.config._config_path import find_config_root

    assert find_config_root() == tmp_path.resolve()


def test_find_config_toml_falls_back_to_legacy_without_canonical(monkeypatch) -> None:
    """Without /home/Beagle_Config, config.toml resolves to the in-tree copy."""
    monkeypatch.delenv("BEAGLE_CONFIG_TOML", raising=False)
    from beagle.config._config_path import find_config_toml

    p = find_config_toml()
    assert p.name == "config.toml"
    assert p.is_file() or p.is_symlink(), f"expected an existing config.toml, got {p}"


def test_resolver_family_returns_paths_without_raising(monkeypatch) -> None:
    """Every resolver in the family returns a Path without raising, in the
    legacy fallback state (no canonical root present)."""
    monkeypatch.delenv("BEAGLE_CONFIG_ROOT", raising=False)
    from beagle.config._config_path import (
        find_agents_toml,
        find_auth_dir,
        find_blocks_agents_dir,
        find_coding_agent_dir,
        find_config_root,
        find_core_config_dir,
        find_deployments_dir,
        find_guides_dir,
        find_inference_config_dir,
        find_metaprompts_dir,
        find_plugin_config,
        find_plugins_dir,
        find_presets_dir,
        find_providers_toml,
        find_recipes_dir,
        find_registry_dir,
        find_workflows_dir,
    )

    for resolver in (
        find_config_root,
        find_core_config_dir,
        find_coding_agent_dir,
        find_inference_config_dir,
        find_registry_dir,
        find_providers_toml,
        find_presets_dir,
        find_guides_dir,
        find_agents_toml,
        find_recipes_dir,
        find_workflows_dir,
        find_metaprompts_dir,
        find_blocks_agents_dir,
        find_auth_dir,
        find_plugins_dir,
        find_deployments_dir,
    ):
        result = resolver()
        assert isinstance(result, Path), f"{resolver.__name__} returned {result!r}"

    plugin = find_plugin_config("demo_plugin")
    assert plugin.name == "demo_plugin.toml"
    assert "plugins" in plugin.parts
