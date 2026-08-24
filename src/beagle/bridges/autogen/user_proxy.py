"""AutoGen UserProxyAgent backed by Beagle."""

from __future__ import annotations

from typing import Any

from .agent import BeagleAutoGenAgent


class BeagleAutoGenUserProxy(BeagleAutoGenAgent):
    """AutoGen-compatible UserProxyAgent.

    In Beagle context, this agent provides task input and receives results.
    Does NOT execute code (Beagle uses SandboxedExecutor for that).
    """

    def __init__(
        self,
        name: str = "user_proxy",
        human_input_mode: str = "NEVER",
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, **kwargs)
        self.human_input_mode = human_input_mode

    async def generate_reply(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        """UserProxy doesn't generate LLM replies by default."""
        if self.human_input_mode == "NEVER":
            return ""
        return await super().generate_reply(messages)
