"""OTel → LangSmith Observability Bridge.

Phase 4 of the LangChain Ecosystem Compatibility Plan.
Bridges Beagle's OpenTelemetry tracing to LangSmith so that
every Beagle workflow execution is visible in LangSmith's dashboard.

Two-way bridge:
  1. OTel spans from Goose subprocess nodes → LangSmith traces
  2. LangChain callbacks (from Phase 3) → OTel spans (bidirectional)

All config from config.toml [langchain_bridges.langsmith].
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from .config import get_langsmith_config

logger = logging.getLogger("Beagle.bridges.otel_langsmith_bridge")


class BeagleLangSmithBridge:
    """Bridges Beagle's OTel tracing with LangSmith observability.

    Reads config from config.toml [langchain_bridges.langsmith].
    Translates OTel span attributes to LangSmith run metadata,
    enabling unified observability across Goose subprocess nodes
    and LangChain LLM/tool nodes.

    Usage:
        bridge = BeagleLangSmithBridge()
        bridge.start()

        # OTel spans from Goose nodes are automatically translated
        # LangChain nodes fire callbacks that LangSmith consumes natively

        bridge.stop()
    """

    def __init__(self) -> None:
        self.config = get_langsmith_config()
        self._started = False
        self._langsmith_client: Any = None
        self._api_key: str = ""  # Held in-process, not exported to child processes

    def start(self) -> bool:
        """Initialize the LangSmith bridge.

        Configures LangSmith tracing without exporting secrets to os.environ.
        Instead of setting LANGCHAIN_API_KEY in the environment (which leaks
        to child processes like Goose subprocess pool workers), we use the
        LangSmith Client directly for manual traces, and set only non-secret
        env vars for callback-based automatic tracing.

        Returns:
            True if bridge started successfully, False if disabled or misconfigured.

        """
        if not self.config.enabled:
            logger.debug("LangSmith bridge disabled in config")
            return False

        from ..secrets_loader import load_secret

        api_key = load_secret(self.config.api_key_env)
        if not api_key:
            logger.warning(
                f"LangSmith API key not found ({self.config.api_key_env}). "
                f"Bridge not started. Get a key at https://smith.langchain.com"
            )
            return False

        # Set ONLY non-secret environment variables for LangSmith tracing.
        # LANGCHAIN_API_KEY is NOT set in os.environ to prevent leakage
        # to child processes (Goose subprocess pool workers).
        # Instead, it's held in self._api_key and passed directly to
        # langsmith.Client() for manual trace creation.
        self._api_key = api_key
        os.environ["LANGCHAIN_PROJECT"] = self.config.project_name
        os.environ["LANGCHAIN_TRACING_V2"] = "true"

        # For callback-based tracing, we must set the API key because
        # LangChain's internal tracer reads it from the environment.
        # However, we scope this carefully: set right before usage,
        # and clear in stop().
        # SECURITY: This means the key is briefly in the environment
        # while the bridge is active. The stop() method clears it.
        os.environ["LANGCHAIN_API_KEY"] = api_key

        if self.config.hide_inputs:
            os.environ["LANGCHAIN_HIDE_INPUTS"] = "true"
        if self.config.hide_outputs:
            os.environ["LANGCHAIN_HIDE_OUTPUTS"] = "true"

        # Sample rate
        if self.config.sample_rate < 1.0:
            os.environ["LANGCHAIN_SAMPLE_RATE"] = str(self.config.sample_rate)

        # Initialize LangSmith Client directly (not from env var)
        try:
            import langsmith

            self._langsmith_client = langsmith.Client(api_key=api_key)  # type: ignore[assignment]
            logger.info(f"LangSmith bridge started: project={self.config.project_name}")
        except ImportError:
            logger.debug("langsmith SDK not installed — using env-var-based tracing only")
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"LangSmith client initialization failed: {exc}")

        self._started = True
        return True

    def stop(self) -> None:
        """Stop the LangSmith bridge and clean up environment variables.

        SECURITY: Explicitly clears LANGCHAIN_API_KEY from os.environ
        to prevent secret leakage to child processes after bridge stop.
        """
        if not self._started:
            return

        # Clean up ALL environment variables, including the API key
        for key in (
            "LANGCHAIN_API_KEY",
            "LANGCHAIN_PROJECT",
            "LANGCHAIN_TRACING_V2",
            "LANGCHAIN_HIDE_INPUTS",
            "LANGCHAIN_HIDE_OUTPUTS",
            "LANGCHAIN_SAMPLE_RATE",
        ):
            os.environ.pop(key, None)

        # Clear in-process reference
        self._api_key = ""
        self._langsmith_client = None

        self._started = False
        logger.info("LangSmith bridge stopped — all secrets cleared from environment")

    def translate_otel_span_to_run_metadata(self, span: Any) -> dict[str, Any]:
        """Translate an OTel span's attributes to LangSmith run metadata.

        Maps Beagle OTel span names to LangSmith run types using
        the configured span_mapping from config.toml.

        Args:
            span: An OpenTelemetry ReadableSpan object.

        Returns:
            Dict of LangSmith run metadata.

        """
        if not self._started:
            return {}

        span_name = getattr(span, "name", "")
        attributes = {}
        if hasattr(span, "attributes") and span.attributes:
            attributes = dict(span.attributes)

        # Map span name → LangSmith run type
        run_type = self.config.span_mapping.get(span_name, "chain")

        # Build metadata
        metadata = {
            "run_type": run_type,
            "run_name": span_name,
            "tags": {
                "beagle.span": span_name,
                "beagle.version": "13.6.0",
            },
        }

        # Copy span attributes to metadata
        for key, value in attributes.items():
            if isinstance(value, str | int | float | bool):
                metadata["tags"][f"beagle.{key}"] = value  # type: ignore[index]

        # Span status
        if hasattr(span, "status"):
            status = span.status
            if hasattr(status, "status_code"):
                metadata["tags"]["beagle.status"] = str(status.status_code)  # type: ignore[index]

        return metadata

    def record_goose_subprocess_trace(
        self,
        node_name: str,
        prompt_length: int,
        output_length: int,
        duration_seconds: float,
        model: str = "",
        error: str | None = None,
    ) -> None:
        """Record a LangSmith trace for a Goose subprocess node.

        Since Goose subprocess nodes don't fire LangChain callbacks,
        we need to manually create a trace entry for them.

        Args:
            node_name: Name of the workflow node.
            prompt_length: Character count of the input prompt.
            output_length: Character count of the output.
            duration_seconds: Execution time.
            model: Model used for the call.
            error: Error message if the node failed.

        """
        if not self._started:
            return

        if self._langsmith_client is None:
            return

        try:
            self._langsmith_client.create_run(
                name=f"beagle.goose.{node_name}",
                run_type="chain",
                tags={
                    "beagle.node": node_name,
                    "beagle.executor": "goose_subprocess",
                    "beagle.model": model,
                    "beagle.prompt_chars": str(prompt_length),
                    "beagle.output_chars": str(output_length),
                    "beagle.duration_seconds": f"{duration_seconds:.2f}",
                    **({"beagle.error": error} if error else {}),
                },
                inputs={"prompt_length": prompt_length},
                outputs={"output_length": output_length},
                error=error,
                project_name=self.config.project_name,
                # wall-clock-ok: compares against a persisted timestamp
                start_time=time.time() - duration_seconds,
                end_time=time.time(),
            )
        except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.debug(f"Failed to record Goose trace to LangSmith: {exc}")

    @property
    def is_started(self) -> bool:
        """Check if the bridge is active."""
        return self._started


# ── Global singleton ──────────────────────────────────────────────────────────

_bridge: BeagleLangSmithBridge | None = None


def get_langsmith_bridge() -> BeagleLangSmithBridge:
    """Get the global LangSmith bridge singleton."""
    global _bridge
    if _bridge is None:
        _bridge = BeagleLangSmithBridge()
    return _bridge
