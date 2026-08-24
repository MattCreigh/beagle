"""SP-5: tests for blocks/python_blocks/base (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The @python_block decorator and
BlockMetadata had no direct tests. These exercise the wrapper contract (result
envelope, error wrapping, metadata attachment).
"""

from __future__ import annotations

import pytest

from beagle.blocks.errors import ExecutionError
from beagle.blocks.python_blocks.base import BlockMetadata, python_block


def test_block_metadata_defaults() -> None:
    """BlockMetadata has safe defaults."""
    meta = BlockMetadata(name="x", description="desc")
    assert meta.inputs is None
    assert meta.outputs is None
    assert meta.cost_weight == 1.0
    assert meta.timeout_seconds == 30.0
    assert meta.retry_count == 0


def test_python_block_wraps_success() -> None:
    """A successful block returns the success envelope with the output."""

    @python_block(name="add", description="adds")
    def add(_ctx, a: int, b: int) -> int:
        return a + b

    result = add(None, a=2, b=3)
    assert result["success"] is True
    assert result["output"] == 5
    assert "duration" in result


def test_python_block_wraps_failure() -> None:
    """A failing block raises ExecutionError with block_name."""

    @python_block(name="boom", description="raises")
    def boom(_ctx) -> None:
        raise ValueError("kaboom")

    with pytest.raises(ExecutionError) as excinfo:
        boom(None)
    assert excinfo.value.block_name == "boom"
    assert "kaboom" in str(excinfo.value)


def test_python_block_uses_function_name() -> None:
    """name defaults to the function name when not provided."""

    @python_block(description="desc")
    def my_custom_block(_ctx) -> str:
        return "ok"

    assert my_custom_block.__block_name__ == "my_custom_block"  # type: ignore[attr-defined]
    assert my_custom_block(None)["output"] == "ok"


def test_python_block_uses_docstring_as_description() -> None:
    """description defaults to the function docstring."""

    @python_block(name="doc")
    def doc(_ctx) -> str:
        """The doc description."""
        return "x"

    assert doc.__block_meta__.description == "The doc description."  # type: ignore[attr-defined]


def test_python_block_attaches_raw_func() -> None:
    """The original function is preserved on __raw_func__."""

    @python_block(name="raw")
    def raw(_ctx) -> str:
        return "x"

    assert raw.__raw_func__ is raw.__wrapped__ or callable(raw.__raw_func__)  # type: ignore[attr-defined]


def test_python_block_meta_timeout() -> None:
    """Decorator kwargs propagate to BlockMetadata."""

    @python_block(name="slow", timeout_seconds=5.0, retry_count=2, cost_weight=0.5)
    def slow(_ctx) -> str:
        return "x"

    assert slow.__block_meta__.timeout_seconds == 5.0  # type: ignore[attr-defined]
    assert slow.__block_meta__.retry_count == 2
    assert slow.__block_meta__.cost_weight == 0.5
