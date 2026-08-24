"""Beagle LangChain Callback Handler — Bridge to Orpheus event bus.

Phase 4 companion: translates LangChain callback events into
Beagle Orpheus events, enabling unified monitoring across all
node types (Goose subprocess + LangChain in-process).

Also provides the conduit by which LangChain LLM/tool nodes
become visible in Beagle's event-driven monitoring pipeline.

Usage:
    from beagle.bridges.callback_handler import BeagleCallbackHandler

    handler = BeagleCallbackHandler()
    chat = OllamaCloudChatModel()
    result = await chat.ainvoke(messages, config={"callbacks": [handler]})
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from ..events import NodeCompleted, NodeStarted, get_event_bus

logger = logging.getLogger("Beagle.bridges.callback_handler")


class BeagleCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that bridges to Beagle's Orpheus event bus.

    Translates LangChain's on_llm_start/end, on_tool_start/end,
    on_chain_start/end, and on_retriever_start/end into Orpheus events.

    This means any LangChain node (from Phase 2/3) fired via
    callbacks is visible in Beagle's monitoring pipeline — alongside
    Goose subprocess nodes that publish via NodeFailed/NodeCompleted.
    """

    def __init__(self, workflow_id: str = "") -> None:
        self._workflow_id = workflow_id
        self._start_times: dict[str, float] = {}

    def _get_bus(self) -> Any:
        return get_event_bus()

    # ── LLM callbacks ─────────────────────────────────────────────────────

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Fired when an LLM call starts."""
        _ = prompts, parent_run_id, tags, metadata
        run_id_str = str(run_id)
        self._start_times[run_id_str] = time.monotonic()
        model_name = serialized.get("kwargs", {}).get("model_name", "unknown")
        logger.debug(f"[Callback] LLM started: model={model_name}")
        self._get_bus().publish(
            NodeStarted(
                workflow_id=self._workflow_id,
                node_name=f"llm:{model_name}",
                model=model_name,
            )
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """Fired when an LLM call completes."""
        run_id = str(kwargs.get("run_id", "unknown"))
        start = self._start_times.pop(run_id, time.monotonic())
        duration = time.monotonic() - start
        logger.debug(f"[Callback] LLM completed: {duration:.2f}s")
        self._get_bus().publish(
            NodeCompleted(
                workflow_id=self._workflow_id,
                node_name="llm",
                duration_seconds=duration,
            )
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """Fired when an LLM call fails."""
        run_id = str(kwargs.get("run_id", "unknown"))
        self._start_times.pop(run_id, None)
        from ..events import NodeFailed

        logger.debug(f"[Callback] LLM error: {error}")
        self._get_bus().publish(
            NodeFailed(
                workflow_id=self._workflow_id,
                node_name="llm",
                error=str(error),
                attempt=1,
                error_category="llm_error",
            )
        )

    # ── Tool callbacks ────────────────────────────────────────────────────

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Fired when a tool call starts."""
        _ = input_str, parent_run_id, tags, metadata, inputs
        run_id_str = str(run_id)
        self._start_times[run_id_str] = time.monotonic()
        tool_name = serialized.get("name", "unknown_tool")
        logger.debug(f"[Callback] Tool started: {tool_name}")
        self._get_bus().publish(
            NodeStarted(
                workflow_id=self._workflow_id,
                node_name=f"tool:{tool_name}",
            )
        )

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        """Fired when a tool call completes."""
        run_id = str(kwargs.get("run_id", "unknown"))
        start = self._start_times.pop(run_id, time.monotonic())
        duration = time.monotonic() - start
        logger.debug(f"[Callback] Tool completed: {duration:.2f}s")
        self._get_bus().publish(
            NodeCompleted(
                workflow_id=self._workflow_id,
                node_name="tool",
                duration_seconds=duration,
            )
        )

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        """Fired when a tool call fails."""
        run_id = str(kwargs.get("run_id", "unknown"))
        self._start_times.pop(run_id, None)
        from ..events import NodeFailed

        self._get_bus().publish(
            NodeFailed(
                workflow_id=self._workflow_id,
                node_name="tool",
                error=str(error),
                attempt=1,
                error_category="tool_error",
            )
        )

    # ── Chain callbacks ──────────────────────────────────────────────────

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Fired when a chain starts."""
        _ = inputs, parent_run_id, tags, metadata, name
        run_id_str = str(run_id)
        self._start_times[run_id_str] = time.monotonic()
        chain_name = serialized.get("name", serialized.get("id", ["unknown"])[-1])
        logger.debug(f"[Callback] Chain started: {chain_name}")

    def on_chain_end(self, outputs: dict, **kwargs: Any) -> None:
        """Fired when a chain completes."""
        run_id = str(kwargs.get("run_id", "unknown"))
        start = self._start_times.pop(run_id, time.monotonic())
        duration = time.monotonic() - start
        logger.debug(f"[Callback] Chain completed: {duration:.2f}s")

    def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        """Fired when a chain fails."""
        run_id = str(kwargs.get("run_id", "unknown"))
        self._start_times.pop(run_id, None)

    # ── Retriever callbacks ───────────────────────────────────────────────

    def on_retriever_start(self, serialized: dict, query: str, **kwargs: Any) -> None:
        """Fired when a retriever starts."""
        run_id = str(kwargs.get("run_id", "unknown"))
        self._start_times[run_id] = time.monotonic()
        logger.debug(f"[Callback] Retriever started: query={query[:50]}")

    def on_retriever_end(self, documents: list, **kwargs: Any) -> None:  # type: ignore[override]
        """Fired when a retriever completes."""
        run_id = str(kwargs.get("run_id", "unknown"))
        start = self._start_times.pop(run_id, time.monotonic())
        duration = time.monotonic() - start
        logger.debug(f"[Callback] Retriever completed: {len(documents)} docs in {duration:.2f}s")

    def on_retriever_error(self, error: BaseException, **kwargs: Any) -> None:
        """Fired when a retriever fails."""
        run_id = str(kwargs.get("run_id", "unknown"))
        self._start_times.pop(run_id, None)
