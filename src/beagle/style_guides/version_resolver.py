"""Dynamic version and project facts resolver.

Reads the canonical package version from importlib.metadata and other
dynamic facts from pyproject.toml / config.toml so downstream renderers
never hardcode version strings.

Used by GooseTopOfMindRenderer (render.py) and CLI (cli/cli.py) to
generate .goose/project.json, CLAUDE.md, README markers, etc.
"""

from __future__ import annotations

import importlib.metadata
import logging
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger("Beagle.style_guides.version_resolver")

_PKG = "beagle"
_IMPORT_NAME = "beagle"


def get_version(repo_root: Path | None = None) -> str:
    """Return the canonical package version.

    Source of truth is ``pyproject.toml`` ``[project].version`` (always current in
    the repo). ``importlib.metadata`` is only a fallback — under an editable/dev
    install its dist-info lags behind the source until the next real wheel install,
    which would emit a stale version into generated steering files.
    """
    try:
        version = get_pyproject(repo_root).get("project", {}).get("version")
        if version:
            return str(version)
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        logger.warning(
            "Cannot read [project].version from pyproject.toml (%s); falling back to the "
            "installed package metadata, which lags the source under an editable install.",
            exc,
        )
    return importlib.metadata.version(_PKG)


def get_pyproject(repo_root: Path | None = None) -> dict[str, Any]:
    """Load pyproject.toml from *repo_root* (default: auto-detect)."""
    root = _resolve_repo_root(repo_root)
    path = root / "pyproject.toml"
    if not path.is_file():
        raise FileNotFoundError(f"pyproject.toml not found at {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)


def get_config(repo_root: Path | None = None) -> dict[str, Any]:
    """Load config.toml from *repo_root* (default: auto-detect).

    When *repo_root* is not given and the repo-root walk does not turn up a
    config.toml, fall back to the canonical resolver in
    ``beagle.config._config_path``.

    ``_resolve_repo_root`` walks up from ``__file__`` looking for
    pyproject.toml. Under a wheel install this module lives in
    ``site-packages/beagle/style_guides/``, there is no pyproject.toml above
    it, and the parent-of-package heuristic lands on ``site-packages`` — so
    the lookup became ``site-packages/config.toml``, which does not exist.
    ``get_config()`` then returned ``{}`` and every caller saw an empty
    config: ``get_model_fallback_chain()`` raised "[goose].fallback_chain is
    required" against a config.toml that declares eight models.
    """
    # v1.1.1 (S4): config.toml is detached to the canonical config root. The
    # canonical resolver is the primary source of truth for its location.
    # An explicitly-passed repo_root that contains its own config.toml is
    # still honoured (used by tests that mutate a temp config, and by
    # callers that intentionally point at an alternate tree); otherwise we
    # fall back to the canonical resolver.
    root = _resolve_repo_root(repo_root)
    root_cfg = root / "config.toml"
    from ..config._config_path import find_config_toml

    path = root_cfg if root_cfg.is_file() else find_config_toml()
    if not path.is_file():
        logger.debug("config.toml not found at %s", path)
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def get_python_version_requirement(repo_root: Path | None = None) -> str:
    """Return the python_requires string from pyproject.toml (e.g. '>=3.12,<3.15')."""
    pyproject = get_pyproject(repo_root)
    requires = pyproject.get("project", {}).get("requires-python", ">=3.12")
    return str(requires)


def get_model_fallback_chain(repo_root: Path | None = None) -> list[str]:
    """Return the canonical model fallback chain from config.toml.

    SSOT (single source of truth) for the model chain is config.toml's
    [goose] fallback_chain. This function never falls back to a Python
    literal — if config.toml is missing or has no fallback_chain, we raise
    RuntimeError so the misconfiguration is loud at startup, not silent at
    runtime. The doctrine in beagle_core_directives.toml mandates
    "config.toml is SSOT for config"; a hard-coded Python fallback would
    silently bypass that contract.

    v13.21.3 — F6 fix: cross-validate the chain against the runtime
    allowlist (``[models.allowed]``). The chain and the allowlist are
    intentionally separate config sections (the chain is routing intent;
    the allowlist is the security perimeter), but every chain entry MUST
    be in the allowlist or the misconfiguration is silent until the LLM
    call returns a 404. The validation runs on every call so a stale
    cache cannot mask a config edit; tests that mutate config.toml in
    place should call ``config.allowlist.reload_allowlist()`` first.
    """
    config = get_config(repo_root)
    chain = config.get("goose", {}).get("fallback_chain")
    if not chain:
        raise RuntimeError(
            "config.toml [goose].fallback_chain is required and must be non-empty. "
            "Hard-coded Python defaults are forbidden by Beagle doctrine "
            "('config.toml is SSOT for config')."
        )
    # Cross-validate against the runtime allowlist (F6 fix). The
    # allowlist is in beagle.config.allowlist; this
    # module is in beagle.style_guides, so the import
    # is a sibling-of-parent: ``..config``. The import is local (not
    # top-of-file) to avoid a circular import — allowlist does not
    # import version_resolver, but it loads the same config.toml and
    # the import order on cold-start is fragile.
    from ..config.allowlist import validate_against_allowlist

    return validate_against_allowlist(list(chain), on_violation="raise")


def get_primary_model(repo_root: Path | None = None) -> str:
    """Return the primary model from config.toml.

    The primary is the first entry of [goose].fallback_chain. If the chain
    is missing, RuntimeError surfaces the misconfiguration loudly. This
    function never returns a Python literal default.
    """
    chain = get_model_fallback_chain(repo_root)
    return chain[0]


def get_workflow_list(repo_root: Path | None = None) -> list[str]:
    """Return available workflow names from the workflows directory."""
    root = _resolve_repo_root(repo_root)
    wf_dir = root / _IMPORT_NAME / "workflows"
    if not wf_dir.is_dir():
        return []
    return sorted(p.stem for p in wf_dir.glob("*.yaml") if p.is_file())


def _resolve_repo_root(repo_root: Path | None = None) -> Path:
    """Resolve the repository root from an explicit path or auto-detect."""
    if repo_root is not None:
        return repo_root.resolve()

    # Auto-detect: look for pyproject.toml upwards from this file
    candidate = Path(__file__).resolve()
    for _ in range(6):
        if (candidate / "pyproject.toml").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    # Fallback: use parent-of-package heuristic
    pkg_dir = Path(__file__).resolve().parents[1]  # style_guides → beagle
    return pkg_dir.parent
