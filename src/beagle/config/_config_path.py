"""Canonical configuration-root resolver — the ONE place that finds config.

All other modules MUST import from here. No duplicate path-computing
implementations, no ad-hoc parent-walking.

Resolution order for ``CONFIG_ROOT``:

  1. $BEAGLE_CONFIG_ROOT env var (explicit override for tests/ops)
  2. ~/.config/beagle            (platformdirs user config dir)
  3. <repo_root>/config          (source-tree legacy fallback)
  4. ~/.config/beagle            (unpopulated — stable writable fallback;
                                   the wheel ships no bundled config)

An operator keeps an existing root with ``export BEAGLE_CONFIG_ROOT=<existing root>``,
or with a symlink ``~/.config/beagle -> <existing root>``.

The configuration is *detached* from the source tree. Code never computes a
config path via ``Path(__file__).parent...``; every data file (config.toml,
providers.toml, presets/, agents.toml, style guides, metaprompts, recipes,
auth policy, deployments) is resolved through this module against
``find_config_root()``. Each resolver prefers the canonical location and falls
back to the legacy in-tree location until the data has moved, so the suite
stays green at every migration step.

The canonical layout under the config root:

  beagle_core_config/       foundational behaviour/setup (config.toml,
                            defaults, feature_flags, context_management,
                            paths, hardware, security, observability,
                            lifecycle, auth/)
  coding_agent_config/      goose<->openclaw wiring (goose, openclaw,
                            orchestrator, agents.toml, recipes/, workflows/,
                            metaprompts/, blocks/)
  beagle_inference_config/  model/inference providers (providers.toml,
                            presets/, inference/ fleet cards)
  style_guides/guides/      doctrine SSOT
  plugins/<name>/           per-plugin config, namespaced by plugin
  deployments/              per-deployment overrides
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("Beagle.config._config_path")

_CONFIG_PATH: Path | None = None
_CONFIG_ROOT: Path | None = None


def _legacy_repo_root() -> Path:
    """Return the legacy source-tree repo root (where config used to live).

    This file is at <repo>/src/beagle/config/_config_path.py, so the repo
    root is parents[3] of this file's directory... parents[2] of the file
    itself is the repo root.
    """
    return Path(__file__).resolve().parents[2]


def _user_config_dir() -> Path:
    """Return the platform user-config directory for beagle.

    Uses ``platformdirs.user_config_dir("beagle")`` (``~/.config/beagle``
    on Linux). Imported lazily so the module stays importable even when
    platformdirs is not yet installed (e.g. during a partial install).

    Returns:
        The user-config directory path.

    """
    try:
        import platformdirs

        return Path(platformdirs.user_config_dir("beagle"))
    except ImportError:
        return Path.home() / ".config" / "beagle"


def find_config_root() -> Path:
    """Return the canonical configuration root.

    Cached on first call. Use ``reset_config_path_cache()`` to force a
    re-scan.

    Resolution (each step gated on POPULATED, not bare is_dir, so an empty
    skeleton dir does not shadow a populated fallback):

      1. $BEAGLE_CONFIG_ROOT env var (explicit override)
      2. ~/.config/beagle (platformdirs user config dir)
      3. <repo_root>/config (legacy source-tree fallback)
      4. ~/.config/beagle unpopulated (stable writable fallback; no bundled config)
    """
    global _CONFIG_ROOT
    if _CONFIG_ROOT is not None:
        return _CONFIG_ROOT

    # 1. Explicit env override
    env_override = os.environ.get("BEAGLE_CONFIG_ROOT")
    if env_override:
        p = Path(env_override).expanduser()
        _CONFIG_ROOT = p
        return p

    # 2. Platform user-config dir, gated on POPULATED so an empty
    #    ~/.config/beagle (created by any tool that touches XDG dirs) does
    #    not shadow a valid repo or bundled config.
    user_config = _user_config_dir()
    if _dir_has(user_config / "beagle_core_config", ("config.toml",)):
        _CONFIG_ROOT = user_config
        return _CONFIG_ROOT

    # 3. Legacy source-tree fallback: <repo_root>/config, gated on POPULATED.
    legacy = _legacy_repo_root() / "config"
    if _dir_has(legacy / "beagle_core_config", ("config.toml",)):
        _CONFIG_ROOT = legacy
        return _CONFIG_ROOT

    # 4. No wheel-bundled defaults: the distribution ships NO configuration —
    #    all user-editable config lives under ~/.config/beagle (tier 2) and
    #    programmatic defaults live in beagle.config.defaults / schema. Return
    #    the user-config dir even when unpopulated so callers have a stable,
    #    writable location; `beagle config init` seeds it from code defaults.
    _CONFIG_ROOT = _user_config_dir()
    return _CONFIG_ROOT


def find_config_toml() -> Path:
    """Return the path to the canonical config.toml.

    Cached on first call. Use ``reset_config_path_cache()`` to force
    a re-scan (e.g. in tests that create a temp config.toml).
    """
    global _CONFIG_PATH
    if _CONFIG_PATH is not None:
        return _CONFIG_PATH

    # 1. Explicit env override
    env_override = os.environ.get("BEAGLE_CONFIG_TOML")
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file():
            _CONFIG_PATH = p
            return p

    # 2. Canonical: <config_root>/beagle_core_config/config.toml (resolved via
    #    find_config_root so the wheel-bundled default_config fallback works).
    canonical = find_config_root() / "beagle_core_config" / "config.toml"
    if canonical.is_file():
        _CONFIG_PATH = canonical
        return canonical

    # 3. Legacy source-tree locations (in priority order)
    pkg_dir = Path(__file__).resolve().parent
    for candidate in (
        pkg_dir.parents[2] / "config.toml",  # project root (legacy SSOT)
        pkg_dir.parents[0] / "config.toml",  # wheel: next to package source
        pkg_dir.parents[1] / "config.toml",  # legacy wheel layout
        pkg_dir.parents[3] / "config.toml",  # monorepo
    ):
        if candidate.is_file():
            _CONFIG_PATH = candidate
            return candidate

    # Default to canonical for the error message
    _CONFIG_PATH = canonical
    return _CONFIG_PATH


def reset_config_path_cache() -> None:
    """Clear the cached paths (for tests)."""
    global _CONFIG_PATH, _CONFIG_ROOT
    _CONFIG_PATH = None
    _CONFIG_ROOT = None


# ---------------------------------------------------------------------------
# Core config subdirectory resolvers
# ---------------------------------------------------------------------------


def find_core_config_dir() -> Path:
    """Return the core-config directory under the config root."""
    return find_config_root() / "beagle_core_config"


def _dir_has(candidate: Path, patterns: tuple[str, ...]) -> bool:
    """Return True if ``candidate`` contains at least one matching file.

    Used to prefer the canonical location only once it actually holds data,
    so an empty skeleton dir (created during the migration) does not shadow
    the populated legacy dir.
    """
    return candidate.is_dir() and any(m for p in patterns for m in candidate.glob(p))


def find_auth_dir() -> Path:
    """Return the auth policy directory (model.conf / policy.csv).

    Prefers the canonical ``beagle_core_config/auth``; falls back to the
    legacy in-package ``<pkg>/auth`` until the data has moved.
    """
    canonical = find_core_config_dir() / "auth"
    if _dir_has(canonical, ("model.conf", "policy.csv")):
        return canonical
    return Path(__file__).resolve().parents[1] / "auth"


def find_guides_dir() -> Path:
    """Return the style-guides directory (doctrine SSOT).

    Layered resolution (highest precedence first):

      1. ``$BEAGLE_STYLE_GUIDES_DIR`` env var — explicit override.
      2. Operator config root: ``<config_root>/style_guides/guides`` — when
         populated (has at least one ``.toml``), it is the authority and we
         defer to it ENTIRELY. This is the TOML-settable surface: an operator
         who wants file-based doctrine seeds it (``beagle config init``) and
         every guide is loaded from there, not the shipped defaults.
      3. In-package default: ``src/beagle/style_guides/guides`` (the doctrine
         defaults shipped with the wheel). This is what a clean checkout / CI
         uses when no operator config root is populated.

    A missing directory at every layer simply means zero guides are loaded.
    """
    # 1. Explicit env override.
    env_override = os.environ.get("BEAGLE_STYLE_GUIDES_DIR")
    if env_override:
        return Path(env_override).expanduser()

    # 2. Operator config root (defer entirely when populated).
    config_root_guides = find_config_root() / "style_guides" / "guides"
    if config_root_guides.is_dir() and _dir_has(config_root_guides, ("*.toml",)):
        return config_root_guides

    # 3. In-package default doctrine (bundled).
    #    This module lives at <pkg>/config/_config_path.py, so parents[1] is
    #    the package root.
    return Path(__file__).resolve().parents[1] / "style_guides" / "guides"


def find_context_management_toml() -> Path:
    """Return the context-management config TOML.

    Prefers the canonical ``beagle_core_config/context_management.toml``;
    falls back to a legacy in-package path until the data has moved.
    """
    return find_core_config_dir() / "context_management.toml"


# ---------------------------------------------------------------------------
# Coding-agent config resolvers
# ---------------------------------------------------------------------------


def find_coding_agent_dir() -> Path:
    """Return the coding-agent config directory under the config root."""
    return find_config_root() / "coding_agent_config"


def find_agents_toml() -> Path:
    """Return the agents.toml path (canonical XDG location).

    The fleet definition is OPERATOR configuration — the open-source wheel
    ships none. The canonical ``~/.config/beagle/coding_agent_config/agents.toml``
    is returned whether or not it exists yet, so callers can render a clear
    "not configured" state instead of silently reading a stale bundled file.
    """
    return find_coding_agent_dir() / "agents.toml"


def _find_populated_subdir(canonical: Path, legacy: Path, patterns: tuple[str, ...]) -> Path:
    """Return canonical if it holds data, else legacy."""
    if _dir_has(canonical, patterns):
        return canonical
    return legacy


def find_recipes_dir() -> Path:
    """Return the recipes directory.

    Prefers the canonical ``coding_agent_config/recipes`` once it holds data;
    falls back to the legacy in-package ``<pkg>/recipes`` until the data moves.
    """
    return _find_populated_subdir(
        find_coding_agent_dir() / "recipes",
        Path(__file__).resolve().parents[1] / "recipes",
        ("*.yaml", "*.xml", "*.yml", "*.md"),
    )


def find_workflows_dir() -> Path:
    """Return the workflows directory.

    Prefers the canonical ``coding_agent_config/workflows`` once it holds data;
    falls back to the legacy in-package ``<pkg>/workflows`` until the data moves.
    """
    return _find_populated_subdir(
        find_coding_agent_dir() / "workflows",
        Path(__file__).resolve().parents[1] / "workflows",
        ("*.yaml", "*.yml"),
    )


def find_metaprompts_dir() -> Path:
    """Return the metaprompts directory.

    Prefers the canonical ``coding_agent_config/metaprompts`` once it holds data;
    falls back to the legacy in-package ``<pkg>/metaprompts`` until the data moves.
    """
    return _find_populated_subdir(
        find_coding_agent_dir() / "metaprompts",
        Path(__file__).resolve().parents[1] / "metaprompts",
        ("*.toml", "*.yaml", "*.yml", "*.xml", "*.md"),
    )


def find_blocks_agents_dir() -> Path:
    """Return the blocks/agents directory.

    Prefers the canonical ``coding_agent_config/blocks`` once it holds data;
    falls back to the legacy in-package ``<pkg>/blocks/agents`` until the data moves.
    """
    return _find_populated_subdir(
        find_coding_agent_dir() / "blocks" / "agents",
        Path(__file__).resolve().parents[1] / "blocks" / "agents",
        ("*.toml",),
    )


# ---------------------------------------------------------------------------
# Inference config resolvers
# ---------------------------------------------------------------------------


def find_inference_config_dir() -> Path:
    """Return the inference-config directory under the config root."""
    return find_config_root() / "beagle_inference_config"


def find_registry_dir() -> Path:
    """Return the directory that owns providers.toml / presets.toml.

    Under the canonical layout this is ``beagle_inference_config``. Under the
    legacy layout it is the parent of config.toml (project root). The resolver
    prefers the canonical location once it holds data, else falls back so the
    migration stays green.
    """
    canonical = find_inference_config_dir()
    if _dir_has(canonical, ("providers.toml", "presets.toml", "inference/*.toml")):
        return canonical
    return find_config_toml().resolve().parent


def find_providers_toml() -> Path:
    """Return the path to providers.toml."""
    canonical = find_inference_config_dir() / "providers.toml"
    if canonical.is_file():
        return canonical
    return find_registry_dir() / "providers.toml"


def find_presets_toml() -> Path:
    """Return the path to presets.toml (legacy single-file registry)."""
    return find_registry_dir() / "presets.toml"


def find_presets_dir() -> Path:
    """Return the presets/ card directory.

    Prefers the canonical ``beagle_inference_config/inference`` (fleet cards);
    falls back to the legacy ``presets/`` beside config.toml.
    """
    canonical = find_inference_config_dir() / "inference"
    if _dir_has(canonical, ("*.toml",)):
        return canonical
    return find_registry_dir() / "presets"


def find_preset_cards() -> list[Path]:
    """Return the preset card files to load, in deterministic order.

    Resolution:
      1. If the preset-card directory exists, glob ``*.toml``.
         ``_index.toml`` is metadata, not a card, and is excluded. When
         ``_index.toml`` declares ``[meta].load_order``, listed cards are
         returned first (in that order) followed by any unlisted cards
         alphabetically. Without an index, all cards are returned
         alphabetically.
      2. If the card directory does not exist, fall back to the legacy single
         ``presets.toml`` file if it exists (returned as a one-element list).
      3. Otherwise an empty list (caller raises a descriptive error).

    A malformed ``_index.toml`` is logged and ignored (falls back to
    alphabetical ordering) rather than breaking startup.
    """
    presets_dir = find_presets_dir()

    if presets_dir.is_dir():
        all_cards = sorted(presets_dir.glob("*.toml"))
        index_path = presets_dir / "_index.toml"
        # Key cards by filename for robust lookup — avoids Path.__eq__
        # fragility when symlinks or bind-mounts produce different
        # string forms for the same inode (F2/F9).
        card_by_name: dict[str, Path] = {c.name: c for c in all_cards if c != index_path}

        if index_path.is_file():
            try:
                import tomllib

                with open(index_path, "rb") as f:
                    idx = tomllib.load(f)
                order = (idx.get("meta") or {}).get("load_order") or []
                ordered: list[Path] = []
                for name in order:
                    key = str(name)
                    if key in card_by_name:
                        ordered.append(card_by_name.pop(key))
                # Remaining unlisted cards, alphabetical (dict preserves
                # insertion order from the sorted glob above).
                ordered.extend(card_by_name.values())
                cards = ordered
            except (OSError, ValueError, KeyError, TypeError):
                # Malformed index — fall back to alphabetical. Log only at
                # debug; the cards themselves remain loadable.
                logger.debug("%s/_index.toml unreadable; using alphabetical order", presets_dir)
                cards = list(card_by_name.values())
        else:
            cards = list(card_by_name.values())
        return cards

    # Back-compat: card directory absent but legacy single file present.
    legacy = find_presets_toml()
    if legacy.is_file():
        return [legacy]

    return []


# ---------------------------------------------------------------------------
# Plugins / deployments resolvers
# ---------------------------------------------------------------------------


def find_plugins_dir() -> Path:
    """Return the plugins config directory (per-plugin namespaced TOMLs)."""
    return find_config_root() / "plugins"


def find_plugin_config(plugin_name: str) -> Path:
    """Return the config TOML for a named plugin.

    Args:
        plugin_name: The plugin name (used as the namespaced subdir).

    Returns:
        The path ``<config_root>/plugins/<plugin_name>/<plugin_name>.toml``.

    """
    return find_plugins_dir() / plugin_name / f"{plugin_name}.toml"


def find_deployments_dir() -> Path:
    """Return the deployments config directory.

    Prefers the canonical ``deployments``; falls back to the legacy in-package
    ``<pkg>/infrastructure`` until the data moves.
    """
    canonical = find_config_root() / "deployments"
    if canonical.is_dir():
        return canonical
    return Path(__file__).resolve().parents[1] / "infrastructure"
