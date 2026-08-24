"""OllamaCloudChatModel — In-process LLM via Ollama Cloud API.

Phase 3 of the LangChain Ecosystem Compatibility Plan.
Subclasses ChatOpenAI from langchain-openai with base_url
pointed at Ollama Cloud's OpenAI-compatible endpoint.

This replaces the Goose subprocess execution pattern for
LangChain-compatible nodes, unlocking:
  - LangSmith tracing (callbacks fire on every LLM call)
  - Structured output (with_structured_output())
  - Streaming (token-by-token delivery)
  - Tool/function calling (model-dependent)

All config from config.toml — no new config files.
API key from secrets_loader (never hardcoded).
Model resolution via model_resolver.py hierarchy.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..secrets_loader import load_secret
from .config import get_chat_model_config

logger = logging.getLogger("Beagle.bridges.chat_model")


def _get_ollama_cloud_base_url() -> str:
    """Resolve the Ollama Cloud API base URL from config.toml."""
    try:
        from ..config.config import get_config

        config = get_config()
        if hasattr(config, "_raw"):
            url = config._raw.get("llm", {}).get("ollama_cloud_endpoint", "")
            if url:
                return url  # type: ignore[no-any-return]
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Could not read ollama_cloud_endpoint from config: {exc}")

    # Try environment variable
    url = os.environ.get("OLLAMA_CLOUD_BASE_URL", "")
    if url:
        return url

    # Default Ollama Cloud endpoint
    return "https://api.ollama.com/v1"


def create_chat_model(
    model_name: str | None = None,
    temperature: float | None = None,
    **kwargs: Any,
) -> Any:
    """Create an OllamaCloudChatModel instance.

    Uses ChatOpenAI from langchain-openai as the base class,
    configured with Ollama Cloud's OpenAI-compatible endpoint.

    Args:
        model_name: Model to use. If None, resolved via model_resolver.
        temperature: Temperature override. If None, read from config.
        **kwargs: Additional kwargs passed to ChatOpenAI constructor.

    Returns:
        A ChatOpenAI instance configured for Ollama Cloud.

    Raises:
        ImportError: If langchain-openai is not installed.
        RuntimeError: If OLLAMA_CLOUD_API_KEY is not available.

    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "langchain-openai is required for OllamaCloudChatModel. "
            "Install with: pip install langchain-openai"
        ) from exc

    cfg = get_chat_model_config()

    # Resolve model via model_resolver hierarchy
    if model_name is None:
        try:
            from ..config.model_resolver import resolve_model

            model_name = resolve_model()
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Model resolver failed ({exc}), falling back to config.toml default")
            try:
                from ..config.config import get_config

                model_name = (
                    get_config()._raw.get("goose", {}).get("default_model", "glm-5.1:cloud")  # type: ignore[attr-defined]
                )
            except ImportError as exc2:
                logger.debug(f"Config fallback also failed ({exc2}), using glm-5.1:cloud")
                model_name = "glm-5.1:cloud"

    # Load API key
    api_key = load_secret("OLLAMA_CLOUD_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OLLAMA_CLOUD_API_KEY not found. Set via environment variable "
            "or ~/.config/goose/secrets.yaml"
        )

    # Resolve base URL
    base_url = _get_ollama_cloud_base_url()

    # Temperature: explicit > agents.toml profile > config default
    if temperature is None:
        try:
            from ..config.config import get_config

            config = get_config()
            temperature = float(config._raw.get("llm", {}).get("temperature", 0.7))  # type: ignore[attr-defined]
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Could not read temperature from config: {exc}")
            temperature = 0.7
    streaming = cfg.stream_responses

    # Max retries
    max_retries = cfg.max_retries

    logger.info(
        f"Creating OllamaCloudChatModel: model={model_name}, "
        f"base_url={base_url}, temperature={temperature}"
    )

    # openai_api_key/openai_api_base are deprecated-but-valid pydantic fields;
    # mypy's stub omits them, so route them through model_kwargs (accepted by
    # the stub) — the underlying OpenAI client reads api_key/base_url from
    # kwargs and the behavior is identical.
    chat_model = ChatOpenAI(
        model=model_name,
        model_kwargs={
            "api_key": api_key,
            "base_url": base_url,
        },
        temperature=temperature,
        streaming=streaming,
        max_retries=max_retries,
        **kwargs,
    )

    return chat_model


class OllamaCloudChatModel:
    """Factory/wrapper for Ollama Cloud ChatOpenAI instances.

    Provides a convenient interface that abstracts away the
    ChatOpenAI constructor details.

    Usage:
        from beagle.bridges.chat_model import OllamaCloudChatModel

        model = OllamaCloudChatModel(model_name="glm-5.1:cloud")
        response = await model.ainvoke("Explain the A2A protocol")

        # With structured output:
        from pydantic import BaseModel
        class Result(BaseModel): findings: list[str]
        structured = model.with_structured_output(Result)
        result = await structured.ainvoke("Review this code")
    """

    def __init__(
        self,
        model_name: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> None:
        """Create a chat model instance for Ollama Cloud."""
        self._model_name = model_name
        self._temperature = temperature
        self._kwargs = kwargs
        self._instance = None

    @property
    def _llm(self) -> Any:
        """Lazy-create the underlying ChatOpenAI instance."""
        if self._instance is None:
            self._instance = create_chat_model(
                model_name=self._model_name,
                temperature=self._temperature,
                **self._kwargs,
            )
        return self._instance

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """Async invoke the chat model."""
        return await self._llm.ainvoke(input, config=config, **kwargs)

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """Sync invoke the chat model."""
        return self._llm.invoke(input, config=config, **kwargs)

    async def astream(self, input: Any, config: Any = None, **kwargs: Any):
        """Async stream tokens from the chat model."""
        async for chunk in self._llm.astream(input, config=config, **kwargs):
            yield chunk

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        """Return a runnable that outputs structured (typed) results."""
        return self._llm.with_structured_output(schema, **kwargs)

    def bind_tools(self, tools: list, **kwargs: Any) -> Any:
        """Bind tools for function calling."""
        return self._llm.bind_tools(tools, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "ollama-cloud"

    def __repr__(self) -> str:
        return f"OllamaCloudChatModel(model={self._model_name})"
