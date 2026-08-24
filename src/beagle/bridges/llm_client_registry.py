"""LLM Client Registry for bridge tool delegation.

Provides singleton DirectLLMClient instances keyed by composite key
(model, host, api_key, timeout), with automatic cost tracking and
tenant awareness.

Bridge code (CrewAI, AutoGen, LLMNode, etc.) should use
get_llm_client() rather than instantiating DirectLLMClient directly.
This ensures connection reuse, shared cost tracking, and consistent
configuration.

Usage:
    from beagle.bridges.llm_client_registry import get_llm_client

    client = get_llm_client("glm-5.1:cloud")
    resp = await client.generate("Hello world")
    # cost automatically tracked via record_usage()
"""

from __future__ import annotations

import logging
import threading

from ..config.models import get_context_window, get_max_output_tokens
from .llm_direct import DirectLLMClient, LLMResponse

logger = logging.getLogger("Beagle.bridges.llm_client_registry")


def _default_model() -> str:
    """Resolve the default model from the canonical config preset.

    v1.1.1 (S9): the previous ``"glm-5.1:cloud"`` literal drifted from the
    allowlisted fleet. The SSOT is the config ``[model_presets]`` default.
    """
    try:
        from ..config.model_resolver import get_preset

        return get_preset("default")
    except (ImportError, KeyError, ValueError, RuntimeError):  # pragma: no cover
        return "gemma4:31b"


# Default maximum number of cached clients before refusing new registrations.
_MAX_CLIENTS = 64


class LLMClientRegistry:
    """Thread-safe singleton registry for DirectLLMClient instances.

    Clients are keyed by composite (model, host, api_key, timeout) so
    different timeout/host combinations for the same model don't collide.

    Usage:
        registry = LLMClientRegistry()
        client = registry.get("glm-5.1:cloud")
        resp = await client.generate("Hello")
    """

    _instance: LLMClientRegistry | None = None
    _lock = threading.Lock()

    # Instance attributes — declared at class scope so mypy sees them; the
    # singleton __new__ populates them atomically.
    _clients: dict[tuple[str, str, str, float], DirectLLMClient]
    _client_lock: threading.Lock
    _max_clients: int

    def __new__(cls) -> LLMClientRegistry:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    # All init happens here, atomically.  __init__ is a no-op
                    # so a racing thread can't see a partially-built dict.
                    cls._instance._clients = {}
                    cls._instance._client_lock = threading.Lock()
                    cls._instance._max_clients = _MAX_CLIENTS
        return cls._instance

    def __init__(self) -> None:
        # No-op — all real initialisation happens inside __new__.
        pass

    # ── Internal key builder ─────────────────────────────────────────────

    @staticmethod
    def _make_key(
        model: str,
        host: str,
        api_key: str,
        timeout: float,
    ) -> tuple[str, str, str, float]:
        """Normalise to a deterministic cache key."""
        return (model or _default_model(), host, api_key, timeout)

    @staticmethod
    def _resolve_max_tokens(model: str, max_tokens: int | None) -> int:
        """Resolve the effective max_tokens for a model call.

        An explicit caller-supplied value wins, but is clamped to the
        model's context window so a caller cannot request an output
        budget that exceeds the model's context (resource-exhaustion
        guard). When the caller omits it (None), fall back to the model's
        declared budget from config/models.py (get_max_output_tokens),
        which itself falls back to 8000 for unknown models and clamps to
        the context window.

        Args:
            model: Model name string.
            max_tokens: Caller-supplied value, or None to use the model default.

        Returns:
            Effective max_tokens for the request.

        """
        if max_tokens is not None:
            return min(max_tokens, get_context_window(model))
        return get_max_output_tokens(model)

    # ── Public API ───────────────────────────────────────────────────────

    def get(
        self,
        model: str = "",
        host: str = "",
        api_key: str = "",
        timeout: float = 30.0,
    ) -> DirectLLMClient:
        """Get or create a DirectLLMClient keyed by (model, host, api_key, timeout).

        Args:
            model: Model name (e.g., "glm-5.1:cloud"). Defaults to env/config.
            host: API host URL. Defaults to Ollama Cloud.
            api_key: API key. Defaults to OLLAMA_CLOUD_API_KEY env var.
            timeout: Request timeout in seconds.

        Returns:
            A DirectLLMClient instance (singleton per composite key).

        """
        key = self._make_key(model, host, api_key, timeout)
        if key in self._clients:
            return self._clients[key]

        with self._client_lock:
            if key in self._clients:
                return self._clients[key]

            if len(self._clients) >= self._max_clients:
                logger.error(
                    f"LLMClientRegistry: refused new client ({len(self._clients)} "
                    f"already cached, cap={self._max_clients}) — failing loud"
                )
                raise RuntimeError(
                    f"LLMClientRegistry cache full ({self._max_clients} clients max)"
                )

            client = DirectLLMClient(
                model=key[0],
                host=key[1],
                api_key=key[2],
                timeout=key[3],
            )
            self._clients[key] = client
            logger.info(
                f"LLMClientRegistry: created client for '{key[0]}' "
                f"(host={key[1] or 'default'}, timeout={key[3]})"
            )
            return client

    def get_or_default(self, model: str = "") -> DirectLLMClient:
        """Get client for model, falling back to default if model is empty."""
        return self.get(model=model or _default_model())

    def list_clients(self) -> list[str]:
        """List all registered model names (deduplicated)."""
        with self._client_lock:
            return sorted({k[0] for k in self._clients})

    async def close_all(self) -> None:
        """Close and clear all cached clients (call this in test cleanup)."""
        import contextlib

        with self._client_lock:
            for client in self._clients.values():
                with contextlib.suppress(Exception):
                    await client.close()
            self._clients.clear()

    async def aclose(self) -> None:
        """Alias for close_all."""
        await self.close_all()

    # ── Convenience: tracked generation ──────────────────────────────────

    async def _check_tenant_budget(self, tenant_id: str) -> None:
        """Check tenant budget before an LLM call; raise if exceeded."""
        try:
            from ..cost_tracker import (
                TenantBudgetExceeded,
                TenantBudgetTracker,
                get_tenant_budget_tracker,
            )
        except ImportError:  # pragma: no cover — test-only paths
            return

        tracker: TenantBudgetTracker = get_tenant_budget_tracker()
        if not tracker.check_budget(tenant_id):
            spent = tracker._spent.get(tenant_id, 0.0)
            budget = tracker._budgets.get(tenant_id, 0.0)
            raise TenantBudgetExceeded(tenant_id, budget, spent)

    async def _record_cost(
        self,
        resp: LLMResponse,
        tenant_id: str | None = None,
    ) -> None:
        try:
            from ..cost_tracker import get_cost_tracker

            tracker = get_cost_tracker()
            await tracker.record_usage(
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                model=resp.model,
                tenant_id=tenant_id,
            )
        except Exception:  # broad catch intentional
            logger.warning(
                "Cost tracking unavailable for bridge LLM call",
                exc_info=True,
            )

    async def tracked_generate(
        self,
        prompt: str,
        system: str = "",
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tenant_id: str | None = None,
    ) -> LLMResponse:
        """Generate with automatic cost tracking via context-aware tracker.

        Args:
            prompt: User message.
            system: System directive.
            model: Model name.
            temperature: Sampling temperature.
            max_tokens: Max output tokens. When omitted, the model's
                declared budget from config/models.py is used.
            tenant_id: Optional tenant for per-tenant budget tracking.

        Returns:
            LLMResponse with content, tokens, cost, latency.

        Raises:
            TenantBudgetExceeded: If the tenant is over budget before the call.

        """
        if tenant_id:
            await self._check_tenant_budget(tenant_id)

        client = self.get(model)
        resp = await client.generate(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=self._resolve_max_tokens(model, max_tokens),
        )

        await self._record_cost(resp, tenant_id=tenant_id)
        return resp

    async def tracked_chat(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tenant_id: str | None = None,
    ) -> LLMResponse:
        """Chat with automatic cost tracking.

        Args:
            messages: Chat messages (role/content dicts).
            model: Model name.
            temperature: Sampling temperature.
            max_tokens: Max output tokens. When omitted, the model's
                declared budget from config/models.py is used.
            tenant_id: Optional tenant for per-tenant budget tracking.

        Returns:
            LLMResponse with content, tokens, cost, latency.

        Raises:
            TenantBudgetExceeded: If the tenant is over budget before the call.

        """
        if tenant_id:
            await self._check_tenant_budget(tenant_id)

        client = self.get(model)
        resp = await client.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=self._resolve_max_tokens(model, max_tokens),
        )

        await self._record_cost(resp, tenant_id=tenant_id)
        return resp


# ── Module-level convenience functions ────────────────────────────────────────


def get_llm_client(
    model: str = "",
    host: str = "",
    api_key: str = "",
    timeout: float = 30.0,
) -> DirectLLMClient:
    """Get a DirectLLMClient from the global registry.

    Args:
        model: Model name. Defaults to "glm-5.1:cloud".
        host: API host URL.
        api_key: API key.
        timeout: Request timeout.

    Returns:
        A DirectLLMClient instance (singleton per composite key).

    """
    return LLMClientRegistry().get(
        model=model or _default_model(),
        host=host,
        api_key=api_key,
        timeout=timeout,
    )


async def reset_llm_client_registry() -> None:
    """Close all clients and reset the global registry (useful for testing)."""
    await LLMClientRegistry().close_all()


async def tracked_llm_call(
    prompt: str,
    system: str = "",
    model: str = "",
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tenant_id: str | None = None,
) -> LLMResponse:
    """Single convenience call: generate + track cost.

    Args:
        prompt: User message.
        system: System directive.
        model: Model name.
        temperature: Sampling temperature.
        max_tokens: Max output tokens. When omitted, the model's
            declared budget from config/models.py is used.
        tenant_id: Optional tenant for per-tenant budget tracking.

    Returns:
        LLMResponse with content, tokens, cost, latency.

    Raises:
        TenantBudgetExceeded: If the tenant is over budget before the call.

    """
    return await LLMClientRegistry().tracked_generate(
        prompt=prompt,
        system=system,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        tenant_id=tenant_id,
    )


__all__ = [
    "LLMClientRegistry",
    "get_llm_client",
    "reset_llm_client_registry",
    "tracked_llm_call",
]
