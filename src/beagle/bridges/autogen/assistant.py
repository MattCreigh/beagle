"""AutoGen AssistantAgent backed by Beagle."""

from __future__ import annotations

from typing import Any

from .agent import BeagleAutoGenAgent


class BeagleAutoGenAssistant(BeagleAutoGenAgent):
    """AutoGen-compatible AssistantAgent.

    Generates LLM responses. Can use tools.
    Delegates to Beagle's subprocess pool for all inference.
    """

    def __init__(
        self,
        name: str = "assistant",
        system_message: str = "You are a helpful AI assistant.",
        tools: list | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, system_message=system_message, **kwargs)
        self._tools = tools or []
