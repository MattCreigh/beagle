"""Sections 11.1-11.2: Structured JSON log formatter + top-10 conversion tests."""

from __future__ import annotations

import json
import logging

from beagle.utils.structured_logging import (
    StructuredJSONFormatter,
    setup_structured_logging,
)


class TestStructuredJSONFormatter:
    """Structured JSON formatter produces valid, parseable JSON."""

    def test_basic_log_record(self):
        """A basic log record produces valid JSON with required fields."""
        formatter = StructuredJSONFormatter()
        record = logging.LogRecord(
            name="Beagle.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Hello world",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["logger"] == "Beagle.test"
        assert data["message"] == "Hello world"
        assert data["line"] == 42
        assert "timestamp" in data
        assert "hostname" in data
        assert "process" in data

    def test_exception_info_included(self):
        """Exception info appears in JSON output."""
        formatter = StructuredJSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="Beagle.test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=10,
            msg="failed",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["exception_type"] == "ValueError"
        assert "test error" in data["exception_message"]

    def test_beagle_workflow_id_included(self):
        """Custom beagle_workflow_id fields appear in JSON output."""
        formatter = StructuredJSONFormatter()
        record = logging.LogRecord(
            name="Beagle.orchestrator",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="workflow started",
            args=(),
            exc_info=None,
        )
        record.beagle_workflow_id = "wf-123"  # type: ignore[attr-defined]
        record.beagle_node_name = "researcher"  # type: ignore[attr-defined]
        record.beagle_cost_usd = 0.05  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert data["beagle_workflow_id"] == "wf-123"
        assert data["beagle_node_name"] == "researcher"
        assert data["beagle_cost_usd"] == 0.05

    def test_missing_custom_fields_omitted(self):
        """Fields not set on the record are omitted from JSON output."""
        formatter = StructuredJSONFormatter()
        record = logging.LogRecord(
            name="Beagle.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="no extras",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "beagle_workflow_id" not in data
        assert "beagle_node_name" not in data


class TestSetupStructuredLogging:
    """setup_structured_logging configures root logger correctly."""

    def test_json_format_from_env(self):
        """BEAGLE_LOG_JSON=true enables JSON structured logging."""
        import os

        os.environ["BEAGLE_LOG_JSON"] = "true"
        try:
            handler = setup_structured_logging(level="INFO", json_format=None)
            assert isinstance(handler.formatter, StructuredJSONFormatter)
        finally:
            os.environ.pop("BEAGLE_LOG_JSON", None)

    def test_text_format_default(self):
        """Without BEAGLE_LOG_JSON, default is text format."""
        import os

        os.environ.pop("BEAGLE_LOG_JSON", None)
        handler = setup_structured_logging(level="INFO", json_format=False)
        assert not isinstance(handler.formatter, StructuredJSONFormatter)

    def test_explicit_json_format(self):
        """Explicitly setting json_format=True uses JSON formatter."""
        handler = setup_structured_logging(level="DEBUG", json_format=True)
        assert isinstance(handler.formatter, StructuredJSONFormatter)
