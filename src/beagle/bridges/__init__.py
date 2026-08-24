"""Beagle ↔ LangChain Ecosystem Compatibility Bridges.

Provides 6 compatibility layers making Beagle a first-class citizen
in the LangChain ecosystem, consistent with the plan in
Dev/plans/langchain_ecosystem_compat.md.

Phases:
  1. BeagleRetriever — LangChain BaseRetriever for CAST-indexed RAG
  2. LangChainToolNode — TOML-driven LangChain tool adapter for Beagle workflows
  3. OllamaCloudChatModel — BaseChatModel for in-process LLM calls via Ollama Cloud
  4. BeagleLangSmithBridge — OTel ↔ LangSmith observability bridge
  5. A2A Bridge — Inter-framework agent federation via A2A protocol
  6. Cloud readiness — LangGraph Cloud deployment support

All configuration lives in config.toml [langchain_bridges] section.
All model calls route through Ollama Cloud exclusively.
"""

from __future__ import annotations

import logging

# non-empty-init-module is a preview rule; under preview=false it no longer
# fires, so no suppression is needed. The module-logger and lazy module map are
# deliberate (see below).
logger = logging.getLogger("Beagle.bridges")

# Lazy imports — only load sub-modules when accessed to avoid
# pulling in langchain-openai/langsmith/etc. when unused.

_BRIDGE_MODULES = {
    "retriever": ".retriever",
    "config": ".config",
    "tool_registry": ".tool_registry",
    "tool_node": ".tool_node",
    "chat_model": ".chat_model",
    "llm_node": ".llm_node",
    "llm_direct": ".llm_direct",
    "llm_client_registry": ".llm_client_registry",
    "otel_langsmith_bridge": ".otel_langsmith_bridge",
    "callback_handler": ".callback_handler",
    "a2a_server": ".a2a_server",
    "a2a_client": ".a2a_client",
    "a2a_card_builder": ".a2a_card_builder",
    "orpheus_http_transport": ".orpheus_http_transport",
}


def __getattr__(name: str):
    """Lazy-load bridge sub-modules only when accessed."""
    if name in _BRIDGE_MODULES:
        import importlib

        module = importlib.import_module(_BRIDGE_MODULES[name], __name__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_BRIDGE_MODULES.keys())
