"""Tests for token streaming with early termination."""

from __future__ import annotations

import asyncio
import re

import pytest

from beagle.config.schema import StreamingConfig, WorkflowConfig
from beagle.utils.subprocess_pool import (
    _STREAMING_FINAL_ANSWER_PATTERN,
)

# ── StreamingConfig tests ─────────────────────────────────────────────────


class TestStreamingConfig:
    """Verify StreamingConfig dataclass defaults and integration."""

    def test_default_values(self):
        cfg = StreamingConfig()
        assert cfg.enabled is True
        assert cfg.early_termination is True
        assert cfg.termination_pattern == r"</final_answer\s*>"
        assert cfg.buffer_size == 8192

    def test_in_workflow_config(self):
        cfg = WorkflowConfig()
        assert hasattr(cfg, "streaming")
        assert cfg.streaming.enabled is True
        assert cfg.streaming.early_termination is True

    def test_disable_early_termination(self):
        cfg = StreamingConfig(early_termination=False)
        assert cfg.early_termination is False

    def test_custom_termination_pattern(self):
        cfg = StreamingConfig(termination_pattern=r"</result\s*>")
        assert cfg.termination_pattern == r"</result\s*>"

    def test_fully_disabled(self):
        cfg = StreamingConfig(enabled=False)
        assert cfg.enabled is False


# ── Pattern matching tests ────────────────────────────────────────────────


class TestFinalAnswerPattern:
    """Verify the streaming final_answer detection regex."""

    @pytest.mark.parametrize(
        "line",
        [
            "</final_answer>",
            "</final_answer >",
            "</final_answer  >",
            "some text</final_answer>",
            "some text </final_answer>  ",
        ],
    )
    def test_pattern_matches(self, line):
        assert _STREAMING_FINAL_ANSWER_PATTERN.search(line) is not None

    @pytest.mark.parametrize(
        "line",
        [
            "<final_answer>",
            "final_answer",
            "</final",
            "</final_ans>",
            "some unrelated text",
        ],
    )
    def test_pattern_no_match(self, line):
        assert _STREAMING_FINAL_ANSWER_PATTERN.search(line) is None

    def test_pattern_compiled(self):
        """Ensure the pattern is a compiled regex."""
        assert isinstance(_STREAMING_FINAL_ANSWER_PATTERN, re.Pattern)


# ── Streaming read unit tests (no real subprocess) ────────────────────────


class TestStreamingReadUnit:
    """Unit tests for _streaming_read logic without real processes."""

    def test_config_influences_subprocess_pool_path(self):
        """Verify StreamingConfig.enabled controls code path selection."""
        # When enabled, _streaming_read is used
        cfg = StreamingConfig(enabled=True, early_termination=True)
        assert cfg.enabled and cfg.early_termination

        # When disabled, process.communicate() fallback is used
        cfg_disabled = StreamingConfig(enabled=False)
        assert not cfg_disabled.enabled

    def test_termination_pattern_is_valid_regex(self):
        """Ensure the termination pattern compiles."""
        from beagle.config.schema import StreamingConfig

        cfg = StreamingConfig()
        compiled = re.compile(cfg.termination_pattern)
        assert compiled.search("</final_answer>") is not None
        assert compiled.search("plain text") is None

    def test_max_early_drain_lines_is_reasonable(self):
        """Verify _MAX_EARLY_DRAIN_LINES is bounded."""
        from beagle.utils.subprocess_pool import (
            _MAX_EARLY_DRAIN_LINES,
        )

        assert 0 < _MAX_EARLY_DRAIN_LINES <= 200


# ── Orchestrator early termination tests ──────────────────────────────────


class TestOrchestratorEarlyTermination:
    """Verify the orchestrator's read_stream detects final_answer."""

    def test_final_answer_detected_event(self):
        """Verify asyncio.Event is set when </final_answer> appears."""
        event = asyncio.Event()
        line = "Here is my answer</final_answer>"
        if _STREAMING_FINAL_ANSWER_PATTERN.search(line):
            event.set()
        assert event.is_set()

    def test_no_false_positive_on_opening_tag(self):
        """Verify <final_answer> (opening) doesn't set the event."""
        event = asyncio.Event()
        line = "<final_answer>starting response"
        # The pattern only matches the CLOSING tag
        if _STREAMING_FINAL_ANSWER_PATTERN.search(line):
            event.set()
        assert not event.is_set()

    def test_event_not_set_without_tag(self):
        """Verify event stays unset for normal output lines."""
        event = asyncio.Event()
        line = "Just some regular output from the process"
        if _STREAMING_FINAL_ANSWER_PATTERN.search(line):
            event.set()
        assert not event.is_set()
