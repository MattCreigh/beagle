"""Shared helpers for CLI command modules (cross-group).

Extracted from cli.py in the v1.0.0 F2 split. Registered flat on the root
app via ``app.add_typer(_unused_common)`` (no name), so the CLI surface is
byte-identical to the pre-split monolith.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from ...config._config_path import find_metaprompts_dir

logger = logging.getLogger("Beagle.cli.commands")

console = Console()


def _resolve_workflow(workflow: str) -> Path:
    """Resolve workflow name to path.

    v13.21.3: hardened the explicit-path short-circuit so it only triggers
    on absolute or explicit-relative paths (containing a '/' or starting
    with '~' / './' / '../'). A bare name like 'audit' that happens to
    collide with a directory in cwd (e.g. project-level 'audit/' reports
    folder) will now fall through to the metaprompts lookup instead of
    returning the wrong directory.
    """
    # S5/S6: workflow/metaprompts data is detached to the canonical config
    # root, NOT the workspace/package dir. A wheel install has no workflow YAMLs
    # in-package; resolve through find_metaprompts_dir() (same canonical path
    # workflow_loader.py uses). v1.2.0 regression fix: a non-editable deploy
    # made get_workspace_root()/metaprompts resolve to site-packages, which
    # has no *.yaml, so `beagle run <workflow>` raised FileNotFoundError.
    metaprompts_dir = find_metaprompts_dir()

    # Only treat the argument as an explicit filesystem path if it looks
    # like one (absolute, or contains a separator / '~' / '..'). Bare
    # names are always looked up in metaprompts/.
    if Path(workflow).is_absolute() or "/" in workflow or workflow.startswith(("~", "./", "../")):
        p = Path(workflow).expanduser()
        if p.exists():
            return p

    # Try exact match (canonical name, e.g. "audit")
    path = metaprompts_dir / workflow
    if path.exists():
        return path

    # Try with .yaml extension
    path = metaprompts_dir / f"{workflow}.yaml"
    if path.exists():
        return path

    # Try hyphen↔underscore variants (e.g. "db_migration" → "db-migration.yaml")
    for variant in [
        f"{workflow.replace('-', '_')}.yaml",
        f"{workflow.replace('_', '-')}.yaml",
    ]:
        path = metaprompts_dir / variant
        if path.exists():
            return path

    # Try to find by matching the 'name' field in YAML files
    try:
        import yaml

        for yaml_path in metaprompts_dir.glob("*.yaml"):
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    spec = yaml.safe_load(f)
                if spec and spec.get("name") == workflow:
                    return yaml_path
            except OSError as exc:
                logger.warning(
                    "Cannot read workflow spec %s (%s); skipping it, so a workflow "
                    "defined there will not be found.",
                    yaml_path,
                    exc,
                )
                continue
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.warning(
            "Cannot scan the metaprompts directory for workflow %r (%s); "
            "reporting the workflow as not found.",
            workflow,
            exc,
        )

    raise FileNotFoundError(f"Workflow not found: {workflow}")
