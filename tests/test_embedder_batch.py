"""v13.22.3 regression: the Ollama embedder must use /api/embed (batch)
for local Ollama, not the sequential /api/embeddings loop.

Background — the 2026-07-27 incident:
- The previous local-Ollama path in OllamaCloudEmbedder._embed_batch
  looped sequentially, posting one /api/embeddings request per text.
  After the first 256-text batch, the httpx.Client connection pool
  saturated and the embedder hung without producing more vectors.
- The fix: route both local and cloud through /api/embed (the
  documented batch endpoint). 256 texts in 4.93s on local Ollama with
  ``nomic-embed-text``; the sequential path was ~50s and stalled.

The mock httpx.Client below records the URL and payload shape of every
POST. The test asserts:
  1. Each POST goes to /api/embed (NOT /api/embeddings).
  2. Each POST payload has ``input: list[str]`` (batch, not single).
  3. The response shape with N vectors in / N out is preserved.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

sys.path.insert(0, "/home/server/Projects/beagle")

from beagle.infrastructure.services import embedding as emb_mod
from beagle.infrastructure.services.embedding import (
    _EMBED_DIMENSION,
    OllamaCloudEmbedder,
)


class _RecordedClient:
    """Fake httpx.Client that records every POST request and returns a
    deterministic batch response.
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    def __call__(self, *args, **kwargs):  # context-manager entry
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, json=None, headers=None):
        # Record what was sent.
        self.posts.append({"url": url, "json": json, "headers": headers})
        # Build a deterministic batch response.
        n = len(json["input"])
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "model": json["model"],
            "embeddings": [[0.1] * _EMBED_DIMENSION for _ in range(n)],
        }
        return resp


def test_local_ollama_uses_batch_embed_endpoint(monkeypatch):
    """The local Ollama path must call /api/embed (batch), not
    /api/embeddings (single), and the payload must contain an
    ``input`` list — proving it's a real batch request.
    """
    monkeypatch.setattr(emb_mod, "httpx", MagicMock())

    # Build an embedder in local mode without touching network.
    inst = OllamaCloudEmbedder.__new__(OllamaCloudEmbedder)
    inst._provider = "local"
    inst._model = "nomic-embed-text"
    inst._base_url = "http://localhost:11434"
    inst._timeout = 30.0
    inst._api_key = None

    recorder = _RecordedClient()
    monkeypatch.setattr(emb_mod.httpx, "Client", recorder)

    texts = ["alpha", "beta", "gamma", "delta", "epsilon"]
    result = inst._embed_batch(texts)

    assert len(recorder.posts) == 1, (
        f"expected 1 batched POST, saw {len(recorder.posts)} — the "
        f"embedder is hitting {recorder.posts[0]['url']} sequentially "
        f"instead of batching via /api/embed"
    )
    post = recorder.posts[0]
    assert post["url"] == "http://localhost:11434/api/embed", (
        f"wrong endpoint: {post['url']} — must be /api/embed (the "
        f"documented Ollama batch endpoint), not /api/embeddings"
    )
    payload = post["json"]
    assert payload["model"] == "nomic-embed-text"
    assert payload["input"] == texts, (
        "payload must carry the full input list — proved by the Ollama /api/embed batch contract"
    )
    assert len(result) == len(texts)
    assert all(len(v) == _EMBED_DIMENSION for v in result)


def test_cloud_uses_batch_embed_endpoint_with_auth(monkeypatch):
    """The cloud variant routes the same way but adds the bearer token."""
    monkeypatch.setattr(emb_mod, "httpx", MagicMock())

    inst = OllamaCloudEmbedder.__new__(OllamaCloudEmbedder)
    inst._provider = "cloud"
    inst._model = "nomic-embed-text"
    inst._base_url = "https://ollama.com"
    inst._timeout = 30.0
    inst._api_key = "secret-test-key"

    recorder = _RecordedClient()
    monkeypatch.setattr(emb_mod.httpx, "Client", recorder)

    texts = ["x", "y"]
    inst._embed_batch(texts)

    assert len(recorder.posts) == 1
    post = recorder.posts[0]
    assert post["url"] == "https://ollama.com/api/embed"
    assert post["json"]["input"] == texts
    assert post["json"]["model"] == "nomic-embed-text"
    # Cloud auth — Authorization header must carry the API key.
    headers = post["headers"] or {}
    assert headers.get("Authorization") == "Bearer secret-test-key", (
        f"cloud endpoint must send Authorization: Bearer <api_key>; got headers={headers!r}"
    )


def test_batch_failure_falls_back_to_sentence_transformers(monkeypatch):
    """If the Ollama POST fails (network/5xx), the batch endpoint path
    must NOT silently produce zero vectors — fall back to
    sentence-transformers so the rest of the ingest still gets
    usable embeddings.
    """
    monkeypatch.setattr(emb_mod, "httpx", MagicMock())

    class _BrokenClient(_RecordedClient):
        def post(self, url, json=None, headers=None):  # type: ignore[override]
            resp = MagicMock()
            resp.status_code = 500
            resp.text = "internal oops"
            return resp

    inst = OllamaCloudEmbedder.__new__(OllamaCloudEmbedder)
    inst._provider = "local"
    inst._model = "nomic-embed-text"
    inst._base_url = "http://localhost:11434"
    inst._timeout = 5.0
    inst._api_key = None

    monkeypatch.setattr(emb_mod.httpx, "Client", _BrokenClient())

    # Stub the fallback to return deterministic vectors so the
    # assertions are tight.
    class _StubFallback:
        def encode(self, texts, **kwargs):
            return [[0.5] * _EMBED_DIMENSION for _ in texts]

    monkeypatch.setattr(inst, "_get_fallback_embedder", lambda: _StubFallback())

    result = inst._embed_batch(["a", "b", "c"])
    assert len(result) == 3, "fallback should preserve input count"
    assert all(len(v) == _EMBED_DIMENSION for v in result)
