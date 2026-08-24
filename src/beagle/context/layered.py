"""Layered context merging with explicit precedence (C2).

Context is assembled from ordered layers: **global doctrine**, then a
**directory override**, then **task-specific ephemeral state**. On a
conflict the later layer wins. This module implements the merge and the
staleness check that forces a re-render when a source TOML changes.

The order is fixed by the :class:`ContextLayer` enum, never by prose. See
the logic block in the module docstring for the exact rule.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any


class ContextLayer(IntEnum):
    """Precedence of context layers (later wins on a conflict)."""

    GLOBAL = 0
    DIRECTORY = 1
    TASK = 2


# <invariant>
# MERGED = TASK  if TASK has the key
#        = DIRECTORY if (¬TASK) ∧ DIRECTORY has the key
#        = GLOBAL if (¬TASK) ∧ (¬DIRECTORY) ∧ GLOBAL has the key
#        = default otherwise
#
# where:
#   TASK       := the task-scoped layer dict
#   DIRECTORY  := the directory-scoped layer dict
#   GLOBAL     := the global doctrine layer dict
#   default    := the caller-supplied default for the key (None if none)
# </invariant>


@dataclass
class ContextLayerData:
    """A single context layer's content and source.

    Attributes:
        layer: The precedence tier.
        data: Key/value content of this layer.
        source_path: The file the layer was loaded from, for staleness.
        source_fingerprint: sha256 digest of the source file at load time.

    """

    layer: ContextLayer
    data: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None
    source_fingerprint: str = ""


def _source_fingerprint(path: Path) -> str:
    """Return a stable fingerprint of a source file's current content.

    Args:
        path: The source file.

    Returns:
        A sha256 hex digest of the file bytes (empty string when missing).

    """
    try:
        digest = hashlib.sha256()
        digest.update(path.read_bytes())
        return digest.hexdigest()
    except OSError:
        return ""


@dataclass
class LayerContext:
    """A merged view over ordered context layers.

    Attributes:
        layers: The loaded layers in ascending precedence order.

    """

    layers: list[ContextLayerData] = field(default_factory=list)

    def add(
        self,
        layer: ContextLayer,
        data: dict[str, Any],
        source_path: Path | None = None,
    ) -> None:
        """Add or replace a layer's content.

        Args:
            layer: The precedence tier.
            data: The layer content.
            source_path: The file this layer was loaded from, if any.

        """
        fingerprint = _source_fingerprint(source_path) if source_path else ""
        # Replace an existing layer at the same tier.
        for existing_layer in self.layers:
            if existing_layer.layer == layer:
                existing_layer.data = dict(data)
                existing_layer.source_path = source_path
                existing_layer.source_fingerprint = fingerprint
                self.layers.sort(key=lambda x: x.layer)
                return
        self.layers.append(
            ContextLayerData(
                layer=layer,
                data=dict(data),
                source_path=source_path,
                source_fingerprint=fingerprint,
            )
        )
        self.layers.sort(key=lambda x: x.layer)

    def get(self, key: str, default: Any = None) -> Any:
        """Resolve a key across layers (later layer wins).

        Args:
            key: The key to resolve.
            default: Value returned when no layer has the key.

        Returns:
            The resolved value.

        """
        # Highest precedence first: iterate layers in descending order.
        for layer in reversed(self.layers):
            if key in layer.data:
                return layer.data[key]
        return default

    def merged(self) -> dict[str, Any]:
        """Return the fully merged flat dict (later layer wins).

        Returns:
            A dict with every key from every layer; on a conflict the
            highest-precedence layer wins.

        """
        out: dict[str, Any] = {}
        for layer in self.layers:
            out.update(layer.data)
        return out

    def is_stale(self, path: Path) -> bool:
        """Return whether a source file changed since it was loaded.

        Args:
            path: The source file to check.

        Returns:
            True when the file's current fingerprint differs from the one
            recorded at load time (or the layer was never loaded from it).

        """
        for layer in self.layers:
            if layer.source_path == path:
                return layer.source_fingerprint != _source_fingerprint(path)
        return True


def merge_layers(layers: list[ContextLayerData]) -> dict[str, Any]:
    """Merge a list of layer data into a single flat dict.

    Args:
        layers: Layer data in any order; precedence is by :class:`ContextLayer`.

    Returns:
        The merged dict, highest-precedence layer winning on conflicts.

    """
    ordered = sorted(layers, key=lambda x: x.layer)
    out: dict[str, Any] = {}
    for layer in ordered:
        out.update(layer.data)
    return out


def emit_layers(ctx: LayerContext, needed: set[str] | None = None) -> dict[str, Any]:
    """Emit only the layers the current scope needs.

    Args:
        ctx: The merged layer context.
        needed: Optional set of keys to emit. When ``None``, emits all.

    Returns:
        A dict containing only the needed keys from the merged view.

    """
    merged = ctx.merged()
    if needed is None:
        return merged
    return {k: v for k, v in merged.items() if k in needed}
