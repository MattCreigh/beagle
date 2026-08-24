"""SP-5: tests for blocks/context (was zero-coverage).

beagle-spotless-phase2, work package SP-5. The block execution context and
result dataclasses had no direct tests. These exercise BlockStatus, the
ExecutionContext get/set contract, and BlockResult success/error shims.
"""

from __future__ import annotations

from beagle.blocks.context import (
    BlockResult,
    BlockStatus,
    ExecutionContext,
    ExecutionResult,
)


def test_block_status_values() -> None:
    """BlockStatus covers the full lifecycle."""
    assert BlockStatus.PENDING.value == "pending"
    assert BlockStatus.RUNNING.value == "running"
    assert BlockStatus.SUCCESS.value == "success"
    assert BlockStatus.FAILURE.value == "failure"
    assert BlockStatus.SKIPPED.value == "skipped"


def test_execution_context_defaults() -> None:
    """ExecutionContext has empty containers and depth 0 by default."""
    ctx = ExecutionContext()
    assert ctx.inputs == {}
    assert ctx.outputs == {}
    assert ctx.depth == 0


def test_execution_context_set_get() -> None:
    """set writes to outputs; get reads outputs then inputs."""
    ctx = ExecutionContext(inputs={"role": "user"})
    assert ctx.get("role") == "user"  # from inputs
    ctx.set("result", "done")
    assert ctx.get("result") == "done"  # from outputs
    assert ctx.outputs["result"] == "done"


def test_execution_context_get_default() -> None:
    """get returns the default when the key is absent."""
    ctx = ExecutionContext()
    assert ctx.get("missing", "fallback") == "fallback"
    assert ctx.get("missing") is None


def test_block_result_success_shim() -> None:
    """success is True only for SUCCESS status."""
    assert BlockResult(status=BlockStatus.SUCCESS).success is True
    assert BlockResult(status=BlockStatus.FAILURE).success is False
    assert BlockResult(status=BlockStatus.SKIPPED).success is False


def test_block_result_error_shim() -> None:
    """error returns the first message or empty string."""
    r = BlockResult(status=BlockStatus.FAILURE, errors=["bad", "worse"])
    assert r.error == "bad"
    assert BlockResult(status=BlockStatus.SUCCESS).error == ""


def test_execution_result_fields() -> None:
    """ExecutionResult carries overall success and block results."""
    er = ExecutionResult(
        success=True,
        final_output="ok",
        block_results=[BlockResult(status=BlockStatus.SUCCESS)],
        total_cost_usd=0.5,
    )
    assert er.success is True
    assert er.final_output == "ok"
    assert len(er.block_results) == 1
    assert er.total_cost_usd == 0.5
    assert er.errors == []
