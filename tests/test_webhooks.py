"""Regression tests for beagle.webhooks — D-11 (release-readiness audit 2026-08-28).

The webhook retry loop previously caught only ``ConnectionError``, but
``aiohttp.ClientConnectorError`` (DNS / connect failure) is NOT a subclass of
``ConnectionError`` (verified via MRO), so connection failures escaped the
retry schedule entirely — no backoff, no delivery record, and the exception
propagated out of ``deliver``. This test asserts every aiohttp transport
failure is consumed by the retry loop and returns a ``success=False`` record.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from beagle.webhooks import WebhookConfig, WebhookManager


class _RaisingCM:
    """Async context manager that raises ``exc`` on __aenter__.

    The code under test does ``async with session.post(...) as response``, so
    a patched ``session.post`` must return an async context manager (not raise
    directly). __aenter__ raises the injected failure, which is what the retry
    loop must catch.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


def _patch_session_post(manager: WebhookManager, exc: BaseException) -> Any:
    """Patch aiohttp.ClientSession.post to return a CM raising ``exc``."""
    real_post = aiohttp.ClientSession.post

    def failing_post(self: Any, _url: str, **_kwargs: Any) -> _RaisingCM:
        return _RaisingCM(exc)

    aiohttp.ClientSession.post = failing_post  # type: ignore[method-assign,assignment]
    return real_post


@pytest.mark.asyncio
async def test_client_connector_error_is_consumed_not_raised() -> None:
    """aiohttp.ClientConnectorError (not a ConnectionError) must hit the retry
    schedule and return a failed delivery, not escape the loop."""
    manager = WebhookManager()
    config = WebhookConfig(
        url="http://127.0.0.1:1/nowhere",  # nothing listening
        max_retries=3,
        retry_delay=0.001,  # near-zero backoff so the test is fast
    )
    manager.register("wh", config)

    real_post = _patch_session_post(
        manager,
        aiohttp.ClientConnectorError(
            aiohttp.client_reqrep.ConnectionKey(
                "127.0.0.1", 1, False, None, None, None, None  # type: ignore[arg-type]
            ),
            ConnectionRefusedError(),
        ),
    )
    try:
        deliveries = await manager.deliver("event.x", {"k": "v"})
    finally:
        aiohttp.ClientSession.post = real_post  # type: ignore[method-assign]

    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery.success is False
    assert delivery.attempt == 3, "all retries must be consumed"
    assert "client error" in delivery.error


@pytest.mark.asyncio
async def test_oserror_is_consumed_not_raised() -> None:
    """A bare OSError (local socket/FD failure) must also hit the retry loop."""
    manager = WebhookManager()
    config = WebhookConfig(
        url="http://example.invalid/x",
        max_retries=2,
        retry_delay=0.001,
    )
    manager.register("wh", config)

    real_post = _patch_session_post(manager, OSError(24, "Too many open files"))
    try:
        deliveries = await manager.deliver("event.x", {"k": "v"})
    finally:
        aiohttp.ClientSession.post = real_post  # type: ignore[method-assign]

    assert deliveries[0].success is False
    assert deliveries[0].attempt == 2
    assert "OS error" in deliveries[0].error


@pytest.mark.asyncio
async def test_timeout_is_consumed() -> None:
    """TimeoutError still routes through the retry loop (regression guard)."""
    manager = WebhookManager()
    config = WebhookConfig(url="http://x/", max_retries=2, retry_delay=0.001)
    manager.register("wh", config)

    real_post = _patch_session_post(manager, TimeoutError("took too long"))
    try:
        deliveries = await manager.deliver("event.x", {"k": "v"})
    finally:
        aiohttp.ClientSession.post = real_post  # type: ignore[method-assign]

    assert deliveries[0].success is False
    assert deliveries[0].attempt == 2
    assert deliveries[0].error == "Timeout"
