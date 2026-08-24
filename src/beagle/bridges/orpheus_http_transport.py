"""Orpheus HTTP SSE Transport — Cloud-compatible event bus transport.

Phase 6 of the LangChain Ecosystem Compatibility Plan.
Replaces Unix domain sockets for the Orpheus event bus when
running in LangGraph Cloud (BEAGLE_EXECUTION_ENV=cloud).

Uses Server-Sent Events (SSE) for push notifications and
HTTP POST for publishing events.

In local mode, the existing Unix socket transport is used (no change).
This module ONLY activates when execution_env="cloud".
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from ..events import BeagleEvent, get_event_bus
from .config import get_cloud_config

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger("Beagle.bridges.orpheus_http_transport")


class OrpheusHTTPTransport:
    """HTTP-based Orpheus event transport for cloud environments.

    Replaces Unix domain sockets when BEAGLE_EXECUTION_ENV=cloud.
    Uses Server-Sent Events (SSE) for push notifications.

    Architecture:
      Cloud workflow → HTTP POST → /orpheus/publish → Orpheus bus
      Cloud dashboard ← SSE ← /orpheus/events ← Orpheus bus

    Usage:
        transport = OrpheusHTTPTransport()
        await transport.start()

        # Publishing: POST to /orpheus/publish
        # Subscribing: GET /orpheus/events (SSE stream)
    """

    def __init__(self) -> None:
        self.config = get_cloud_config()
        self._app = None
        self._server = None
        self._subscribers: list[asyncio.Queue] = []
        self._bus = get_event_bus()
        # Reusable HTTP client (Phase 6 edge-inference optimisation).
        # The orpheus bus is the hot path; allocating a new client per
        # publish costs ~20-50 ms of TCP+TLS overhead that compounds
        # when the ensemble issues many events.
        self._http: httpx.AsyncClient | None = None

    async def _get_http(self) -> httpx.AsyncClient:
        """Return the shared ``httpx.AsyncClient``, creating it on first use."""
        if self._http is None or self._http.is_closed:
            from ..core.transports import active

            self._http = active().async_client(
                timeout=10.0,
                limits=httpx.Limits(
                    max_keepalive_connections=5,
                    max_connections=10,
                    keepalive_expiry=30.0,
                ),
            )
        return self._http

    async def aclose(self) -> None:
        """Close the shared HTTP client. Call at process shutdown."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None

    async def start(self) -> None:
        """Start the HTTP SSE transport server.

        Only activates when orpheus_transport="http_sse" in config
        or when BEAGLE_EXECUTION_ENV=cloud.
        """
        if self.config.orpheus_transport != "http_sse":
            logger.debug("Orpheus HTTP transport disabled (not cloud mode)")
            return

        try:
            import uvicorn  # type: ignore[import-untyped]
            from fastapi import FastAPI, Request  # type: ignore[import-untyped]
            from fastapi.responses import StreamingResponse  # type: ignore[import-untyped]

            app = FastAPI(title="Orpheus HTTP Transport", version="1.0.0")
            self._app = app  # type: ignore[assignment]

            @app.post("/orpheus/publish")
            async def publish_event(request: Request) -> dict:
                """Publish an event to the Orpheus bus via HTTP POST."""
                content_length = request.headers.get("content-length")
                if content_length and int(content_length) > 10_000_000:
                    from fastapi.responses import JSONResponse  # type: ignore[import-untyped]

                    # FastAPI routes may return a Response object (HTTP 413)
                    # in addition to the declared dict happy-path payload.
                    err_response: dict[str, Any] = JSONResponse(  # type: ignore[assignment]
                        content={"error": "Payload too large (max 10MB)"},
                        status_code=413,
                    )
                    return err_response
                body = await request.json()
                event_type = body.get("event_type", "custom")
                event_data = body.get("data", {})

                # Create and publish event
                event = BeagleEvent(event_type=event_type, **event_data)
                self._bus.publish(event)

                # Push to SSE subscribers
                event_json = json.dumps(body, default=str)
                for queue in self._subscribers:
                    await queue.put(event_json)

                return {"status": "ok", "event_type": event_type}

            @app.get("/orpheus/events")
            async def event_stream() -> StreamingResponse:
                """SSE stream of Orpheus events."""
                queue: asyncio.Queue = asyncio.Queue()
                self._subscribers.append(queue)

                async def generate():
                    try:
                        while True:
                            try:
                                data = await asyncio.wait_for(queue.get(), timeout=30)
                                yield f"data: {data}\n\n"
                            except TimeoutError:
                                # Send keepalive
                                yield ": keepalive\n\n"
                    except asyncio.CancelledError:
                        # Doctrine: never swallow CancelledError — re-raise so the
                        # cancellation propagates. The finally below still runs and
                        # removes this subscriber's queue.
                        raise
                    finally:
                        if queue in self._subscribers:
                            self._subscribers.remove(queue)

                return StreamingResponse(
                    generate(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            @app.get("/orpheus/health")
            async def health() -> dict:
                return {
                    "status": "ok",
                    "transport": "http_sse",
                    "subscribers": len(self._subscribers),
                }

            # Choose port (offset from A2A port to avoid conflict)
            port = 8430  # Fixed port for Orpheus HTTP transport

            logger.info(f"Orpheus HTTP transport starting on port {port}")
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
            self._server = uvicorn.Server(config)  # type: ignore[assignment]
            await self._server.serve()  # type: ignore[attr-defined]

        except ImportError:
            logger.warning("FastAPI/uvicorn not installed — Orpheus HTTP transport not started")
        except Exception as exc:  # broad catch intentional
            logger.error(f"Orpheus HTTP transport failed to start: {exc}", exc_info=True)

    async def stop(self) -> None:
        """Stop the HTTP transport server."""
        if self._server:
            self._server.should_exit = True
            self._subscribers.clear()
            logger.info("Orpheus HTTP transport stopped")

    async def publish(self, event_type: str, data: dict[str, Any]) -> bool:
        """Publish an event via HTTP POST (if server is running).

        For use by cloud-deployed workflows that need to push
        events back to the local Orpheus bus.

        Args:
            event_type: Event type string.
            data: Event payload.

        Returns:
            True if published successfully.

        """
        try:
            client = await self._get_http()
            resp = await client.post(
                "http://127.0.0.1:8430/orpheus/publish",
                json={"event_type": event_type, "data": data},
            )
            return bool(resp.status_code == 200)
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Orpheus HTTP publish failed, falling back to local bus: {exc}")
            # Local publish fallback
            event = BeagleEvent(event_type=event_type, **data)
            self._bus.publish(event)
            return True
