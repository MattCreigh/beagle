"""Base Beagle adapter for AutoGen ConversableAgent."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("Beagle.bridges.autogen.agent")


@dataclass
class TaskResult:
    """AutoGen-compatible task result."""

    chat_history: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""


class BeagleAutoGenAgent:
    """AutoGen-compatible ConversableAgent backed by Beagle.

    Implements the core AutoGen agent interface: send, receive,
    generate_reply, initiate_chat. All LLM calls route through
    Beagle's subprocess pool with cost tracking and security.
    """

    def __init__(
        self,
        name: str = "assistant",
        system_message: str = "",
        llm_config: dict | None = None,
        max_consecutive_auto_reply: int = 100,
        **kwargs: Any,
    ) -> None:
        self.name = name
        self.system_message = system_message
        self.llm_config = llm_config or {}
        self.max_consecutive_auto_reply = max_consecutive_auto_reply
        self.chat_messages: dict[str, list[dict]] = {}
        self._tools: list[dict] = []
        self._reply_funcs: list[tuple] = []
        self._kwargs = kwargs

        # Resolve Beagle model
        model = ""
        if isinstance(self.llm_config, dict):
            model = self.llm_config.get("model", "")
        if not model:
            try:
                from beagle.config.model_resolver import (
                    resolve_model,
                )

                model = resolve_model(None, None, "normal")
            except ImportError:
                model = "glm-5.1:cloud"
        self._model = model

    async def generate_reply(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        """Generate a reply given message history."""
        from beagle.utils.subprocess_pool import run_goose

        # Semantic firewall check on input
        try:
            from beagle.security.validation import (
                validate_query_async,
            )

            combined_input = "\n".join(m.get("content", "") for m in messages)
            is_valid, error = await validate_query_async(combined_input[:5000])
            if not is_valid:
                return f"[Beagle Security] Input blocked: {error}"
        except ImportError as exc:
            # A security control that cannot load must be loud. Logged at error,
            # not warning: the request proceeds WITHOUT input validation.
            logger.error(
                "Cannot import the query validator (%s); this AutoGen request is being "
                "processed with NO input validation.",
                exc,
            )

        # Build prompt from messages
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", msg.get("source", "user"))
            content = msg.get("content", "")
            prompt_parts.append(f"[{role}]: {content}")
        prompt = "\n\n".join(prompt_parts)

        result, _raw = await run_goose(
            prompt=prompt,
            system_directive=self.system_message or f"You are {self.name}.",
            node_name=f"autogen-{self.name}",
            model_override=self._model,
            timeout=300,
        )
        return result

    async def send(
        self,
        message: str | dict,
        recipient: BeagleAutoGenAgent,
    ) -> None:
        """Send a message to another agent."""
        if isinstance(message, str):
            message = {
                "role": "user",
                "content": message,
                "source": self.name,
            }
        if recipient.name not in self.chat_messages:
            self.chat_messages[recipient.name] = []
        self.chat_messages[recipient.name].append(message)
        await recipient.receive(message, self)

    async def receive(
        self,
        message: str | dict,
        sender: BeagleAutoGenAgent,
    ) -> str:
        """Receive a message and generate a reply."""
        if isinstance(message, str):
            message = {
                "role": "user",
                "content": message,
                "source": sender.name,
            }
        if sender.name not in self.chat_messages:
            self.chat_messages[sender.name] = []
        self.chat_messages[sender.name].append(message)

        reply = await self.generate_reply(self.chat_messages[sender.name])
        reply_msg = {
            "role": "assistant",
            "content": reply,
            "source": self.name,
        }
        self.chat_messages[sender.name].append(reply_msg)
        return reply

    async def initiate_chat(
        self,
        recipient: BeagleAutoGenAgent,
        message: str = "",
        max_turns: int | None = None,
    ) -> TaskResult:
        """Start a conversation with another agent."""
        max_turns = max_turns or 10
        history: list[dict] = []

        # Send initial message
        await self.send(message, recipient)
        history.append({"role": "user", "content": message, "source": self.name})

        for turn in range(max_turns):
            # Recipient generates reply
            reply_msgs = self.chat_messages.get(recipient.name, [])
            if reply_msgs:
                last = reply_msgs[-1]
                history.append(last)

                # Check for termination
                content = last.get("content", "")
                if "TERMINATE" in content:
                    break

                # Send reply back if this agent should respond
                if turn < max_turns - 1 and last.get("source") == recipient.name:
                    my_reply = await self.generate_reply(reply_msgs)
                    reply_msg = {
                        "role": "user",
                        "content": my_reply,
                        "source": self.name,
                    }
                    await self.send(my_reply, recipient)
                    history.append(reply_msg)

        summary = history[-1].get("content", "") if history else ""
        return TaskResult(chat_history=history, summary=summary)

    def clear_history(self, agent_name: str = "") -> None:
        """Clear chat history."""
        if agent_name:
            self.chat_messages.pop(agent_name, None)
        else:
            self.chat_messages.clear()
