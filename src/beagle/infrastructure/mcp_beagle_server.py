"""Unified Beagle MCP server — ONE surface, registered in goose as ``beagle``.

Consolidates the former three-part registration:

    beagle-rag      -> beagle.infrastructure.mcp_rag_server
    beagle-utility  -> beagle.infrastructure.mcp_utility_server
    beagle-openclaw -> beagle_openclaw.server (external plugin repo)

into a single stdio endpoint. Core groups (RAG, utility) are absorbed from
their existing modules so each remains runnable standalone; nothing about
their tool implementations changes.

[tool] standard — TOML-configured plugin loading with hotswap
-------------------------------------------------------------
External MCP plugins (OpenClaw and future ones) are NOT hardcoded here.
They are declared in the registry TOML::

    <config_root>/plugins/tools.toml        (see find_plugins_dir())

Schema (v1)::

    [meta]
    name = "beagle_tool_registry"
    version = "1"
    hotswap_enabled = true       # allow runtime mount/unmount
    auto_enable_detected = false # detected-but-unlisted plugins stay inert

    [[tool]]
    name = "openclaw"
    source = "entry_point"            # entry_point | module
    group = "beagle.mcp_plugins"      # required when source = entry_point
    key = "openclaw"                  # entry-point name within the group
    module = ""                       # required when source = module
    enabled = true                    # registry gate (ANDed with self-gate)
    prefix = ""                       # optional prefix for absorbed names
    description = ""

Plugin contract ("the beagle [tool] standard"): a plugin resolves to an
object exposing ``mcp: FastMCP`` (the object itself may BE a FastMCP, expose
``mcp``, or provide ``factory() -> <that>``). An optional ``is_enabled()``
self-gate is ANDed with the registry ``enabled`` flag. Tools, resources,
and resource templates are absorbed verbatim — no per-tool wiring.

Hotswap: ``plugin_reload()`` re-reads the TOML, re-scans entry points, and
mounts/unmounts plugin surfaces in-process. Adding or removing a plugin is
an edit to ``tools.toml`` + one ``plugin_reload`` call — no restart.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config._config_path import find_plugins_dir, reset_config_path_cache
from ._locks import SWAP_LOCK

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("Beagle.infrastructure.mcp_beagle_server")

REGISTRY_FILENAME = "tools.toml"

# ── Root server ───────────────────────────────────────────────────────────────

app = FastMCP(
    "Beagle",
    instructions=(
        "Beagle — unified agentic workflow engine surface. RAG search/graph "
        "traversal, workflow orchestration, research and code tools, plus "
        "[tool]-standard plugins (OpenClaw task queue). Use plugin_status / "
        "plugin_reload to manage plugin mounts at runtime."
    ),
)


# ── Absorption plumbing ───────────────────────────────────────────────────────


@dataclass(slots=True)
class AbsorbReport:
    """Outcome of absorbing one source server into the root."""

    owner: str
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # name -> reason


def _absorb(
    source: FastMCP,
    *,
    owner: str,
    prefix: str = "",
    renames: dict[str, str] | None = None,
) -> AbsorbReport:
    """Transfer tools/resources/templates from ``source`` onto ``app``.

    Tool objects are moved whole (preserving context kwargs and fn metadata).
    ``renames`` maps original tool name -> final name BEFORE prefixing;
    collisions are first-wins with the loser recorded in ``skipped``.
    """
    report = AbsorbReport(owner=owner)
    renames = renames or {}

    existing_tools = app._tool_manager.list_tools()
    taken: set[str] = {t.name for t in existing_tools}

    for tool in source._tool_manager.list_tools():
        final = renames.get(tool.name, tool.name)
        if prefix:
            final = f"{prefix}{final}"
        if final in taken:
            report.skipped[f"tool:{tool.name}"] = f"name collision on '{final}' (first-wins)"
            continue
        # Whole-object insert keeps context_kwarg / fn_metadata intact.
        app._tool_manager._tools[final] = tool.model_copy(update={"name": final})
        taken.add(final)
        report.tools.append(final)

    for uri, res in list(source._resource_manager._resources.items()):
        final_uri = f"{prefix}{uri}" if prefix else uri
        if final_uri in app._resource_manager._resources:
            report.skipped[f"resource:{uri}"] = "uri collision (first-wins)"
            continue
        app._resource_manager._resources[final_uri] = res.model_copy(update={"uri": final_uri})
        report.resources.append(final_uri)

    for tmpl_key, tmpl in list(source._resource_manager._templates.items()):
        final_key = f"{prefix}{tmpl_key}" if prefix else tmpl_key
        if final_key in app._resource_manager._templates:
            report.skipped[f"template:{tmpl_key}"] = "template collision (first-wins)"
            continue
        app._resource_manager._templates[final_key] = tmpl.model_copy(
            update={"uri_template": final_key}
        )
        report.templates.append(final_key)

    return report


def _unmount(report: AbsorbReport) -> None:
    """Remove everything a previous _absorb mounted for this owner."""
    with SWAP_LOCK:
        for name in report.tools:
            try:
                app._tool_manager.remove_tool(name)
            except Exception as exc:  # noqa: BLE001 - unmount must not crash reload
                logger.warning("unmount %s: tool '%s': %s", report.owner, name, exc)
        for uri in report.resources:
            app._resource_manager._resources.pop(uri, None)
        for key in report.templates:
            app._resource_manager._templates.pop(key, None)


def _source_tool_count(source: Any) -> int:
    try:
        return len(source._tool_manager.list_tools())
    except Exception:  # noqa: BLE001 - introspection only
        return -1


# ── Core groups (static code, always present) ─────────────────────────────────

_CORE_REPORTS: dict[str, AbsorbReport] = {}


def _load_core_groups() -> None:
    """Absorb the rag and utility servers.

    Utility absorbs FIRST so its aggregate health_check/get_metrics keep the
    canonical names; the RAG variants are renamed with an explicit map rather
    than silently dropped by the collision policy.
    """
    from . import mcp_utility_server

    _CORE_REPORTS["utility"] = _absorb(mcp_utility_server.mcp, owner="core:utility")

    from . import mcp_rag_server

    _CORE_REPORTS["rag"] = _absorb(
        mcp_rag_server.mcp,
        owner="core:rag",
        renames={
            "health_check": "rag_health_check",
            "get_metrics": "rag_get_metrics",
        },
    )
    for owner, rep in _CORE_REPORTS.items():
        logger.info(
            "core group %s absorbed: %d tools, %d resources, %d templates, %d skipped",
            owner,
            len(rep.tools),
            len(rep.resources),
            len(rep.templates),
            len(rep.skipped),
        )


# ── Registry TOML ([tool] standard) ───────────────────────────────────────────


@dataclass(slots=True)
class ToolEntry:
    """One [[tool]] declaration from the registry TOML."""

    name: str
    source: str = "entry_point"
    group: str = "beagle.mcp_plugins"
    key: str = ""
    module: str = ""
    enabled: bool = True
    prefix: str = ""
    description: str = ""


@dataclass(slots=True)
class RegistryState:
    """Parsed registry plus load diagnostics."""

    hotswap_enabled: bool = True
    auto_enable_detected: bool = False
    entries: list[ToolEntry] = field(default_factory=list)
    path: Path | None = None
    error: str = ""


def registry_path() -> Path:
    """The [tool] registry TOML location (config-root resolved)."""
    return find_plugins_dir() / REGISTRY_FILENAME


def read_registry(*, refresh_cache: bool = False) -> RegistryState:
    """Read and validate ``plugins/tools.toml``.

    Missing file -> empty-but-valid registry (core-only surface).
    Malformed file -> error recorded; caller decides fail-open vs fail-closed.
    """
    if refresh_cache:
        reset_config_path_cache()
    state = RegistryState(path=registry_path())
    registry = state.path
    if registry is None or not registry.is_file():
        return state
    try:
        import tomllib

        with registry.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        state.error = f"unreadable: {exc}"
        return state

    meta = data.get("meta", {})
    if isinstance(meta, dict):
        state.hotswap_enabled = bool(meta.get("hotswap_enabled", True))
        state.auto_enable_detected = bool(meta.get("auto_enable_detected", False))

    raw_entries = data.get("tool", [])
    if not isinstance(raw_entries, list):
        state.error = "[[tool]] must be an array of tables"
        return state
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            state.error = f"[[tool]] entry #{i} is not a table"
            return state
        name = str(raw.get("name", "")).strip()
        if not name:
            state.error = f"[[tool]] entry #{i} missing 'name'"
            return state
        source = str(raw.get("source", "entry_point")).strip()
        if source not in ("entry_point", "module"):
            state.error = f"[[tool]] {name}: unsupported source '{source}'"
            return state
        if source == "entry_point" and not str(raw.get("group", "")).strip():
            state.error = f"[[tool]] {name}: 'group' required for source=entry_point"
            return state
        if source == "module" and not str(raw.get("module", "")).strip():
            state.error = f"[[tool]] {name}: 'module' required for source=module"
            return state
        state.entries.append(
            ToolEntry(
                name=name,
                source=source,
                group=str(raw.get("group", "beagle.mcp_plugins")),
                key=str(raw.get("key", name)),
                module=str(raw.get("module", "")),
                enabled=bool(raw.get("enabled", True)),
                prefix=str(raw.get("prefix", "")),
                description=str(raw.get("description", "")),
            )
        )

    seen: set[str] = set()
    for entry in state.entries:
        if entry.name in seen:
            state.error = f"duplicate [[tool]] name '{entry.name}'"
            return state
        seen.add(entry.name)
    return state


# ── Plugin discovery & resolution ────────────────────────────────────────────


@dataclass(slots=True)
class PluginView:
    """Everything known about one candidate plugin (for status + reconcile)."""

    name: str
    source: str
    listed: bool = False  # has an explicit [[tool]] entry
    toml_enabled: bool = False  # registry gate
    self_enabled: bool | None = None  # plugin's own is_enabled(), None = no probe
    discovered: bool = False  # resolvable via its source
    importable: bool = False  # loaded without exception
    mounted: bool = False
    error: str = ""
    tools: list[str] = field(default_factory=list)
    description: str = ""


def _scan_entry_group(group: str) -> dict[str, tuple[Any, str, str]]:
    """Scan an entry-point group -> {key: (loaded_object, error, defining_module)}."""
    from importlib.metadata import entry_points

    found: dict[str, tuple[Any, str, str]] = {}
    try:
        eps = list(entry_points(group=group))
    except Exception as exc:  # noqa: BLE001 - discovery never crashes callers
        logger.warning("entry-point scan of %s failed: %s", group, exc)
        return found
    for ep in eps:
        try:
            found[ep.name] = (ep.load(), "", ep.module)
        except Exception as exc:  # noqa: BLE001 - one bad plugin hides none
            found[ep.name] = (None, f"import failed: {exc}", "")
    return found


def _resolve_plugin_mcp(obj: Any) -> FastMCP:
    """Extract the FastMCP instance from a loaded plugin object.

    Accepts: a FastMCP instance, any object exposing ``mcp``, or a callable
    factory returning either of the former.
    """
    target = obj
    if isinstance(target, FastMCP):
        return target
    if callable(target) and not hasattr(target, "mcp"):
        target = target()
    candidate = getattr(target, "mcp", target)
    if isinstance(candidate, FastMCP):
        return candidate
    raise TypeError(f"plugin object {type(obj).__name__} does not expose a FastMCP 'mcp'")


def _plugin_self_gate(obj: Any, owner_module: str = "") -> bool | None:
    """Probe a plugin's own enablement gate.

    Resolution chain: the loaded object's ``is_enabled``, then the callable
    product's (entry-point factory), then the entry point's defining module.
    None = plugin exposes no gate (registry TOML is the only gate).
    """

    def _probe(target: Any) -> Any:
        probe = getattr(target, "is_enabled", None)
        return probe if callable(probe) else None

    probe = _probe(obj)
    if probe is None and callable(obj) and not isinstance(obj, FastMCP):
        try:
            probe = _probe(obj())
        except Exception as exc:  # noqa: BLE001 - factory failure = disabled
            logger.warning("plugin factory raised during gate probe (%s)", exc)
            return False
    if probe is None and owner_module:
        try:
            import importlib

            probe = _probe(importlib.import_module(owner_module))
        except Exception as exc:  # noqa: BLE001 - import failure leaves gate unknown
            logger.warning("owner-module gate probe failed (%s)", exc)
    if probe is not None:
        try:
            return bool(probe())
        except Exception as exc:  # noqa: BLE001 - gate failure = disabled
            logger.warning("plugin self-gate raised (%s) — treating as disabled", exc)
            return False
    return None


# ── Mount bookkeeping & reconcile ────────────────────────────────────────────

_MOUNTED: dict[str, AbsorbReport] = {}  # plugin name -> absorb report
_PLUGIN_VIEWS: dict[str, PluginView] = {}
_LAST_REGISTRY_ERROR: str = ""


def _desired_plugin_set(state: RegistryState) -> dict[str, ToolEntry]:
    """Registry-listed plugins whose gates pass, keyed by name."""
    desired: dict[str, ToolEntry] = {}
    for entry in state.entries:
        if not entry.enabled:
            continue
        desired[entry.name] = entry
    return desired


def _mount_plugin(entry: ToolEntry, obj: Any, owner_module: str = "") -> PluginView:
    view = PluginView(name=entry.name, source=entry.source, listed=True, toml_enabled=True)
    try:
        plugin_mcp = _resolve_plugin_mcp(obj)
        view.self_enabled = _plugin_self_gate(obj, owner_module)
        if view.self_enabled is False:
            view.error = "disabled by plugin self-gate (its own TOML)"
            return view
        report = _absorb(plugin_mcp, owner=f"plugin:{entry.name}", prefix=entry.prefix)
        _MOUNTED[entry.name] = report
        view.mounted = True
        view.importable = True
        view.tools = report.tools
        desc = getattr(obj, "PLUGIN_DESCRIPTION", "")
        if not desc and owner_module:
            try:
                import importlib

                desc = getattr(importlib.import_module(owner_module), "PLUGIN_DESCRIPTION", "")
            except Exception:  # noqa: BLE001 - description is best-effort
                desc = ""
        view.description = entry.description or desc
        logger.info("plugin '%s' mounted: %d tools", entry.name, len(report.tools))
    except Exception as exc:  # noqa: BLE001 - plugin failure must not kill server
        view.importable = False
        view.error = str(exc)
        logger.warning("plugin '%s' failed to mount: %s", entry.name, exc)
    return view


def reconcile_plugins(reason: str = "manual") -> dict[str, Any]:
    """Bring mounted plugins in line with the registry. Hotswap core."""
    global _LAST_REGISTRY_ERROR
    with SWAP_LOCK:
        state = read_registry(refresh_cache=True)
        _LAST_REGISTRY_ERROR = state.error
        views: dict[str, PluginView] = {}

        if state.error:
            # Fail-closed to last-known-good: leave current mounts untouched.
            logger.error("registry unreadable (%s) — keeping last-good mounts", state.error)
            for name, rep in _MOUNTED.items():
                views[name] = PluginView(
                    name=name,
                    source="?",
                    listed=True,
                    toml_enabled=True,
                    mounted=True,
                    tools=list(rep.tools),
                    error=f"registry error kept last-good: {state.error}",
                )
            _PLUGIN_VIEWS.clear()
            _PLUGIN_VIEWS.update(views)
            return {"reason": reason, "registry_error": state.error, "mounted": sorted(_MOUNTED)}

        if not state.hotswap_enabled and _MOUNTED:
            # Hotswap disabled: static startup mounts remain, runtime changes refused.
            pass

        # Resolve every listed plugin.
        desired = _desired_plugin_set(state)
        resolved: dict[str, tuple[Any, str, str]] = {}
        for name, entry in desired.items():
            if entry.source == "entry_point":
                scanned = _scan_entry_group(entry.group)
                hit = scanned.get(entry.key)
                if hit is None:
                    views[name] = PluginView(
                        name=name,
                        source=entry.source,
                        listed=True,
                        toml_enabled=True,
                        error=f"'{entry.key}' not found in group '{entry.group}'",
                    )
                    continue
                obj, err, owner_mod = hit
                resolved[name] = (obj, err, owner_mod)
            else:
                try:
                    import importlib

                    resolved[name] = (importlib.import_module(entry.module), "", entry.module)
                except Exception as exc:  # noqa: BLE001
                    views[name] = PluginView(
                        name=name,
                        source=entry.source,
                        listed=True,
                        toml_enabled=True,
                        error=f"module import failed: {exc}",
                    )

        for name, (obj, err, owner_mod) in resolved.items():
            if err:
                views[name] = PluginView(
                    name=name,
                    source=desired[name].source,
                    listed=True,
                    toml_enabled=True,
                    error=err,
                )
            elif desired[name].enabled:
                views[name] = _mount_plugin(desired[name], obj, owner_mod)

        # Unmount anything mounted but no longer desired.
        for name in sorted(set(_MOUNTED) - set(desired)):
            rep = _MOUNTED.pop(name)
            _unmount(rep)
            logger.info("plugin '%s' unmounted (%s)", name, reason)

        # Auto-detected, unlisted plugins: report only (never auto-mount unless
        # meta.auto_enable_detected=true — explicit opt-in per plugin docs).
        detected = _scan_entry_group("beagle.mcp_plugins")
        for key, (obj, err, owner_mod) in detected.items():
            if key in views:
                continue
            v = PluginView(name=key, source="entry_point", listed=False, discovered=obj is not None)
            if err:
                v.error = err
            else:
                v.self_enabled = _plugin_self_gate(obj, owner_mod)
                if state.auto_enable_detected and v.self_enabled is not False:
                    fake_entry = ToolEntry(
                        name=key, source="entry_point", group="beagle.mcp_plugins"
                    )
                    views[key] = _mount_plugin(fake_entry, obj, owner_mod)
                    views[key].listed = False
                    continue
            views[key] = v

        _PLUGIN_VIEWS.clear()
        _PLUGIN_VIEWS.update(views)
        return {
            "reason": reason,
            "registry_error": "",
            "mounted": sorted(_MOUNTED),
            "known": sorted(_PLUGIN_VIEWS),
        }


# ── Management tools (always available) ──────────────────────────────────────


@app.tool()
async def plugin_status() -> str:
    """Report the unified Beagle surface: core groups, [tool] registry state, and every known plugin (mounted / gated / errored)."""
    state = read_registry()
    payload: dict[str, Any] = {
        "server": "Beagle",
        "registry": {
            "path": str(state.path),
            "exists": bool(state.path and state.path.is_file()),
            "error": state.error or _LAST_REGISTRY_ERROR,
            "hotswap_enabled": state.hotswap_enabled,
            "auto_enable_detected": state.auto_enable_detected,
        },
        "core": {
            owner: {
                "tools": len(rep.tools),
                "resources": len(rep.resources),
                "templates": len(rep.templates),
                "skipped": rep.skipped,
            }
            for owner, rep in _CORE_REPORTS.items()
        },
        "total_tools": len(app._tool_manager.list_tools()),
        "plugins": [
            {
                "name": v.name,
                "source": v.source,
                "listed": v.listed,
                "toml_enabled": v.toml_enabled,
                "self_enabled": v.self_enabled,
                "discovered": v.discovered,
                "mounted": v.mounted,
                "tools": v.tools,
                "description": v.description,
                "error": v.error,
            }
            for v in sorted(_PLUGIN_VIEWS.values(), key=lambda x: x.name)
        ],
    }
    return json.dumps(payload, indent=2)


@app.tool()
async def plugin_reload(plugin_name: str = "") -> str:
    """Re-read the [tool] registry TOML and reconcile plugin mounts WITHOUT restarting (hotswap). Pass plugin_name to scope reporting to one plugin; all plugins are reconciled regardless."""
    state = read_registry()
    if state.path and state.path.is_file() and not state.hotswap_enabled:
        return json.dumps(
            {
                "error": "hotswap disabled by registry [meta].hotswap_enabled=false",
                "registry": str(state.path),
                "mounted_unchanged": sorted(_MOUNTED),
            },
            indent=2,
        )
    result = reconcile_plugins(reason=f"reload({plugin_name or 'all'})")
    if plugin_name:
        view = _PLUGIN_VIEWS.get(plugin_name)
        result["scoped"] = (
            {
                "name": view.name,
                "mounted": view.mounted,
                "tools": view.tools,
                "error": view.error,
                "toml_enabled": view.toml_enabled,
                "self_enabled": view.self_enabled,
            }
            if view
            else {"name": plugin_name, "error": "unknown plugin"}
        )
    return json.dumps(result, indent=2)


# ── Boot ─────────────────────────────────────────────────────────────────────


def build_app() -> FastMCP:
    """Assemble the full unified surface (idempotent per process)."""
    if not _CORE_REPORTS:
        _load_core_groups()
        reconcile_plugins(reason="startup")
    return app


if __name__ == "__main__":
    # Consistent --version across dev-tool entry points.
    from .mcp_common import maybe_print_version

    if maybe_print_version():
        raise SystemExit(0)

    # SECURITY FLOOR: stdio only. HTTP transports require mandatory bearer
    # auth (doctrine); the unified surface is local-first and does not open
    # a network listener.
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        raise RuntimeError(
            f"transport='{transport}' REJECTED by mcp_beagle_server: only stdio "
            "is permitted for the unified surface."
        )
    server = build_app()
    logger.info("Unified Beagle MCP starting (tools=%d)", len(server._tool_manager.list_tools()))
    server.run(transport="stdio")
