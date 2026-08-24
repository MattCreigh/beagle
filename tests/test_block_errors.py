"""SP-5: tests for blocks/errors exception hierarchy (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The block-composition error
hierarchy had no direct tests. These exercise construction, attributes, and
the subclass relationships.
"""

from __future__ import annotations

import pytest

from beagle.blocks.errors import (
    BlockError,
    BlockNotFoundError,
    BlockTimeoutError,
    BudgetExceededError,
    ExecutionError,
    SchemaError,
)


def test_block_error_message() -> None:
    """BlockError passes the message to Exception."""
    err = BlockError("boom")
    assert str(err) == "boom"
    assert isinstance(err, Exception)


def test_block_error_attributes() -> None:
    """BlockError carries block_name and details."""
    err = BlockError("boom", block_name="render", details={"x": 1})
    assert err.block_name == "render"
    assert err.details == {"x": 1}


def test_block_error_default_attributes() -> None:
    """block_name and details have safe defaults."""
    err = BlockError("boom")
    assert err.block_name == ""
    assert err.details == {}


def test_block_error_details_never_none() -> None:
    """details defaults to {} rather than None (callers index into it)."""
    err = BlockError("boom", details=None)
    assert err.details == {}


@pytest.mark.parametrize(
    "cls",
    [
        SchemaError,
        BlockNotFoundError,
        ExecutionError,
        BlockTimeoutError,
        BudgetExceededError,
    ],
)
def test_all_errors_subclass_block_error(cls: type[BlockError]) -> None:
    """Every block error is a BlockError (catchable by one type)."""
    assert issubclass(cls, BlockError)


def test_all_errors_catchable_as_block_error() -> None:
    """A single except BlockError catches every specific error."""
    cases = [
        SchemaError("schema"),
        BlockNotFoundError("missing"),
        ExecutionError("exec"),
        BlockTimeoutError("timeout"),
        BudgetExceededError("budget"),
    ]
    for err in cases:
        with pytest.raises(BlockError):
            raise err
