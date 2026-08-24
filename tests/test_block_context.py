"""Tests for ExecutionContext and BlockResult."""

from __future__ import annotations

from beagle.blocks.context import (
    BlockResult,
    BlockStatus,
    ExecutionContext,
    ExecutionResult,
)


def test_execution_context_get_set():
    ctx = ExecutionContext(inputs={"a": 1})
    ctx.set("b", 2)
    assert ctx.get("a") == 1
    assert ctx.get("b") == 2
    assert ctx.get("c", "default") == "default"


def test_block_result():
    br = BlockResult(status=BlockStatus.SUCCESS, output="hello")
    assert br.success is True
    assert br.output == "hello"
    assert br.errors == []
    assert br.error == ""


def test_block_result_failure_carries_identity_and_errors():
    br = BlockResult(
        status=BlockStatus.FAILURE,
        block_name="parser",
        errors=["boom", "bad input"],
    )
    assert br.success is False
    assert br.block_name == "parser"
    assert br.errors == ["boom", "bad input"]
    assert br.error == "boom"  # back-compat shim returns first error


def test_block_result_skipped_is_not_success_and_not_failure():
    br = BlockResult(status=BlockStatus.SKIPPED, block_name="cond")
    assert br.success is False
    assert br.status != BlockStatus.FAILURE


def test_execution_result():
    er = ExecutionResult(
        success=True,
        block_results=[BlockResult(status=BlockStatus.SUCCESS)],
    )
    assert er.success is True
    assert len(er.block_results) == 1
