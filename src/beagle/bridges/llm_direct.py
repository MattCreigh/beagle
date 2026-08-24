"""DirectLLMClient — bypass Goose subprocess overhead via httpx.

Provides direct HTTP calls to Ollama Cloud's OpenAI-compatible API
with the same cost tracking, firewall, and rate limiting as the
subprocess path.

Ollama Cloud exposes OpenAI-compatible endpoints at /v1/completions
and /v1/chat/completions.  Request and response schemas follow the
OpenAI format — NOT the native Ollama /api/generate or /api/chat.
This is the canonical bridge contract; all LLM bridges (CrewAI,
AutoGen, LLMNode, LLMClientRegistry) route through this client.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("Beagle.bridges.llm_direct")


@dataclass
class LLMResponse:
    """Standardized response from any LLM call."""

    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0
    latency_seconds: float = 0.0
    finish_reason: str = "stop"


class DirectLLMClient:
    """Direct httpx client for Ollama Cloud (OpenAI-compatible API).

    Bypasses ~3-5s Goose subprocess overhead for bridge contexts.
    Same cost tracking, semantic firewall, rate limiting.

    API contract: Ollama Cloud is OpenAI-compatible.
      - Completions:  POST /v1/completions
      - Chat:         POST /v1/chat/completions
      - Auth:         Bearer token via OLLAMA_CLOUD_API_KEY
      - Response schema follows OpenAI format with "choices"
        and "usage" keys.

    Usage:
        client = DirectLLMClient(model="glm-5.1:cloud")
        resp = await client.generate("Hello world")
    """

    # ── Configuration ─────────────────────────────────────────────────────

    # Default host for Ollama Cloud OpenAI-compatible API.
    # No default provider: set host to any OpenAI-compatible base URL.
    # Examples (see README): https://ollama.com  |  http://localhost:11434
    # | your vLLM/LiteLLM/OpenAI-compatible endpoint.
    DEFAULT_HOST = ""

    # Pricing: $0.0015 per 1K tokens (input + output).
    COST_PER_1K_TOKENS = 0.0015

    def __init__(
        self,
        model: str = "",
        host: str = "",
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.model = model  # required — no model presets ship with beagle
        self.host = host or self.DEFAULT_HOST
        self.api_key = api_key
        self.timeout = timeout
        self._http: Any | None = None

    # ── HTTP client ──────────────────────────────────────────────────────

    def _get_http(self) -> Any:
        if self._http is None:
            from beagle.core.transports import active

            self._http = active().async_client(
                base_url=self.host,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._http

    async def _resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        from beagle.secrets_loader import load_secret

        return load_secret("OLLAMA_CLOUD_API_KEY")

    # ── Generation (OpenAI-compatible /v1/completions) ─────────────────────

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.7,
        max_tokens: int = 8000,
    ) -> LLMResponse:
        """Generate completion via OpenAI-compatible /v1/completions.

        Args:
            prompt: User message.
            system: System directive (prepended to prompt).
            temperature: Sampling temperature.
            max_tokens: Max output tokens (default 8000; reasoning models
                        consume ~100-200 tokens for chain-of-thought before
                        producing content).

        Returns:
            LLMResponse with content, tokens, cost, latency.

        """
        start = time.monotonic()
        api_key = await self._resolve_api_key()
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        if system:
            prompt = f"{system}\n\n{prompt}"

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            http = self._get_http()
            response = await http.post(
                "/v1/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(f"API error for model {self.model}: {data['error']}")

            # OpenAI-format response:
            #   {"choices": [{"text": "...", "finish_reason": "stop"}],
            #    "usage": {"prompt_tokens": N, "completion_tokens": N}}
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"No choices in response for model {self.model}")
            content = choices[0].get("text", "")
            finish_reason = choices[0].get("finish_reason", "stop")
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            latency = time.monotonic() - start
            cost = self._estimate_cost(prompt_tokens, completion_tokens)
            return LLMResponse(
                content=content,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                model=self.model,
                cost_usd=cost,
                latency_seconds=latency,
                finish_reason=finish_reason,
            )
        except Exception:  # broad catch intentional
            logger.exception(f"DirectLLMClient generate failed for {self.model}")
            raise

    # ── Chat (OpenAI-compatible /v1/chat/completions) ──────────────────────

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 8000,
    ) -> LLMResponse:
        """Generate chat completion via OpenAI-compatible /v1/chat/completions.

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.
            temperature: Sampling temperature.
            max_tokens: Max output tokens (default 8000; reasoning models
                        consume ~100-200 tokens for chain-of-thought before
                        producing content).

        Returns:
            LLMResponse with content, tokens, cost, latency.

        """
        start = time.monotonic()
        api_key = await self._resolve_api_key()
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            http = self._get_http()
            response = await http.post(
                "/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(f"API error for model {self.model}: {data['error']}")

            # OpenAI-format response:
            #   {"choices": [{"message": {"role": "assistant", "content": "..."},
            #                  "finish_reason": "stop"}],
            #    "usage": {"prompt_tokens": N, "completion_tokens": N}}
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"No choices in chat response for model {self.model}")
            msg = choices[0].get("message", {})
            content = msg.get("content", "")
            finish_reason = choices[0].get("finish_reason", "stop")
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            latency = time.monotonic() - start
            cost = self._estimate_cost(prompt_tokens, completion_tokens)
            return LLMResponse(
                content=content,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                model=self.model,
                cost_usd=cost,
                latency_seconds=latency,
                finish_reason=finish_reason,
            )
        except Exception:  # broad catch intentional
            logger.exception(f"DirectLLMClient chat failed for {self.model}")
            raise

    # ── Cost estimation ───────────────────────────────────────────────────

    @staticmethod
    def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
        """Estimate cost in USD. Ollama Cloud: $0.0015/1K tokens."""
        return (input_tokens + output_tokens) * DirectLLMClient.COST_PER_1K_TOKENS / 1000

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
