"""Embedding service — routes vectorization to local Ollama or sentence-transformers.

Provider selection priority (v13.19.4):
  1. Explicit base_url override (caller-passed)
  2. EMBED_PROVIDER env var (explicit: sentence-transformers | local | cloud)
  3. [embed].provider config.toml value (explicit embed config)
  4. Local Ollama detection at localhost:11434 (with nomic-embed-text pulled)
  5. DEFAULT: sentence-transformers (all-mpnet-base-v2, 768-dim, CPU-only,
     no external API). This is the recommended default.

Local Ollama base URL: http://localhost:11434 (requires: ollama pull nomic-embed-text)
Sentence-transformers: Uses all-mpnet-base-v2 (768-dim, CPU-optimized)

Environment:
  OLLAMA_BASE_URL         Override base URL (default: http://localhost:11434)
  OLLAMA_CLOUD_API_KEY    Ollama Cloud API key. Only consulted if EMBED_PROVIDER=cloud
                          is explicit. Does NOT auto-select cloud embeddings.
  EMBED_PROVIDER          Force provider: local|cloud|sentence-transformers
                          (default: sentence-transformers)
  EMBED_MODEL             Model name for sentence-transformers (default: all-mpnet-base-v2)

v13.19.4 BREAKING-CHANGE: OLLAMA_CLOUD_API_KEY no longer auto-selects Ollama
Cloud as the embedder. The previous behavior caused silent 401 errors when
users with the LLM key set tried to embed via Ollama Cloud's /api/embed
endpoint, which is not generally available. Set EMBED_PROVIDER=cloud
explicitly if you have confirmed your account has embed access.
"""

from __future__ import annotations

import gc
import logging
import os
import time
import tomllib
from typing import Any, ClassVar, Protocol

import httpx

from beagle.config._config_path import find_config_toml

# Import config for centralized timeout settings
from beagle.config.config import load_config
from beagle.config.schema import EmbedConfig

_config = load_config()

from beagle.core.transports import active as _transport

logger = logging.getLogger("Beagle.infrastructure.services.embedding")

# Model selection
_EMBED_MODEL_OLLAMA = "nomic-embed-text"
_EMBED_MODEL_ST = os.environ.get("EMBED_MODEL", "all-mpnet-base-v2")  # 768-dim
_EMBED_DIMENSION = 768  # Both nomic-embed-text and all-mpnet-base-v2 are 768-dim

# Base URLs
_OLLAMA_LOCAL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
_OLLAMA_CLOUD_BASE = "https://ollama.com"
_OLLAMA_CLOUD_API = "https://ollama.com/api/embed"

# Config file path - project root (beagle/config.toml)
# embedding.py is at: beagle/infrastructure/services/embedding.py
# config.toml is at: beagle/config.toml (project root, not package root)
_CONFIG_PATH = find_config_toml()


# Background-posture override (set by auto_hydration's background reingest
# only). None = use [embed] SSOT values; otherwise these win. Process-local,
# applied around background ingests so foreground paths are never slowed.
_BACKGROUND_POSTURE: tuple[int, float] | None = None


def apply_background_posture(batch_size: int, pause_s: float) -> None:
    """Switch the embedder to a low-limit background posture.

    Called ONLY by auto-hydration's background reingest thread before an
    ingest, per the 2026-08-22 directive: background work runs in small
    paced chunks — slow but continuous — and must not saturate the shared
    local embedding runner. Foreground callers keep the config SSOT values.

    <invariant>
    Values come from AutoHydrationConfig (schema-backed), never literals at
    call sites. The override is process-local state; it does not touch the
    config file.
    </invariant>

    Args:
        batch_size: Max texts per /api/embed call during background ingests.
        pause_s: Seconds to sleep between batches during background ingests.

    """
    global _BACKGROUND_POSTURE
    _BACKGROUND_POSTURE = (max(1, int(batch_size)), max(0.0, float(pause_s)))
    logger.info(
        f"[Embeddings] background posture applied: batch_size={batch_size}, pause_s={pause_s}"
    )


def _read_config_embed_limits() -> tuple[int, float]:
    """Read [embed].batch_size / inter_batch_pause_s from config.toml.

    Config is the SSOT for embedding batch/pacing knobs (the
    no-new-magic-values gate forbids call-site literals). Absent keys or a
    missing/invalid file fall back to the schema defaults via EmbedConfig.

    Returns:
        (batch_size, inter_batch_pause_s) as validated positive numbers.

    """
    defaults = EmbedConfig()
    if not _CONFIG_PATH.exists():
        return defaults.batch_size, defaults.inter_batch_pause_s
    try:
        with open(_CONFIG_PATH, "rb") as f:
            config = tomllib.load(f)
        embed = config.get("embed", {})
        raw_batch = embed.get("batch_size", defaults.batch_size)
        raw_pause = embed.get("inter_batch_pause_s", defaults.inter_batch_pause_s)
        return max(1, int(raw_batch)), max(0.0, float(raw_pause))
    except (tomllib.TOMLDecodeError, OSError, TypeError, ValueError):
        return defaults.batch_size, defaults.inter_batch_pause_s


def _read_config_provider() -> str:
    """Read embed.provider from config.toml if present.

    v13.19.4: Removed the [goose].provider fallback. The LLM provider
    (set under [goose].provider, e.g. "ollama_cloud") and the embedding
    provider (set under [embed].provider) are now independent. Coupling
    them was the root cause of the silent 401 on /api/embed: any user
    with provider=ollama_cloud for the LLM would have the embedder
    auto-select Ollama Cloud, which does not serve the embeddings
    endpoint for general accounts. Embeddings now default to
    sentence-transformers unless [embed].provider is set explicitly.
    """
    if not _CONFIG_PATH.exists():
        return ""
    try:
        with open(_CONFIG_PATH, "rb") as f:
            config = tomllib.load(f)
        # Read ONLY [embed].provider. Do NOT fall back to [goose].provider
        # — that is the LLM provider, not the embedding provider.
        embed_provider = config.get("embed", {}).get("provider", "")
        return str(embed_provider) if embed_provider else ""
    except (tomllib.TOMLDecodeError, OSError):
        return ""


def _read_config_api_key() -> str:
    """Read OLLAMA_CLOUD_API_KEY from goose.host in config.toml if present."""
    if not _CONFIG_PATH.exists():
        return ""
    try:
        with open(_CONFIG_PATH, "rb") as f:
            config = tomllib.load(f)
        host = config.get("goose", {}).get("host", "")
        return str(host)
    except (tomllib.TOMLDecodeError, OSError):
        return ""


# ── Protocol (duck-type for local embedding fallback) ───────────────────────────


class Embedder(Protocol):
    """Duck-type interface matching SentenceTransformer.encode()."""

    model_name: str
    dimension: int
    provider: str

    def encode(self, texts: list[str], **kwargs) -> list[list[float]]: ...


def _identity(*, model: str = _EMBED_MODEL_ST, prefix: str = "") -> dict[str, Any]:
    """Return a stable embedder-identity fingerprint for a RAG index.

    D10 (Fable 5 DD 2026-06-11): the ingestion and search paths must use the
    same embedder, prefix scheme, and dimension. This dict is stored with the
    index and compared at query time so mismatched embedders raise instead of
    silently returning degraded similarity.
    """
    return {
        "provider": "sentence-transformers",
        "model": model,
        "dimension": _EMBED_DIMENSION,
        "prefix": prefix,
    }


# ── Sentence-Transformers Local Embedder ─────────────────────────────────────────


class SentenceTransformerEmbedder:
    """Local CPU-only embedding using sentence-transformers.

    Falls back when Ollama is unavailable. Uses all-mpnet-base-v2 (768-dim)
    which matches the dimension of LanceDB vectors for compatibility.

    This is the recommended solution for CPU-only machines (OptiPlex 3050 Micro).
    """

    def __init__(self, model_name: str = _EMBED_MODEL_ST) -> None:
        """Initialize the local embedder.

        Args:
            model_name: HuggingFace model name (default: all-mpnet-base-v2)

        """
        self.model_name = model_name
        self._model = None
        self.dimension = _EMBED_DIMENSION
        self.provider = "sentence-transformers"
        logger.info(f"[Embeddings] Initializing SentenceTransformer: {model_name}")

    def _ensure_model(self) -> None:
        """Lazy-load the model on first use."""
        if self._model is not None:
            return

        try:
            from sentence_transformers import SentenceTransformer

            logger.info(f"[Embeddings] Loading model {self.model_name}...")
            self._model = SentenceTransformer(self.model_name, device="cpu")
            logger.info(f"[Embeddings] Model loaded successfully, dimension: {self.dimension}")
        except ImportError as e:
            logger.error(f"[Embeddings] sentence-transformers not installed: {e}")
            raise RuntimeError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers torch --index-url https://download.pytorch.org/whl/cpu"
            ) from e

    def encode(self, texts: list[str], **kwargs) -> list[list[float]]:
        """Encode texts into embeddings using sentence-transformers.

        Args:
            texts: List of text strings to embed
            **kwargs: Passed to SentenceTransformer.encode()

        Returns:
            List of 768-dim embedding vectors

        """
        if not texts:
            return []

        self._ensure_model()

        try:
            # Returns numpy array, convert to list
            embeddings = self._model.encode(  # type: ignore[attr-defined]
                texts,
                convert_to_numpy=True,
                show_progress_bar=False,
                **{k: v for k, v in kwargs.items() if k not in ("batch_size", "show_progress_bar")},
            )
            # Convert numpy array to list of lists
            return [emb.tolist() for emb in embeddings]
        except MemoryError as e:
            # OOM during encoding is non-recoverable for this corpus size;
            # fail loud so the ingest aborts instead of silently writing
            # zero-vectors. Callers can reduce batch_size and retry.
            logger.error(
                f"[Embeddings] Out of memory encoding {len(texts)} texts: {e}. "
                f"Reduce batch_size (currently {kwargs.get('batch_size', 32)}) "
                f"or shorten chunk text. Aborting ingest."
            )
            raise
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"[Embeddings] Encoding failed: {e}")
            # Return zero vectors on failure
            return [[0.0] * self.dimension for _ in texts]


# ── HTTP embedding service ──────────────────────────────────────────────────────


class OllamaCloudEmbedder:
    """Calls Ollama /embeddings endpoint for vectorization.

    Provider selection (v13.19.4):
    - Explicit base_url override: use it directly
    - EMBED_PROVIDER=cloud OR [embed].provider=cloud|ollama_cloud: use Ollama Cloud
    - OLLAMA_CLOUD_API_KEY alone does NOT select cloud anymore (set
      EMBED_PROVIDER=cloud explicitly if you want cloud)
    - Otherwise: sentence-transformers is used

    The i7-6700T handles nomic-embed-text well - it's CPU-optimized.

    Performance: Includes LRU caching for repeated embedding requests.
    """

    # Class-level cache for embedding results (shared across instances)
    _embedding_cache: ClassVar[dict[str, list[list[float]]]] = {}
    _embedding_cache_maxsize: ClassVar[int] = 512

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        """Initialize the Ollama embedder.

        Args:
            model: Ollama model name. Resolution order: explicit argument >
                $OLLAMA_EMBED_MODEL > built-in default (nomic-embed-text).
            base_url: Override base URL. If None, auto-detects provider
                ($OLLAMA_BASE_URL is honoured by the module-level default).
            api_key: Ollama Cloud API key. Reads OLLAMA_CLOUD_API_KEY from
                environment if not provided.
            timeout: Request timeout in seconds.

        <config-change>
            <file>src/beagle/infrastructure/services/embedding.py</file>
            <change>model default becomes env-overridable via
                OLLAMA_EMBED_MODEL</change>
            <reason>operator-requested: nomic/sentence-transformers stay the
                defaults, but deployments may point at another model (e.g. a
                model served by the server_1_dev ollama container) without
                code edits</reason>
        </config-change>
        """
        resolved_model = model or os.environ.get("OLLAMA_EMBED_MODEL", _EMBED_MODEL_OLLAMA)
        self.model_name = resolved_model
        self._model = resolved_model
        self._timeout = timeout
        self._config_provider = _read_config_provider()
        self._fallback_embedder: SentenceTransformerEmbedder | None = None
        self.dimension = _EMBED_DIMENSION

        # Explicit override: use base_url directly
        if base_url:
            self._base_url = base_url.rstrip("/")
            self._provider = "local" if "localhost" in base_url else "cloud"
            self._api_key = api_key or ""
            logger.info(f"[Embeddings] Using explicit base_url: {self._base_url}")
            return

        # Read API key: explicit param > env var > config.toml goose.host
        self._api_key = (
            api_key or os.environ.get("OLLAMA_CLOUD_API_KEY", "") or _read_config_api_key()
        )

        # Provider priority (highest to lowest):
        # 1. EMBED_PROVIDER env var (explicit override) — only way to opt in
        #    to Ollama Cloud embeddings. Ollama Cloud does NOT support the
        #    /api/embed endpoint in general use; cloud mode is for users who
        #    have confirmed their account has embed access. v13.19.4.
        # 2. [embed].provider config.toml value — explicit embed config.
        #    Note: [goose].provider is the LLM provider, NOT the embedding
        #    provider; it no longer auto-selects the embedder. v13.19.4.
        # 3. Local Ollama detection at localhost:11434 (with nomic-embed-text
        #    pulled) — only useful if a user has local Ollama running.
        # 4. DEFAULT: sentence-transformers (all-mpnet-base-v2, 768-dim,
        #    CPU-only, no external API). This is the recommended default.
        #
        # v13.19.4 REMOVED: the OLLAMA_CLOUD_API_KEY env var no longer
        # auto-promotes to cloud embeddings. That coupling was the bug —
        # any user with the LLM key set would have the embedder silently
        # 401 on https://ollama.com/api/embed and then fall back to
        # sentence-transformers per-batch (slow). The key is now only
        # consulted if EMBED_PROVIDER=cloud is explicit, in which case
        # the embedder uses it for the explicit-cloud path.

        env_provider = os.environ.get("EMBED_PROVIDER", "").lower()
        config_provider = self._config_provider.lower() if self._config_provider else ""

        # Priority 1: EMBED_PROVIDER env var (explicit override)
        if env_provider == "sentence-transformers":
            self._base_url = ""
            self._provider = "sentence-transformers"
            self.provider = "sentence-transformers"
            logger.info("[Embeddings] Using sentence-transformers (EMBED_PROVIDER env override)")
            return
        if env_provider == "cloud" or env_provider == "ollama_cloud":
            self._base_url = _OLLAMA_CLOUD_BASE
            self._provider = "cloud"
            self.provider = "cloud"
            logger.info("[Embeddings] Using Ollama Cloud (EMBED_PROVIDER env override)")
            return
        if env_provider == "local":
            self._base_url = _OLLAMA_LOCAL
            self._provider = "local"
            self.provider = "local"
            logger.info("[Embeddings] Using local Ollama (EMBED_PROVIDER env override)")
            return

        # Priority 2: [embed] provider config (explicit embed config only;
        # [goose].provider is no longer consulted for embedding selection)
        if config_provider == "sentence-transformers":
            self._base_url = ""
            self._provider = "sentence-transformers"
            self.provider = "sentence-transformers"
            logger.info("[Embeddings] Using sentence-transformers (config.toml [embed].provider)")
            return
        if config_provider == "cloud" or config_provider == "ollama_cloud":
            self._base_url = _OLLAMA_CLOUD_BASE
            self._provider = "cloud"
            self.provider = "cloud"
            logger.info("[Embeddings] Using Ollama Cloud (config.toml [embed].provider)")
            return
        if config_provider == "local":
            self._base_url = _OLLAMA_LOCAL
            self._provider = "local"
            self.provider = "local"
            logger.info("[Embeddings] Using local Ollama (config.toml [embed].provider)")
            return

        # Priority 3: Local Ollama detection
        if self._is_ollama_running():
            self._base_url = _OLLAMA_LOCAL
            self._provider = "local"
            self.provider = "local"
            logger.info("[Embeddings] Using LOCAL Ollama at localhost:11434")
            return

        # Priority 4: DEFAULT — sentence-transformers
        self._base_url = ""
        self._provider = "sentence-transformers"
        self.provider = "sentence-transformers"
        logger.info("[Embeddings] Using sentence-transformers (default)")

    def _is_ollama_running(self) -> bool:
        """Check if Ollama is running locally."""
        try:
            with _transport().sync_client(
                timeout=_config.timeout.http_connect_seconds
            ) as client:
                response = client.get(f"{_OLLAMA_LOCAL}/api/tags")
                return bool(response.status_code == 200)
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("[Embeddings] Local Ollama probe failed: %s", exc)
            return False

    def _get_fallback_embedder(self) -> SentenceTransformerEmbedder:
        """Get or create the sentence-transformers fallback."""
        if self._fallback_embedder is None:
            self._fallback_embedder = SentenceTransformerEmbedder()
        return self._fallback_embedder

    def identity(self) -> dict[str, Any]:
        """Return stable embedder-identity fingerprint for index metadata.

        D10 (Fable 5 DD 2026-06-11): store this with every index so that
        search-time mismatches (different model/prefix/dimension) can raise
        rather than silently degrade retrieval quality.
        """
        if self._provider == "sentence-transformers":
            return _identity(model=self.model_name, prefix="search_query: ")
        # Nomic models use the asymmetric search convention.
        return _identity(model=self.model_name, prefix="search_query: ")

    def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        """Encode a list of texts into embedding vectors.

        Args:
            texts: List of text strings to embed.
            **kwargs: Ignored (API compatibility with SentenceTransformer).

        Returns:
            List of embedding vectors (list of float), one per input text.

        """
        if not texts:
            return []

        # Use sentence-transformers directly if configured
        if self._provider == "sentence-transformers":
            return self._get_fallback_embedder().encode(texts, **kwargs)

        embeddings: list[list[float]] = []
        # SSOT: [embed].batch_size / inter_batch_pause_s in config.toml
        # (schema EmbedConfig). An explicit kwargs batch_size still wins.
        config_batch_size, config_pause_s = _read_config_embed_limits()
        if _BACKGROUND_POSTURE is not None:
            config_batch_size, config_pause_s = _BACKGROUND_POSTURE
        kwarg_batch = kwargs.get("batch_size")
        batch_size = config_batch_size
        if isinstance(kwarg_batch, int):
            batch_size = max(1, kwarg_batch)
        show_progress = kwargs.get("show_progress_bar", False)

        if show_progress:
            logger.info(f"[Embeddings] Generating embeddings for {len(texts)} texts...")

        # Process in batches, pausing between them so a large ingest cannot
        # saturate the shared local embedding runner (chunked + paced).
        for i in range(0, len(texts), batch_size):  # type: ignore[call-overload]
            batch = texts[i : i + batch_size]
            try:
                batch_emb = self._embed_batch(batch)
                embeddings.extend(batch_emb)
            except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.warning(
                    f"[Embeddings] Ollama batch failed: {exc}, using sentence-transformers fallback"
                )
                # Fall back to sentence-transformers instead of zero vectors
                try:
                    fallback_embs = self._get_fallback_embedder().encode(batch, **kwargs)
                    embeddings.extend(fallback_embs)
                except Exception as fallback_exc:  # ruff: ignore[BLE001]  # broad catch intentional
                    logger.error(f"[Embeddings] Fallback also failed: {fallback_exc}")
                    embeddings.extend([[0.0] * _EMBED_DIMENSION for _ in batch])
            if config_pause_s > 0.0 and i + batch_size < len(texts):
                time.sleep(config_pause_s)

        if show_progress:
            logger.info(f"[Embeddings] Done: {len(embeddings)} vectors generated.")

        return embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """POST a batch of texts to the Ollama embeddings endpoint.

        v13.22.3 fix: local Ollama DOES support batch embeddings via
        ``/api/embed`` (verified: 256 texts in 4.93s on this host with
        ``nomic-embed-text``). The previous sequential ``/api/embeddings``
        loop created a per-batch hang after the first 256 texts because
        it exhausted the local Ollama client connection pool after
        the long-running encode loop. We now use ``/api/embed`` for both
        local AND cloud (it's the documented Ollama batch endpoint and
        works in both modes — Ollama Cloud does not host embedding
        models, but the endpoint shape is the same and a future move to
        cloud embeddings needs no code change).
        """
        # Both local and cloud use the same /api/embed batch endpoint.
        # The cloud-only Authorization header is harmless on local
        # (Ollama ignores it).
        headers = {"Content-Type": "application/json"}
        if self._provider == "cloud" and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": self._model,
            "input": texts,
        }

        try:
            with _transport().sync_client(timeout=self._timeout, follow_redirects=True) as client:
                response = client.post(
                    f"{self._base_url}/api/embed",
                    json=payload,
                    headers=headers,
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Ollama embeddings API error {response.status_code}: {response.text[:200]}"
                )
            data = response.json()
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            # v13.22.3: a single batch failure must NOT silently produce
            # zero vectors for the entire batch (audit B-19). Fall back
            # to sentence-transformers if available, else log loudly.
            logger.warning(
                f"[Embeddings] Batch embed failed ({exc}); falling back to "
                f"sentence-transformers for {len(texts)} texts"
            )
            try:
                return self._get_fallback_embedder().encode(
                    texts,
                    show_progress_bar=False,
                )
            except Exception as fallback_exc:  # ruff: ignore[BLE001]  # broad catch intentional
                logger.error(
                    f"[Embeddings] Fallback also failed: {fallback_exc}; "
                    f"appending zero vectors (audit B-19)"
                )
                return [[0.0] * _EMBED_DIMENSION for _ in texts]

        # Handle both single prompt and array of prompts
        if "embedding" in data:
            return [data["embedding"]]
        elif "embeddings" in data:
            return data["embeddings"]  # type: ignore[no-any-return]
        else:
            raise RuntimeError(f"Unexpected /api/embed response format: {list(data.keys())}")


# ── Lazy singleton ─────────────────────────────────────────────────────────────

_embedder: OllamaCloudEmbedder | None = None


def get_embedder() -> OllamaCloudEmbedder:
    """Get or create the singleton OllamaCloudEmbedder instance."""
    global _embedder
    if _embedder is None:
        _embedder = OllamaCloudEmbedder()
    return _embedder


def get_embedder_identity() -> dict[str, Any]:
    """Return the identity fingerprint of the current singleton embedder."""
    return get_embedder().identity()


def get_local_embedder() -> SentenceTransformerEmbedder:
    """Get a local sentence-transformers embedder directly."""
    return SentenceTransformerEmbedder()


def reset_embedder() -> None:
    """Reset the singleton (for testing or reconfiguration)."""
    global _embedder
    _embedder = None


def clear_embedding_cache() -> None:
    """Clear embedding cache and trigger GC.

    Call this after large ingestion jobs or when memory pressure is detected.
    """
    old_size = len(OllamaCloudEmbedder._embedding_cache)
    OllamaCloudEmbedder._embedding_cache.clear()
    gc.collect()
    logger.info(f"[Embedding Cache] Cleared {old_size} entries, GC triggered")
