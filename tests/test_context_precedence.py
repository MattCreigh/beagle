"""Tests for the layered context merge with explicit precedence (C2).

Verifies the precedence rule (global → directory → task, later wins), the
flat merge, the staleness re-render trigger, and selective layer emission.
"""

from __future__ import annotations

from pathlib import Path

from beagle.context.layered import (
    ContextLayer,
    ContextLayerData,
    LayerContext,
    emit_layers,
    merge_layers,
)


def test_precedence_task_wins_over_directory_and_global() -> None:
    """On a conflict the task layer wins, then directory, then global."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"mode": "global", "only_global": "g"})
    ctx.add(ContextLayer.DIRECTORY, {"mode": "directory", "only_dir": "d"})
    ctx.add(ContextLayer.TASK, {"mode": "task"})

    assert ctx.get("mode") == "task"  # task overrides directory/global
    assert ctx.get("only_global") == "g"  # only in global
    assert ctx.get("only_dir") == "d"  # only in directory
    assert ctx.get("missing") is None  # default None


def test_directory_wins_over_global() -> None:
    """Without a task layer, directory overrides global."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"mode": "global"})
    ctx.add(ContextLayer.DIRECTORY, {"mode": "directory"})
    assert ctx.get("mode") == "directory"


def test_global_alone() -> None:
    """With only the global layer, its value is returned."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"mode": "global"})
    assert ctx.get("mode") == "global"


def test_get_default_when_absent() -> None:
    """get returns the caller default when no layer has the key."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"a": 1})
    assert ctx.get("b", "fallback") == "fallback"


def test_merged_flat_dict() -> None:
    """merged() returns a flat dict with later-layer-wins semantics."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"a": "global", "b": "global"})
    ctx.add(ContextLayer.TASK, {"b": "task"})
    merged = ctx.merged()
    assert merged == {"a": "global", "b": "task"}


def test_merge_layers_free_function() -> None:
    """merge_layers() sorts by precedence before merging."""
    merged = merge_layers(
        [
            ContextLayerData(ContextLayer.TASK, {"x": "task"}),
            ContextLayerData(ContextLayer.GLOBAL, {"x": "global", "y": "g"}),
        ]
    )
    assert merged == {"x": "task", "y": "g"}


def test_replacing_a_layer_keeps_precedence() -> None:
    """Re-adding a layer at the same tier replaces its content."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"mode": "global"})
    ctx.add(ContextLayer.GLOBAL, {"mode": "global2"})
    assert ctx.get("mode") == "global2"
    assert len(ctx.layers) == 1


def test_staleness_trigger_re_render(tmp_path: Path) -> None:
    """A changed TOML source forces a re-render (is_stale → True)."""
    src = tmp_path / "directive.toml"
    src.write_text('mode = "global"', encoding="utf-8")

    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"mode": "global"}, source_path=src)
    assert ctx.is_stale(src) is False

    src.write_text('mode = "changed"', encoding="utf-8")
    assert ctx.is_stale(src) is True


def test_emit_layers_all() -> None:
    """emit_layers with needed=None emits the full merged view."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"a": 1, "b": 2})
    ctx.add(ContextLayer.TASK, {"c": 3})
    assert emit_layers(ctx) == {"a": 1, "b": 2, "c": 3}


def test_emit_layers_subset() -> None:
    """emit_layers with a needed set emits only those keys."""
    ctx = LayerContext()
    ctx.add(ContextLayer.GLOBAL, {"a": 1, "b": 2})
    ctx.add(ContextLayer.TASK, {"c": 3})
    assert emit_layers(ctx, needed={"a", "c"}) == {"a": 1, "c": 3}
