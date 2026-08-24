"""AutoGen GroupChat backed by Beagle's DAGOrchestrator."""

from __future__ import annotations

import logging
from typing import Any

from .agent import BeagleAutoGenAgent, TaskResult

logger = logging.getLogger("Beagle.bridges.autogen.group_chat")


class BeagleGroupChat:
    """AutoGen-compatible GroupChat using Beagle's event bus for coordination.

    Supports round_robin and auto speaker selection.
    """

    def __init__(
        self,
        agents: list[BeagleAutoGenAgent] | None = None,
        max_round: int = 10,
        speaker_selection_method: str = "round_robin",
        **kwargs: Any,
    ) -> None:
        self.agents = agents or []
        self.max_round = max_round
        self.speaker_selection_method = speaker_selection_method
        self.messages: list[dict[str, str]] = []
        self._kwargs = kwargs

    async def run(self, task: str) -> TaskResult:
        """Execute group chat with the given task."""
        # Cost tracking
        try:
            from beagle.cost_tracker import (
                ContextAwareCostTracker,
            )

            tracker = ContextAwareCostTracker(budget_usd=10.0)
        except ImportError:
            tracker = None

        if not self.agents:
            return TaskResult(summary="No agents in group chat")

        self.messages = [{"role": "user", "content": task, "source": "system"}]

        for round_num in range(self.max_round):
            # Select speaker
            speaker = self._select_speaker(round_num)
            if speaker is None:
                break

            # Generate reply
            reply = await speaker.generate_reply(self.messages)
            msg = {
                "role": "assistant",
                "content": reply,
                "source": speaker.name,
            }
            self.messages.append(msg)

            logger.info(
                f"[AutoGen GroupChat] Round {round_num + 1}: "
                f"{speaker.name} responded ({len(reply)} chars)"
            )

            # Check termination
            if "TERMINATE" in reply:
                break

        summary = self.messages[-1].get("content", "") if self.messages else ""

        if tracker:
            logger.info(f"[AutoGen] Cost: ${tracker.total_cost_usd:.4f}")

        return TaskResult(chat_history=self.messages, summary=summary)

    def _select_speaker(self, round_num: int) -> BeagleAutoGenAgent | None:
        """Select next speaker based on method."""
        if not self.agents:
            return None

        if self.speaker_selection_method == "round_robin":
            idx = round_num % len(self.agents)
            return self.agents[idx]

        # "auto" — simple heuristic: last speaker doesn't go twice
        if len(self.messages) > 1:
            last_source = self.messages[-1].get("source", "")
            candidates = [a for a in self.agents if a.name != last_source]
            if candidates:
                return candidates[0]

        return self.agents[0]
