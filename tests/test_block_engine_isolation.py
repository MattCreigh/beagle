"""Regression tests for the block-engine salvage (2026-07-29).

Covers the five behaviours salvaged from the retired
``beagle.blocks`` package into the live
``beagle.blocks`` engine:

S-1  Per-block exception isolation: a crashing/timing-out block yields a
     FAILURE BlockResult instead of unwinding the tier; sibling results
     survive; the caller gets a partial ExecutionResult, not a traceback.
S-2  Block identity: every BlockResult carries ``block_name``.
S-3  ``errors`` is a ``list[str]`` end-to-end.
S-4  ``BlockStatus`` enum: SKIPPED is non-fatal; ``VariableBinding.required``
     is honoured (required+unresolved raises, optional+unresolved is skipped).
S-5  Output contract: return-annotation violations fail the block cleanly;
     unannotated returns pass through unchecked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from beagle.blocks.cache import AgentCache
from beagle.blocks.context import (
    BlockResult,
    BlockStatus,
    ExecutionContext,
)
from beagle.blocks.engine import BlockComposer, ComposerConfig
from beagle.blocks.registry import BlockRegistry
from beagle.blocks.schema import (
    AgentDefinition,
    BlockRef,
    VariableBinding,
)


def _composer(tmp_path, timeout_seconds=5.0, **cfg_kwargs) -> BlockComposer:
    """Composer with single-attempt retries and an isolated cache dir."""
    cfg = ComposerConfig(max_retries=1, timeout_seconds=timeout_seconds, **cfg_kwargs)
    composer = BlockComposer(config=cfg)
    composer.cache = AgentCache(cache_dir=tmp_path / "block_cache")
    return composer


def _register(name: str, func) -> None:
    BlockRegistry.instance().register_python(name, func)


# ── S-1: exception isolation ────────────────────────────────────────────


async def test_s1_crashing_block_returns_failure_result_not_traceback(tmp_path):
    def boom(params=None):
        raise ValueError("schema-adjacent explosion")

    _register("s1_boom", boom)
    composer = _composer(tmp_path)
    recipe = AgentDefinition(name="s1_recipe", blocks=["s1_boom"])

    # Before S-1 this raised ValueError out of compose_async and destroyed
    # the tier; now it must come back as a partial ExecutionResult.
    result = await composer.compose_async(recipe, inputs={})

    assert result.success is False
    assert len(result.block_results) == 1
    br = result.block_results[0]
    assert br.status == BlockStatus.FAILURE
    assert br.block_name == "s1_boom"  # S-2
    assert any("ValueError" in e and "schema-adjacent explosion" in e for e in br.errors)  # S-3
    assert result.errors == list(br.errors)


async def test_s1_sibling_results_survive_tier_crash(tmp_path):
    def ok(params=None):
        return "fine"

    def kaput(params=None):
        raise RuntimeError("kaput")

    def _kaput_raw(s1_ok=None, params=None):
        """Tolerant raw signature, mimicking a wrapped @python_block.

        Tier siblings share ctx, so the surviving sibling's output is
        merged into the crashing block's params (engine design). Without a
        raw signature admitting that key, input validation rejects the
        params before the body runs and the test would not exercise a
        body-crash.
        """

    kaput.__raw_func__ = _kaput_raw

    _register("s1_ok", ok)
    _register("s1_kaput", kaput)
    composer = _composer(tmp_path)
    recipe = AgentDefinition(name="s1_tier_recipe", blocks=["s1_ok", "s1_kaput"])
    tier = [BlockRef(name="s1_ok"), BlockRef(name="s1_kaput")]

    # Before S-1, gather() re-raised the RuntimeError and the completed
    # sibling result was lost.
    results = await composer._execute_tier(tier, ExecutionContext(inputs={}), recipe)

    assert len(results) == 2
    assert results[0].status == BlockStatus.SUCCESS
    assert results[0].output == "fine"
    assert results[1].status == BlockStatus.FAILURE
    assert results[1].block_name == "s1_kaput"
    assert any("RuntimeError" in e and "kaput" in e for e in results[1].errors)


async def test_s1_timeout_returns_failure_result(tmp_path):
    async def slow(params=None):
        await asyncio.sleep(5)

    _register("s1_slow", slow)
    composer = _composer(tmp_path, timeout_seconds=0.2)
    recipe = AgentDefinition(name="s1_slow_recipe", blocks=["s1_slow"])

    # Before S-1 this raised BlockTimeoutError to the caller.
    result = await composer.compose_async(recipe, inputs={})

    assert result.success is False
    br = result.block_results[0]
    assert br.status == BlockStatus.FAILURE
    assert br.block_name == "s1_slow"
    assert any("timed out" in e for e in br.errors)


# ── S-2: block identity ─────────────────────────────────────────────────


async def test_s2_unregistered_block_result_carries_name(tmp_path):
    composer = _composer(tmp_path)
    recipe = AgentDefinition(name="s2_recipe", blocks=["s2_ghost"])

    result = await composer.compose_async(recipe, inputs={})

    assert result.success is False
    br = result.block_results[0]
    assert br.block_name == "s2_ghost"
    assert any("not registered" in e for e in br.errors)


# ── S-4: BlockStatus + required semantics ────────────────────────────────


async def test_s4_skipped_result_is_not_fatal(tmp_path):
    composer = _composer(tmp_path)
    composer._execute_tier = AsyncMock(
        return_value=[BlockResult(status=BlockStatus.SKIPPED, block_name="cond_block")]
    )
    recipe = AgentDefinition(name="s4_skip_recipe", blocks=["cond_block"])

    result = await composer.compose_async(recipe, inputs={})

    assert result.success is True
    assert result.block_results[0].status == BlockStatus.SKIPPED


def test_s4_required_env_var_unresolved_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("S4_DEFINITELY_UNSET", raising=False)
    composer = _composer(tmp_path)
    recipe = AgentDefinition(
        name="s4_req_recipe",
        blocks=[],
        variables=[VariableBinding(name="S4_DEFINITELY_UNSET", source="env", required=True)],
    )

    with pytest.raises(ValueError, match="Required variable 'S4_DEFINITELY_UNSET'"):
        composer._resolve_manifest(recipe)


def test_s4_optional_env_var_unresolved_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("S4_ALSO_UNSET", raising=False)
    composer = _composer(tmp_path)
    recipe = AgentDefinition(
        name="s4_opt_recipe",
        blocks=[],
        variables=[VariableBinding(name="S4_ALSO_UNSET", source="env", required=False)],
    )

    manifest = composer._resolve_manifest(recipe)

    assert "S4_ALSO_UNSET" not in manifest.inputs


def test_s4_env_var_present_is_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("S4_PRESENT", "yes")
    composer = _composer(tmp_path)
    recipe = AgentDefinition(
        name="s4_present_recipe",
        blocks=[],
        variables=[VariableBinding(name="S4_PRESENT", source="env", required=True)],
    )

    manifest = composer._resolve_manifest(recipe)

    assert manifest.inputs["S4_PRESENT"] == "yes"


# ── S-5: output contract ────────────────────────────────────────────────


async def test_s5_wrong_return_type_fails_block_cleanly(tmp_path):
    def lies(params=None) -> str:
        return 42  # type: ignore[return-value]

    _register("s5_lies", lies)
    composer = _composer(tmp_path)
    recipe = AgentDefinition(name="s5_lies_recipe", blocks=["s5_lies"])

    result = await composer.compose_async(recipe, inputs={})

    assert result.success is False
    br = result.block_results[0]
    assert br.status == BlockStatus.FAILURE
    assert br.block_name == "s5_lies"
    assert any("output validation failed" in e for e in br.errors)


async def test_s5_correct_return_type_passes(tmp_path):
    def honest(params=None) -> str:
        return "typed"

    _register("s5_honest", honest)
    composer = _composer(tmp_path)
    recipe = AgentDefinition(name="s5_honest_recipe", blocks=["s5_honest"])

    result = await composer.compose_async(recipe, inputs={})

    assert result.success is True
    assert result.block_results[0].output == "typed"


async def test_s5_unannotated_return_passes_unchecked(tmp_path):
    def loose(params=None):
        return (1, 2)  # JSON-serializable, but no declared contract

    _register("s5_loose", loose)
    composer = _composer(tmp_path)
    recipe = AgentDefinition(name="s5_loose_recipe", blocks=["s5_loose"])

    result = await composer.compose_async(recipe, inputs={})

    assert result.success is True
