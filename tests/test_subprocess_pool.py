import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beagle.config.config import get_config
from beagle.utils.subprocess_pool import (
    CircuitBreakerOpenError,
    GoosePool,
    _execute_goose_with_fallback,
    _execute_single_model,
    get_pool_stats,
    reset_pool_stats,
    truncate_large_output,
)


@pytest.fixture(autouse=True)
def reset_stats():
    reset_pool_stats()


@pytest.fixture(autouse=True)
def reset_circuit_breakers():
    """Reset global circuit breaker state to prevent test pollution."""
    from beagle.utils import circuit_breaker as cb_mod

    old_circuits = cb_mod._circuits.copy()
    cb_mod._circuits.clear()
    yield
    cb_mod._circuits.clear()
    cb_mod._circuits.update(old_circuits)


@pytest.mark.asyncio
async def test_truncate_large_output():
    short_output = "short"
    assert truncate_large_output(short_output) == short_output

    # Create large output
    large_output = "line\n" * (get_config().output.truncation_threshold // 2)
    truncated = truncate_large_output(large_output)
    assert "[TRUNCATED:" in truncated
    assert "Use VFS archive" in truncated


@pytest.mark.asyncio
@patch("beagle.utils.subprocess_pool._execute_single_model")
async def test_execute_goose_with_fallback_success(mock_execute):
    mock_execute.return_value = ("<final_answer>success</final_answer>", "raw_stdout")

    ans, _raw = await _execute_goose_with_fallback("prompt", "sys", "node")
    assert ans == "<final_answer>success</final_answer>"
    assert mock_execute.call_count == 1


@pytest.mark.asyncio
@patch("beagle.utils.subprocess_pool._execute_single_model")
async def test_execute_goose_with_fallback_chain(mock_execute):
    # Fail first, succeed second
    mock_execute.side_effect = [
        RuntimeError("model 1 failed"),
        ("<final_answer>fallback success</final_answer>", "raw_stdout"),
    ]

    with patch(
        "beagle.utils.subprocess_pool._get_fallback_chain",
        return_value=["model1", "model2"],
    ):
        ans, _raw = await _execute_goose_with_fallback("prompt", "sys", "node")
        assert ans == "<final_answer>fallback success</final_answer>"
        assert mock_execute.call_count == 2
        stats = get_pool_stats()
        assert stats["fallback_used"] == 1


@pytest.mark.asyncio
@patch("beagle.utils.subprocess_pool._execute_single_model")
async def test_execute_goose_with_fallback_all_fail(mock_execute):
    mock_execute.side_effect = RuntimeError("all failed")

    with patch(
        "beagle.utils.subprocess_pool._get_fallback_chain",
        return_value=["model1", "model2"],
    ):
        with pytest.raises(RuntimeError, match="all failed"):
            await _execute_goose_with_fallback("prompt", "sys", "node")
        assert mock_execute.call_count == 2


@pytest.mark.asyncio
@patch("beagle.utils.subprocess_pool._execute_goose_with_fallback")
async def test_goose_pool_run(mock_fallback):
    mock_fallback.return_value = ("<final_answer>pool</final_answer>", "raw")
    pool = GoosePool(max_workers=2)
    ans, _raw = await pool.run("p", "s", "n")
    assert ans == "<final_answer>pool</final_answer>"
    stats = pool.stats()
    assert stats["max_workers"] == 2


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_execute_single_model_success(mock_create_subprocess):
    process_mock = AsyncMock()
    # When streaming is enabled (the default in test config), _execute_single_model
    # calls _streaming_read which reads stdout via process.stdout.readline().
    # We need the mock to yield lines (bytes) from readline, NOT a coroutine
    # object. MagicMock returns an AsyncMock by default for any attribute
    # access, so we must set stdout/stderr explicitly.
    process_mock.stdin = MagicMock()
    process_mock.stdin.write = MagicMock()
    process_mock.stdin.close = MagicMock()
    # drain() and wait_closed() are awaited on the stdin transport
    # during the streaming prompt-write step.
    process_mock.stdin.drain = AsyncMock()
    process_mock.stdin.wait_closed = AsyncMock()
    process_mock.stdout = MagicMock()
    process_mock.stdout.readline = AsyncMock(
        side_effect=[
            b"<final_answer>",
            b"test",
            b"</final_answer>",
            b"",
        ]
    )
    process_mock.stderr = MagicMock()
    process_mock.stderr.readline = AsyncMock(return_value=b"")
    process_mock.wait = AsyncMock(return_value=0)
    process_mock.returncode = 0
    # process.communicate is only called when streaming is disabled;
    # we still set it as a safety net.
    process_mock.communicate.return_value = (
        b"<final_answer>test</final_answer>",
        b"",
    )
    mock_create_subprocess.return_value = process_mock

    circuit_mock = AsyncMock()
    ans, _raw = await _execute_single_model(
        "goose",
        "model",
        "provider",
        "prompt",
        "sys",
        "node",
        10,
        False,
        circuit_mock,
    )
    assert ans == "test"


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_execute_single_model_timeout(mock_create_subprocess):
    # Streaming is enabled in the test config; _execute_single_model
    # awaits _streaming_read, which awaits process.stdout.readline().
    # Setting stdout.readline to hang forever reproduces the timeout
    # path the production code handles with SIGTERM.
    process_mock = AsyncMock()
    process_mock.stdin = MagicMock()
    process_mock.stdin.write = MagicMock()
    process_mock.stdin.close = MagicMock()
    process_mock.stdin.drain = AsyncMock()
    process_mock.stdin.wait_closed = AsyncMock()

    async def _hang_forever():
        await asyncio.Event().wait()  # never resolves; only cancelled

    process_mock.stdout = MagicMock()
    process_mock.stdout.readline = AsyncMock(side_effect=_hang_forever)
    process_mock.stderr = MagicMock()
    process_mock.stderr.readline = AsyncMock(side_effect=_hang_forever)
    process_mock.wait = AsyncMock(return_value=0)
    process_mock.returncode = 0
    # process.communicate is only called when streaming is disabled;
    # set it for completeness.
    process_mock.communicate.side_effect = TimeoutError()
    mock_create_subprocess.return_value = process_mock

    circuit_mock = AsyncMock()
    with pytest.raises(RuntimeError, match="Timeout after"):
        await _execute_single_model(
            "goose",
            "model",
            "provider",
            "prompt",
            "sys",
            "node",
            1,
            False,
            circuit_mock,
        )


@pytest.mark.asyncio
@patch("beagle.utils.subprocess_pool.get_circuit_breaker")
async def test_circuit_breaker_open(mock_get_cb):
    # Regression test: the orchestrator must consult `_can_attempt` (which
    # performs the OPEN→HALF_OPEN state transition) rather than reading the
    # raw `is_open` flag. A bare `is_open` read leaves the breaker stuck
    # OPEN forever once it has tripped, because no caller is triggering the
    # cooldown-based transition. See `test_circuit_breaker_recovers_after_cooldown`
    # for the recovery half of that contract.
    cb_mock = MagicMock()
    cb_mock.is_open = True
    cb_mock.get_retry_after.return_value = 10.0
    # `_can_attempt` is an async method — when it returns False, the
    # orchestrator should raise CircuitBreakerOpenError.
    cb_mock._can_attempt = AsyncMock(return_value=False)
    mock_get_cb.return_value = cb_mock

    with pytest.raises(CircuitBreakerOpenError):
        await _execute_goose_with_fallback("p", "s", "n")


@pytest.mark.asyncio
@patch("beagle.utils.subprocess_pool.get_circuit_breaker")
@patch("beagle.utils.subprocess_pool._execute_single_model")
async def test_circuit_breaker_recovers_after_cooldown(mock_execute, mock_get_cb):
    """Regression test for the stuck-OPEN bug.

    When the breaker is in the OPEN state but `_can_attempt` returns True
    (i.e. the cooldown has elapsed and a HALF_OPEN probe is allowed), the
    orchestrator must NOT raise CircuitBreakerOpenError — it must proceed
    to call `_execute_single_model`. Previously, a bare `circuit.is_open`
    read would short-circuit this path and the breaker would be stuck
    OPEN forever.
    """
    cb_mock = MagicMock()
    cb_mock.is_open = True  # raw state is still OPEN
    cb_mock.get_retry_after.return_value = 0.0  # cooldown elapsed
    # `_can_attempt` performs the OPEN→HALF_OPEN transition and returns
    # True when a probe is allowed.
    cb_mock._can_attempt = AsyncMock(return_value=True)
    cb_mock._record_success = AsyncMock()
    mock_get_cb.return_value = cb_mock
    mock_execute.return_value = ("<final_answer>ok</final_answer>", "raw")

    result = await _execute_goose_with_fallback("p", "s", "n")
    assert result == ("<final_answer>ok</final_answer>", "raw")
    cb_mock._can_attempt.assert_awaited_once()
    cb_mock._record_success.assert_awaited_once()
