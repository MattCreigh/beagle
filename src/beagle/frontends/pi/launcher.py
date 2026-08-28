"""Locate and spawn the vendored pi frontend CLI.

The pi frontend is a self-contained Node bundle (``@earendil-works/pi-coding-agent``)
shipped inside the Beagle wheel under ``vendor/pi-prebuild/dist/bundle/cli.js``.
It is not importable Python — it runs under ``node``. This module resolves the
bundle path whether Beagle runs from a source checkout or an installed wheel,
checks that ``node`` is available, and hands off to it.

``beagle`` with no subcommand launches this frontend (the default interactive
experience). See ``beagle/frontends/pi/README.md``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import NoReturn

logger = logging.getLogger(__name__)


def _pi_vendor_dir() -> Path:
    """Return the vendored pi tree root (source checkout or installed wheel)."""
    here = Path(__file__).resolve()
    for root in (here.parent / "vendor", here.parents[2] / "frontends" / "pi" / "vendor"):
        if (root / "pi-prebuild" / "dist" / "bundle" / "cli.js").is_file():
            return root
    raise FileNotFoundError("Vendored pi tree not found; reinstall the Beagle wheel.")


def _mcp_server_module() -> str:
    """Return the import path of Beagle's MCP server (runs as a stdio child)."""
    return "beagle.infrastructure.mcp_beagle_server"


def _extension_path() -> Path:
    """Resolve the pi-mcp-extension entrypoint shipped with the wheel."""
    ext = _pi_vendor_dir() / "pi-mcp-extension" / "src" / "index.ts"
    if not ext.is_file():
        raise FileNotFoundError(f"pi MCP bridge extension not found at {ext}.")
    return ext


def _write_mcp_config(project_dir: Path) -> Path:
    """Write (or refresh) a default ``.pi/mcp.json`` wiring Beagle's MCP server.

    Uses the same convention as pi-mcp-extension: project-level ``.pi/mcp.json``
    wins over the global one. We point the ``beagle`` server at our own bundled
    MCP server over stdio, so a fresh install is immediately connected. Existing
    config is left untouched except that we ensure a ``beagle`` server entry.
    """
    import sys as _sys

    cfg_dir = project_dir / ".pi"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "mcp.json"

    cfg: dict = {"settings": {}, "mcpServers": {}}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text())
        except (OSError, ValueError):
            cfg = {"settings": {}, "mcpServers": {}}

    cfg.setdefault("mcpServers", {})
    cfg["mcpServers"]["beagle"] = {
        "transport": "stdio",
        "command": _sys.executable,
        "args": ["-m", _mcp_server_module()],
        "lifecycle": "eager",
    }
    cfg_path.write_text(json.dumps(cfg, indent=2))
    return cfg_path


def bundle_path() -> Path:
    """Resolve the vendored pi CLI bundle path, source checkout or installed.

    The bundle ships as package data under
    ``beagle/frontends/pi/vendor/pi-prebuild/dist/bundle/cli.js``. Locate it
    relative to this module's file, which is stable for both a source tree and
    an installed wheel (site-packages/beagle/frontends/pi/launcher.py).
    """
    here = Path(__file__).resolve()
    bundle = here.parent / "vendor" / "pi-prebuild" / "dist" / "bundle" / "cli.js"
    if bundle.is_file():
        return bundle
    # Fall back to a source checkout that keeps the tree one level up.
    alt = here.parents[2] / "frontends" / "pi" / "vendor" / "pi-prebuild" / "dist" / "bundle" / "cli.js"
    if alt.is_file():
        return alt
    raise FileNotFoundError(
        "Vendored pi frontend bundle not found. Expected at "
        f"{bundle} or {alt}. Reinstall the Beagle wheel."
    )


def _required_node_version() -> str:
    """Return the Node.js floor that the vendored bundle declares for itself.

    <invariant>
    The floor has exactly ONE source: the ``engines.node`` field of the
    vendored ``pi-prebuild/package.json``. Never restate it as a literal in
    Python. Three hardcoded copies had already drifted apart (``>=18``,
    ``>= 20`` and ``>=22.19.0``) before this helper replaced them, and the
    drift was silent because nothing compares them.
    </invariant>

    Returns:
        The declared range, for example ``">=22.19.0"``. Returns ``""`` when
        the manifest is absent or unreadable, so the caller degrades to a
        message with no version rather than a wrong one.

    """
    try:
        manifest = _pi_vendor_dir() / "pi-prebuild" / "package.json"
        engines = json.loads(manifest.read_text()).get("engines", {})
    except (OSError, ValueError, FileNotFoundError):
        return ""
    declared = engines.get("node", "") if isinstance(engines, dict) else ""
    return declared if isinstance(declared, str) else ""


def _resolve_node() -> str:
    """Resolve a usable ``node`` binary.

    Returns:
        Absolute path to the ``node`` executable.

    Raises:
        RuntimeError: when ``node`` is not on ``PATH``. The message quotes the
            floor from the vendored manifest, not a literal.

    """
    node = shutil.which("node")
    if node:
        return node
    declared = _required_node_version()
    requirement = f" ({declared})" if declared else ""
    raise RuntimeError(
        f"The pi frontend requires Node.js{requirement}. Install Node or add it to PATH."
    )


def run(argv: list[str] | None = None) -> NoReturn:
    """Run the vendored pi CLI, replacing the current process.

    ``node`` is a hard requirement (the pi bundle is JavaScript). The process
    env is inherited, so ``PI_*``/``BEAGLE_*`` config or extension dirs set by
    the caller are honoured.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    bundle = bundle_path()
    node = _resolve_node()

    # Plug-and-play MCP bridge: ensure a default .pi/mcp.json wiring the
    # bundled Beagle MCP server over stdio, and preload the pi-mcp-extension
    # so pi can call Beagle's autonomous agents out of the box.
    try:
        ext = _extension_path()
        _write_mcp_config(Path.cwd())
        # Preload the MCP extension so no manual `pi install` is required.
        if not any(a == "--extension" or a.startswith("-e") for a in argv):
            argv = [f"--extension={ext}"] + argv
    except FileNotFoundError as exc:
        # The bridge is best-effort; the frontend still opens without it.
        logger.warning("pi MCP bridge unavailable: %s", exc)

    logger.info("Launching pi frontend: %s %s", node, bundle)
    # Replace the Python process so terminal key handling is not mediated by
    # typer/subprocess buffering. This also keeps ``pi``'s TUI in control of
    # stdin/stdout. execvpe does not return on success.
    os.execvpe(node, [node, str(bundle), *argv], {**os.environ, "PI_BEAGLE_BUNDLE": str(bundle)})
    raise SystemExit(1)  # defensive: only reached if exec fails


def main() -> int:
    """Entry point used by the ``beagle`` console script when no subcommand is given."""
    try:
        run()
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"beagle: {exc}", file=sys.stderr)
        return 1
    raise SystemExit(0)  # unreachable on success — run() replaces the process


if __name__ == "__main__":
    sys.exit(main())
