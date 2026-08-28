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

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import NoReturn

logger = logging.getLogger(__name__)


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


def _resolve_node() -> str:
    """Resolve a usable ``node`` binary."""
    node = shutil.which("node")
    if node:
        return node
    raise RuntimeError(
        "The pi frontend requires Node.js (>= 20). Install Node or add it to PATH."
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
