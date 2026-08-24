"""Message format conversion between Beagle events and AutoGen messages."""

from __future__ import annotations

from typing import Any


def beagle_event_to_autogen_message(
    event: dict[str, Any],
) -> dict[str, str]:
    """Convert an Beagle event to an AutoGen message dict."""
    return {
        "role": event.get("role", "assistant"),
        "content": event.get("content", ""),
        "source": event.get("source", event.get("agent_name", "system")),
    }


def autogen_message_to_beagle_event(
    message: dict[str, str],
) -> dict[str, Any]:
    """Convert an AutoGen message to an Beagle event dict."""
    return {
        "event_type": "agent_message",
        "role": message.get("role", "user"),
        "content": message.get("content", ""),
        "agent_name": message.get("source", "unknown"),
    }
