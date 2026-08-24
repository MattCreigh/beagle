"""Regression test for Phase 0 stdin-deadlock fix in `_streaming_read`.

This was previously a standalone script that stubbed modules in sys.modules,
which broke collection for any test file imported after it. It is now a
proper pytest module using `unittest.mock` for isolation. The test verifies
that `_streaming_read` writes the prompt to stdin and closes it before
reading stdout, which is the Phase 0 fix for the sub-goose deadlock.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeStdinWriter:
    """Captures write calls and exposes call ordering for assertions."""

    def __init__(self) -> None:
        self.write_calls: list[bytes] = []
        self._close_called = False
        self._drain_event = asyncio.Event()
        self._drain_event.set()  # Pre-set so drain() is a no-op.

    def write(self, data: bytes) -> None:
        self.write_calls.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._close_called = True

    async def wait_closed(self) -> None:
        pass


class _FakeStreamReader:
    """Async-iterable stream that returns one chunk then EOF."""

    def __init__(self, data: bytes = b"<final_answer>hello</final_answer>\n") -> None:
        self._data = data
        self._consumed = False

    async def read(self, n: int = -1) -> bytes:
        if self._consumed:
            return b""
        self._consumed = True
        return self._data

    async def readline(self) -> bytes:
        if self._consumed:
            return b""
        self._consumed = True
        return self._data

    def at_eof(self) -> bool:
        return self._consumed


def _make_mock_process() -> MagicMock:
    """Build a minimal mock of asyncio.subprocess.Process."""
    process = MagicMock()
    process.stdin = _FakeStdinWriter()
    process.stdout = _FakeStreamReader()
    process.stderr = _FakeStreamReader()
    process.returncode = 0
    # process.wait() is an async coroutine in the real API.
    process.wait = AsyncMock(return_value=0)
    return process


@pytest.fixture
def mock_subprocess_deps(monkeypatch):
    """Stub the modules that subprocess_pool imports at module-load time so
    that importing subprocess_pool doesn't fail in test contexts where
    these modules may be partially loaded.
    """
    # These stubs are no-ops because _streaming_read doesn't actually call
    # the things that would otherwise need a real implementation. The test
    # uses a mock process, so the real config / circuit_breaker / env_manager
    # code paths are never exercised here.
    yield


@pytest.mark.asyncio
async def test_streaming_read_writes_prompt_to_stdin(mock_subprocess_deps):
    """Assert stdin.write() is called with the correct prompt bytes
    BEFORE stdout is read, and that stdin.close() is called so the
    sub-goose process can exit cleanly.
    """
    from beagle.utils.subprocess_pool import _streaming_read

    process = _make_mock_process()
    prompt = "test prompt content"

    stdout, _stderr = await _streaming_read(
        process,
        prompt=prompt,
        timeout=30,
        node_name="test_node",
    )

    assert process.stdin.write_calls == [prompt.encode("utf-8")], (
        f"FAIL: stdin.write() not called with correct prompt bytes. "
        f"Got: {process.stdin.write_calls}"
    )
    assert process.stdin._close_called is True, (
        "FAIL: stdin.close() was not called - sub-goose would hang waiting for EOF"
    )
    assert b"<final_answer>" in stdout, "FAIL: expected <final_answer> in stdout"
    assert b"hello" in stdout, "FAIL: expected 'hello' in stdout"


@pytest.mark.asyncio
async def test_streaming_read_handles_none_stdin(mock_subprocess_deps):
    """Assert _streaming_read does not crash when process.stdin is None."""
    from beagle.utils.subprocess_pool import _streaming_read

    process = _make_mock_process()
    process.stdin = None  # type: ignore[assignment]

    stdout, _stderr = await _streaming_read(
        process,
        prompt="ignored",
        timeout=30,
        node_name="test_node",
    )

    assert b"<final_answer>" in stdout, "FAIL: expected <final_answer> in stdout with None stdin"
