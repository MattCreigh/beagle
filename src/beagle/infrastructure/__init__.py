# <decision date="v13.7.0" status="settled">
# The 40+ modules here stay flat rather than being split into
# infrastructure/mcp/, infrastructure/rag/ and infrastructure/openclaw/.
# The flat layout works, and the split would rewrite every import of this
# package for no behavioural gain. This is a decision that was taken, not work
# that is outstanding — re-open it only with a concrete reason the flat layout
# is causing a problem.
# </decision>

"""Infrastructure services - RAG, OpenClaw, Docker, etc."""

from importlib import import_module

__all__ = [
    "cast_ingestion",
    "constraint_extractor",
    "constraint_registry",
    "embedding",
    "knowledge_extractor",
    "mcp_rag_server",
    "mcp_utility_server",
    "semantic_knowledge",
    "session_memory",
]


def __getattr__(name: str):
    """Lazy import infrastructure components."""
    lazy_imports = {
        "cast_ingestion": ".cast_ingestion",
        "mcp_rag_server": ".mcp_rag_server",
        "mcp_utility_server": ".mcp_utility_server",
        "embedding": ".services.embedding",
        "constraint_registry": ".constraint_registry",
        "constraint_extractor": ".constraint_extractor",
        "semantic_knowledge": ".semantic_knowledge",
        "knowledge_extractor": ".knowledge_extractor",
        "session_memory": ".session_memory",
    }
    if name in lazy_imports:
        module = import_module(lazy_imports[name], __package__)
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
