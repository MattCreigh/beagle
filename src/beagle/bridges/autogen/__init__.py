"""AutoGen Runtime Bridge — run AutoGen agents inside Beagle.

Provides adapter classes that implement AutoGen's interfaces but
delegate execution to Beagle's hardened pipeline:
- LLM calls → Beagle model resolution + learned routing + cost tracking
- Tools → Beagle MCP tools with Guardian approval
- Memory → Beagle HierarchicalMemory
- Security → Beagle semantic firewall on all message passing

Usage:
    from beagle.bridges.autogen import (
        BeagleAssistant, BeagleUserProxy, BeagleGroupChat
    )

    assistant = BeagleAssistant(name="coder", system_message="You write code")
    user = BeagleUserProxy(name="user")
    result = await user.initiate_chat(assistant, message="Write hello world")
"""

from __future__ import annotations

from .agent import BeagleAutoGenAgent
from .assistant import BeagleAutoGenAssistant as BeagleAssistant
from .group_chat import BeagleGroupChat
from .user_proxy import BeagleAutoGenUserProxy as BeagleUserProxy

__all__ = ["BeagleAssistant", "BeagleAutoGenAgent", "BeagleGroupChat", "BeagleUserProxy"]
