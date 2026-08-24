"""In-process chatrecall adapter (F4 fix — v13.21.3).

Why this module exists
-----------------------
The v13.21 audit flagged that ``tom_hydrator._chat_query`` did
``from chatrecall import chatrecall`` — but ``chatrecall`` is the
**MCP server name** on the wire (the registered tool is named
``chatrecall__chatrecall``), not an importable Python module. Every
hydration call therefore raised ``ImportError``, was caught by the
defensive ``except`` in ``_resolve_one``, and silently returned an
empty result. The chat side of the hydration block was a permanent
no-op.

This module is the in-process adapter that stands in for the not-yet-
shipped chatrecall Python module. It is intentionally tiny: a single
``chatrecall(query, limit)`` callable that returns an empty list. The
behaviour it represents is "the chatrecall MCP server is not yet
plumbed into the in-process path; emit nothing". When the real
chatrecall implementation lands (a thin wrapper around the existing
``chats/*.jsonl`` corpus, most likely), this adapter will be
replaced — the hydrator's import path is already pointed here.

The module is also the **test seam**: existing hydrator tests replace
``sys.modules['beagle.style_guides._chatrecall_adapter']``
with a fake to exercise the chat-side code path. This is the same
pattern the F4 fix uses for RAG (the real ``mcp_rag_server.rag_search``
is imported at call time and replaced via ``sys.modules`` mocks in
tests).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("Beagle.style_guides.chatrecall_adapter")


def chatrecall(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Stub chatrecall search — returns an empty result.

    Real implementation will eventually look up the query in the
    chatrecall corpus (a JSONL of past goose sessions stored under
    ``~/.config/goose/chats/``) and return the top-*limit* matches
    formatted as ``{"role": ..., "content": ...}`` dicts. Until then,
    we emit nothing — the hydration block's ``<chat_result>`` will be
    empty, which the renderer renders as an empty element (not an
    error), and the operator can see the absence in the canonical
    artefact's ``hydrated`` section.

    Args:
        query: Natural-language search intent (the TOML author
            declared this in ``beagle_core_directives.toml
            [meta].chat_queries``).
        limit: Maximum number of messages to return. Currently
            unused (the stub always returns ``[]``); kept in the
            signature for forward compatibility with the real
            implementation.

    Returns:
        An empty list. A non-empty list of ``{"role", "content"}``
        dicts is the expected shape when the real implementation
        lands — see ``_summarise_chat`` in ``tom_hydrator.py`` for
        the consumer side.

    """
    logger.debug(
        "chatrecall_adapter.chatrecall: stub returning [] (real impl not yet shipped); "
        "query=%r, limit=%d",
        query,
        limit,
    )
    return []


__all__ = ["chatrecall"]
