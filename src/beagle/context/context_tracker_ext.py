"""Context Tracker Extension for Goose.

Provides accurate context window tracking by integrating with goose's session system.
This extension hooks into LLM requests and responses to track actual token usage.

To enable: Add to your goose config.yaml:
  extensions:
    context_tracker:
      enabled: true
      type: platform
      name: context_tracker
      description: Accurate context window tracking with real token counts
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _default_model() -> str:
    """Resolve the default model from the canonical config preset.

    v1.1.1 (S9): the previous ``"glm-5.1:cloud"`` literal drifted from the
    allowlisted fleet. The SSOT is config.toml ``[model_presets]``.
    """
    try:
        from ..config.model_resolver import get_preset

        return get_preset("default")
    except (ImportError, KeyError, ValueError, RuntimeError):  # pragma: no cover
        return "gemma4:31b"


# Token estimation
try:
    import tiktoken

    _tokenizer = tiktoken.get_encoding("cl100k_base")

    def estimate_tokens(text: str) -> int:
        return len(_tokenizer.encode(text))
except ImportError:

    def estimate_tokens(text: str) -> int:
        words = text.split()
        return int(len(words) * 1.3 + len(text) / 4)


logger = logging.getLogger("Beagle.context.context_tracker_ext")


@dataclass
class ContextSnapshot:
    """Snapshot of context at a point in time."""

    timestamp: float
    current_tokens: int
    context_window: int
    prompt_tokens: int
    completion_tokens: int
    session_id: str


@dataclass
class ContextTrackerState:
    """Tracks context across a session."""

    session_id: str = ""
    start_time: float = field(default_factory=time.time)
    # v1.1.1 (S9): monotonic clock for elapsed-interval math (wall-clock
    # time.time() is not monotonic and must not be used for intervals).
    _monotonic_start: float = field(default_factory=time.monotonic, repr=False)

    # Running totals (approximations until real data from LLM)
    input_tokens: int = 0
    output_tokens: int = 0

    # Context window (configurable per model)
    context_window: int = 128000  # Default for Ollama Cloud

    # History of snapshots
    snapshots: list[ContextSnapshot] = field(default_factory=list)

    # Model being used (v1.1.1 S9: resolved from config preset, not a literal)
    model: str = field(default_factory=_default_model)

    # Context management settings
    warning_threshold: float = 0.80
    critical_threshold: float = 0.95

    @property
    def current_tokens(self) -> int:
        """Current context estimate."""
        return self.input_tokens + self.output_tokens

    @property
    def utilization(self) -> float:
        """Current utilization as fraction."""
        if self.context_window <= 0:
            return 0.0
        return self.current_tokens / self.context_window

    @property
    def utilization_percent(self) -> int:
        """Current utilization as percentage."""
        return int(self.utilization * 100)

    @property
    def remaining(self) -> int:
        """Remaining tokens."""
        return max(0, self.context_window - self.current_tokens)

    def get_status_bar(self) -> str:
        """Generate the status bar string for display."""
        pct = self.utilization_percent
        remaining = self.remaining
        window = self.context_window

        # Build bar
        bar_len = 20
        filled = int(bar_len * self.utilization)
        bar = "▓" * filled + "░" * (bar_len - filled)

        # Determine color indicator
        if pct >= 95:
            indicator = "🔴"
        elif pct >= 80:
            indicator = "🟡"
        else:
            indicator = "🟢"

        return f"{indicator} {pct}% {bar} {self.current_tokens:,}/{window:,} ({remaining:,} free)"

    def record_request(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Record tokens from an LLM request/response."""
        self.input_tokens += prompt_tokens
        self.output_tokens += completion_tokens

        snapshot = ContextSnapshot(
            timestamp=time.time(),
            current_tokens=self.current_tokens,
            context_window=self.context_window,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            session_id=self.session_id,
        )
        self.snapshots.append(snapshot)

        # Log status
        status = self.get_status_bar()
        logger.info(f"[Context] {status}")

        # Warnings
        if self.utilization >= self.critical_threshold:
            logger.warning(f"[Context] CRITICAL: {self.utilization_percent}% context used!")
        elif self.utilization >= self.warning_threshold:
            logger.warning(f"[Context] WARNING: {self.utilization_percent}% context used")

    def estimate_from_text(self, input_text: str, output_text: str) -> tuple[int, int]:
        """Estimate tokens from text and record."""
        in_tokens = estimate_tokens(input_text)
        out_tokens = estimate_tokens(output_text)
        self.record_request(in_tokens, out_tokens)
        return in_tokens, out_tokens

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for JSON export."""
        return {
            "session_id": self.session_id,
            "start_time": self.start_time,
            "elapsed_seconds": time.monotonic() - self._monotonic_start,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.current_tokens,
            "context_window": self.context_window,
            "utilization_percent": self.utilization_percent,
            "remaining_tokens": self.remaining,
            "snapshots_count": len(self.snapshots),
        }


def _runtime_state_dir() -> Path:
    """Return a private runtime directory for this process's state."""
    if "XDG_RUNTIME_DIR" in os.environ:
        base = Path(os.environ["XDG_RUNTIME_DIR"]) / "beagle"
    else:
        base = Path.home() / ".beagle" / "runtime"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return base


# Global state
_tracker_state = ContextTrackerState()
_session_state_file = _runtime_state_dir() / "goose_context_tracker.json"


def get_tracker() -> ContextTrackerState:
    """Get the global context tracker."""
    return _tracker_state


def update_tracker_from_llm_response(
    session_id: str,
    model: str,
    input_text: str,
    output_text: str,
    usage: dict | None = None,
) -> ContextTrackerState:
    """Update tracker from LLM response data.

    Args:
        session_id: Current session ID
        model: Model name
        input_text: Input prompt text
        output_text: Output response text
        usage: Optional usage dict with exact token counts from API
            {"prompt_tokens": X, "completion_tokens": Y, "total_tokens": Z}

    """
    state = get_tracker()
    state.session_id = session_id
    state.model = model

    # Update context window based on model
    _update_context_window(state, model)

    if usage:
        # Use exact counts from API
        state.record_request(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )
    else:
        # Estimate from text
        state.estimate_from_text(input_text, output_text)

    # Persist state
    _persist_state(state)

    return state


def _update_context_window(state: ContextTrackerState, model: str) -> None:
    """Update context window based on model."""
    # Model context windows
    windows = {
        # Known-context sizes are a lookup CACHE for named models only;
        # unknown models get the conservative default below. No provider
        # is preset — add entries for YOUR models in config if desired.
        "glm-5.1:cloud": 128000,
        "minimax-m2.7:cloud": 128000,
        "minimax-m2.5": 128000,
        "qwen3.5:397b": 397000,
        "deepseek-v3.2": 128000,
        "gemma3:27b": 128000,
        "gpt-4o": 128000,
        "gemini-2.5-pro": 1000000,
    }
    state.context_window = windows.get(model, 128000)


def _persist_state(state: ContextTrackerState) -> None:
    """Persist tracker state to file."""
    try:
        _session_state_file.write_text(json.dumps(state.to_dict(), indent=2))
    except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
        logger.debug(f"Could not persist state: {e}")


def load_session_state(session_id: str) -> ContextTrackerState | None:
    """Load tracker state for a session."""
    try:
        if _session_state_file.exists():
            data = json.loads(_session_state_file.read_text())
            if data.get("session_id") == session_id:
                state = ContextTrackerState()
                state.session_id = data.get("session_id", "")
                state.start_time = data.get("start_time", time.time())
                state.input_tokens = data.get("input_tokens", 0)
                state.output_tokens = data.get("output_tokens", 0)
                state.context_window = data.get("context_window", 128000)
                state.model = data.get("model", "unknown")
                return state
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning(
            "Cannot load the context-tracker state from %s (%s); starting from a fresh "
            "state, so this session's earlier token usage is lost.",
            _session_state_file,
            exc,
        )
    return None


def get_context_status() -> str:
    """Get formatted context status for display."""
    state = get_tracker()
    return state.get_status_bar()


def get_context_summary() -> dict[str, Any]:
    """Get context summary as dict."""
    return get_tracker().to_dict()


# CLI integration - called by shell prompt hook
if __name__ == "__main__":
    import sys

    def reset_tracker():
        global _tracker_state
        _tracker_state = ContextTrackerState()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if cmd == "status":
            logger.info(get_context_status())
        elif cmd == "summary":
            logger.info(json.dumps(get_context_summary(), indent=2))
        elif cmd == "reset":
            reset_tracker()
            logger.info("Context tracker reset")
        elif cmd == "update" and len(sys.argv) >= 4:
            session_id = sys.argv[2]
            model = sys.argv[3]
            prompt = sys.argv[4] if len(sys.argv) > 4 else ""
            response = sys.argv[5] if len(sys.argv) > 5 else ""
            update_tracker_from_llm_response(session_id, model, prompt, response)
            logger.info(get_context_status())
        else:
            logger.info(
                f"Usage: {sys.argv[0]} status|summary|reset|update "
                f"<session_id> <model> [prompt] [response]"
            )
    else:
        logger.info(get_context_status())
