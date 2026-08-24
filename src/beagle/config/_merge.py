"""Merge & composition primitives for the registry (plan v2, B3).

Separates render-time from merge-time: these functions operate on **parsed**
TOML data (after ``tomllib``), never on raw template text. Bundle expansion is
a data operation, not a Jinja interpolation.

Public surface:
    - ``deep_merge(base, override)`` — recursive dict merge (lists replace,
      nested dicts merge), with tombstone support ('~' deletes a key).
    - ``expand_bundle(bundle, presets, role_resolver)`` — resolve a's named
      bundle into a concrete ``{role: ModelPreset}`` map, applying overrides.
    - ``apply_tombstones`` — helper so a live overlay can delete a preset key
      rather than only override it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

# Sentinel marking a key for deletion in a merge (explicit delete, not just
# override). Mirrors the '~' tombstone used by several TOML overlay tools.
_TOMBSTONE = object()


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*.

    Rules:
      - dicts merge recursively (keys unioned, nested dicts combined);
      - non-dict values in *override* replace the base value;
      - a value equal to the tombstone sentinel deletes the key from the result
        (``apply_tombstones`` makes this ergonomic from TOML's ``"~"``).
    """
    result = dict(base)
    for key, value in override.items():
        if value is _TOMBSTONE:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def apply_tombstones(data: dict, tombstone_value: Any = "~") -> dict:
    """Convert literal ``"~"`` values into delete-tombstones before merging.

    Lets a live overlay express ``key = "~"`` to *delete* a preset key rather
    than override it with the literal string ``"~"``.
    """
    out: dict = {}
    for key, value in data.items():
        if value == tombstone_value:
            out[key] = _TOMBSTONE
        elif isinstance(value, dict):
            out[key] = apply_tombstones(value, tombstone_value)
        else:
            out[key] = value
    return out


def expand_bundle(
    bundle_name: str,
    includes: list[str],
    overrides: dict,
    role_resolver: Callable[[str], Any],
) -> dict[str, Any]:
    """Expand a named bundle into a concrete ``{role: preset}`` map.

    Args:
        bundle_name: The bundle's name (for diagnostics only).
        includes: Ordered list of role names the bundle composes.
        overrides: Structural overrides applied LAST (deep-merged onto each
            resolved preset's dict form).
        role_resolver: ``role -> ModelPreset``; used to materialise each
            included role.

    Returns:
        ``{role_name: resolved_preset}`` with overrides applied. Order follows
        *includes*.

    """
    resolved: dict[str, Any] = {}
    clean_overrides = apply_tombstones(dict(overrides))
    budget_override = clean_overrides.get("budget_usd")

    for role in includes:
        preset = role_resolver(role)
        if preset is None:
            # Validation should have caught this earlier; be loud, not silent.
            raise KeyError(f"bundle '{bundle_name}' includes unknown role '{role}'")
        resolved[role] = preset

    # v1: the only bundle-level override we apply structurally is budget_usd.
    # Routing-only (7.6): we do NOT enforce toolset or rebuild agent topology.
    if budget_override is not None:
        with contextlib.suppress(TypeError, ValueError):
            for preset in resolved.values():
                preset.budget_usd = float(budget_override)
    return resolved


__all__ = ["apply_tombstones", "deep_merge", "expand_bundle"]
