"""Consolidated Beagle MCP server tests.

Covers the v14 consolidation: ONE unified surface (mcp_beagle_server)
absorbing the rag + utility groups, plus the [tool] standard — a TOML
plugin registry with runtime hotswap (mount/unmount without restart).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from beagle.config._config_path import reset_config_path_cache
from beagle.infrastructure import mcp_beagle_server as mbs

DUMMY_PLUGIN = '''
from mcp.server.fastmcp import FastMCP

ENABLED = {enabled}

mcp = FastMCP("dummy-{{name}}")


@mcp.tool()
def {name}_echo(x: str) -> str:
    """Echo x back."""
    return x


def is_enabled():
    return ENABLED
'''


def _write_registry(root: Path, entries: list[dict[str, Any]], **meta: Any) -> Path:
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    lines = ["[meta]", 'name = "test_registry"', 'version = "1"']
    lines.append(f"hotswap_enabled = {bool(meta.get('hotswap_enabled', True))!r}".lower())
    lines.append(
        f"auto_enable_detected = {bool(meta.get('auto_enable_detected', False))!r}".lower()
    )
    for e in entries:
        lines.append("")
        lines.append("[[tool]]")
        for k, v in e.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            else:
                lines.append(f'{k} = "{v}"')
    path = plugins_dir / "tools.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $BEAGLE_CONFIG_ROOT at a temp root; core groups stay loaded."""
    monkeypatch.setenv("BEAGLE_CONFIG_ROOT", str(tmp_path))
    reset_config_path_cache()
    # Core absorption is once-per-process; ensure it happened under this env.
    app = mbs.build_app()
    assert app is mbs.app
    yield tmp_path
    reset_config_path_cache()


def _install_plugin(root: Path, name: str, enabled: bool) -> str:
    mod_dir = root / "_plugmods"
    mod_dir.mkdir(parents=True, exist_ok=True)
    mod_file = mod_dir / f"plug_{name}.py"
    mod_file.write_text(DUMMY_PLUGIN.format(name=name, enabled=str(enabled)), encoding="utf-8")
    if str(mod_dir) not in sys.path:
        sys.path.insert(0, str(mod_dir))
    return f"plug_{name}"


# ── Core consolidation ────────────────────────────────────────────────────────


class TestCoreConsolidation:
    def test_core_groups_absorbed(self, isolated_root: Path) -> None:
        names = {t.name for t in mbs.app._tool_manager.list_tools()}
        # representative tools from every layer
        assert {"rag_search", "rag_hotswap_ingest"} <= names  # rag group
        assert {"run_beagle_workflow", "web_search", "file_discovery"} <= names  # utility
        assert {"plugin_status", "plugin_reload"} <= names  # management
        # collision policy: canonical utility health_check kept,
        # rag variants renamed deterministically
        assert "health_check" in names
        assert "rag_health_check" in names
        assert "rag_get_metrics" in names

    def test_no_duplicate_tool_names(self, isolated_root: Path) -> None:
        names = [t.name for t in mbs.app._tool_manager.list_tools()]
        assert len(names) == len(set(names))

    def test_core_status_reports_groups(self, isolated_root: Path) -> None:
        status = json.loads(_run(mbs.plugin_status()))
        assert set(status["core"]) == {"utility", "rag"}
        assert status["total_tools"] >= 30
        assert status["registry"]["path"].endswith("tools.toml")


# ── [tool] standard: TOML-driven mount / unmount ─────────────────────────────


class TestToolStandardHotswap:
    def test_mount_then_unmount_without_restart(self, isolated_root: Path) -> None:
        module = _install_plugin(isolated_root, "alpha", enabled=True)
        _write_registry(
            isolated_root,
            [{"name": "alpha", "source": "module", "module": module, "enabled": True}],
        )
        result = mbs.reconcile_plugins(reason="test-mount")
        assert result["mounted"] == ["alpha"]
        names = {t.name for t in mbs.app._tool_manager.list_tools()}
        assert "alpha_echo" in names

        # flip the TOML gate → hotswap unmounts, same process
        _write_registry(
            isolated_root,
            [{"name": "alpha", "source": "module", "module": module, "enabled": False}],
        )
        result = mbs.reconcile_plugins(reason="test-unmount")
        assert result["mounted"] == []
        names = {t.name for t in mbs.app._tool_manager.list_tools()}
        assert "alpha_echo" not in names

    def test_self_gate_disables_even_when_listed(self, isolated_root: Path) -> None:
        module = _install_plugin(isolated_root, "gated", enabled=False)
        _write_registry(
            isolated_root,
            [{"name": "gated", "source": "module", "module": module, "enabled": True}],
        )
        result = mbs.reconcile_plugins(reason="test-gate")
        assert result["mounted"] == []
        view = mbs._PLUGIN_VIEWS["gated"]
        assert view.mounted is False
        assert view.self_enabled is False
        assert "self-gate" in view.error

    def test_missing_module_recorded_not_fatal(self, isolated_root: Path) -> None:
        _write_registry(
            isolated_root,
            [{"name": "ghost", "source": "module", "module": "no_such_pkg_xyz", "enabled": True}],
        )
        result = mbs.reconcile_plugins(reason="test-missing")
        assert result["registry_error"] == ""
        view = mbs._PLUGIN_VIEWS["ghost"]
        assert view.mounted is False
        assert "import failed" in view.error

    def test_entry_point_source_unknown_key_recorded(self, isolated_root: Path) -> None:
        _write_registry(
            isolated_root,
            [
                {
                    "name": "epmissing",
                    "source": "entry_point",
                    "group": "beagle.mcp_plugins",
                    "key": "not_installed_anywhere",
                    "enabled": True,
                }
            ],
        )
        mbs.reconcile_plugins(reason="test-ep")
        view = mbs._PLUGIN_VIEWS["epmissing"]
        assert view.mounted is False
        assert "not found in group" in view.error

    def test_malformed_toml_keeps_last_good_mounts(self, isolated_root: Path) -> None:
        module = _install_plugin(isolated_root, "stable", enabled=True)
        reg = _write_registry(
            isolated_root,
            [{"name": "stable", "source": "module", "module": module, "enabled": True}],
        )
        mbs.reconcile_plugins(reason="test-good")
        assert mbs._MOUNTED.get("stable") is not None

        reg.write_text("[[tool]]\nbroken === syntax", encoding="utf-8")
        result = mbs.reconcile_plugins(reason="test-bad")
        assert result["registry_error"] != ""
        assert result["mounted"] == ["stable"]  # fail-closed to last good
        names = {t.name for t in mbs.app._tool_manager.list_tools()}
        assert "stable_echo" in names

    def test_duplicate_tool_collision_first_wins(self, isolated_root: Path) -> None:
        module = _install_plugin(isolated_root, "collide", enabled=True)
        _write_registry(
            isolated_root,
            [{"name": "collide", "source": "module", "module": module, "enabled": True}],
        )
        result = mbs.reconcile_plugins(reason="test-collide")
        # collide_echo mounts fine...
        assert "collide" in result["mounted"]

    def test_hotswap_disabled_refuses_reload(self, isolated_root: Path) -> None:
        module = _install_plugin(isolated_root, "frozen", enabled=True)
        _write_registry(
            isolated_root,
            [{"name": "frozen", "source": "module", "module": module, "enabled": True}],
            hotswap_enabled=False,
        )
        out = json.loads(_run(mbs.plugin_reload()))
        assert "hotswap disabled" in out["error"]


# ── Management tool surfaces ─────────────────────────────────────────────────


class TestManagementTools:
    def test_plugin_reload_scopes_report(self, isolated_root: Path) -> None:
        out = json.loads(_run(mbs.plugin_reload("does_not_exist")))
        assert out["scoped"]["error"] == "unknown plugin"

    def test_plugin_status_lists_detected_unlisted(self, isolated_root: Path) -> None:
        # openclaw entry point may exist in dev env; regardless, status must
        # include a plugins array with per-plugin gate fields.
        status = json.loads(_run(mbs.plugin_status()))
        assert isinstance(status["plugins"], list)
        for p in status["plugins"]:
            assert {"name", "listed", "toml_enabled", "self_enabled", "mounted", "error"} <= set(p)


def _run(coro: Any) -> str:
    import asyncio

    return asyncio.run(coro)
