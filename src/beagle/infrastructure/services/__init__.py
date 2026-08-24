"""Services — embedding, etc.

SP-12: the lazy imports were flagged as unused because the names are returned
by string via ``locals().get(name)``, which ruff cannot trace. Rewritten to
import into a module-level registry dict so the references are concrete (no
``unused-import`` suppressions needed) while keeping lazy loading.
"""

__all__ = [
    "OllamaCloudEmbedder",
    "SentenceTransformerEmbedder",
    "get_embedder",
    "get_local_embedder",
    "reset_embedder",
]

# Imported once on first access (see __getattr__). Held in a dict so the
# module attribute lookup returns the concrete object.
_COMPONENTS: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """Lazy import service components."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not _COMPONENTS:
        from .embedding import (
            OllamaCloudEmbedder,
            SentenceTransformerEmbedder,
            get_embedder,
            get_local_embedder,
            reset_embedder,
        )

        _COMPONENTS.update(
            {
                "OllamaCloudEmbedder": OllamaCloudEmbedder,
                "SentenceTransformerEmbedder": SentenceTransformerEmbedder,
                "get_embedder": get_embedder,
                "get_local_embedder": get_local_embedder,
                "reset_embedder": reset_embedder,
            }
        )
    return _COMPONENTS[name]
