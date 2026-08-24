"""Beagle LLM adapter for CrewAI.

Replaces CrewAI's native LLM with Beagle's execution pipeline:
model resolution → learned routing → subprocess pool → cost tracking.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("Beagle.bridges.crewai.llm")


class BeagleCrewAILLM:
    """Drop-in replacement for CrewAI LLM that routes through Beagle.

    CrewAI agents call self.llm.call(messages) to get responses.
    We intercept this and route through Beagle's subprocess pool with
    cost tracking, Guardian approval, and learned routing.
    """

    def __init__(
        self,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 4000,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._kwargs = kwargs

        # Resolve model via Beagle
        if not self.model:
            try:
                from beagle.config.model_resolver import (
                    resolve_model,
                )

                self.model = resolve_model(None, None, "normal")
            except ImportError:
                self.model = "glm-5.1:cloud"

    def call(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        """Synchronous LLM call — CrewAI's primary interface.

        Converts messages to a prompt, routes through Beagle's subprocess
        pool, and returns the response text.
        """
        prompt = self._messages_to_prompt(messages)
        system = ""
        for msg in messages:
            if msg.get("role") == "system":
                system = msg.get("content", "")
                break

        try:
            # v1.2.0 (RG-7, BGL-008): this is a synchronous boundary (CrewAI's
            # call interface). The prior code called asyncio.get_event_loop(),
            # and when a loop was running it scheduled the coroutine on that
            # same loop via run_coroutine_threadsafe then blocked on
            # future.result() — the loop could not advance, so the call hung
            # for 300s. asyncio.run() is the correct sync-boundary primitive.
            return asyncio.run(self._async_call(prompt, system))
        except Exception as e:  # broad catch intentional
            logger.error(f"Beagle LLM call failed: {e}")
            raise

    async def _async_call(self, prompt: str, system: str) -> str:
        """Async LLM call through Beagle's subprocess pool."""
        from beagle.utils.subprocess_pool import run_goose

        result, _raw = await run_goose(
            prompt=prompt,
            system_directive=system or "You are a helpful assistant.",
            node_name=f"crewai-{self.model}",
            model_override=self.model,
            timeout=300,
        )
        return result

    @staticmethod
    def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
        """Convert chat messages to a single prompt string."""
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                continue  # Handled separately
            parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)
