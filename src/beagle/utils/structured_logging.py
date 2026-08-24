"""Structured JSON log formatter for Beagle operational observability.

Outputs log records as JSON objects for machine-parseable log aggregation
(Logstash, Fluentd, CloudWatch, etc.). Enabled via config.toml
[logging].json_format = true or BEAGLE_LOG_JSON=true env var.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import UTC, datetime
from typing import Any, Literal


class StructuredJSONFormatter(logging.Formatter):
    """Format log records as JSON objects.

    Each record produces a single JSON line with:
    - timestamp: ISO 8601 UTC
    - level: log level name
    - logger: logger name
    - message: formatted message
    - module, line, function: source location
    - hostname: machine name
    - process, thread: process/thread IDs
    - beagle_workflow_id: workflow ID from LogRecord (if set)
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: Literal["%", "{", "$"] = "%",
        validate: bool = True,
        *,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            fmt=fmt, datefmt=datefmt, style=style, validate=validate, defaults=defaults
        )
        self._hostname = socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        """Format a LogRecord as a JSON string."""
        # Build the structured record
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
            "function": record.funcName,
            "hostname": self._hostname,
            "process": record.process,
            "thread": record.thread,
        }

        # Add workflow/correlation IDs if present
        if hasattr(record, "beagle_workflow_id") and record.beagle_workflow_id:
            entry["beagle_workflow_id"] = record.beagle_workflow_id
        if hasattr(record, "beagle_node_name") and record.beagle_node_name:
            entry["beagle_node_name"] = record.beagle_node_name
        if hasattr(record, "beagle_model") and record.beagle_model:
            entry["beagle_model"] = record.beagle_model
        if hasattr(record, "beagle_cost_usd") and record.beagle_cost_usd is not None:
            entry["beagle_cost_usd"] = record.beagle_cost_usd
        if hasattr(record, "beagle_tokens") and record.beagle_tokens is not None:
            entry["beagle_tokens"] = record.beagle_tokens
        if hasattr(record, "beagle_duration_s") and record.beagle_duration_s is not None:
            entry["beagle_duration_s"] = record.beagle_duration_s

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception_type"] = record.exc_info[0].__name__
            entry["exception_message"] = str(record.exc_info[1])

        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_structured_logging(
    level: str = "INFO",
    json_format: bool | None = None,
) -> logging.Handler:
    """Configure logging with optional JSON structured output.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR).
        json_format: If True, use StructuredJSONFormatter.
            If None, reads from BEAGLE_LOG_JSON env var or config.

    Returns:
        The configured handler.

    """
    if json_format is None:
        json_format = os.environ.get("BEAGLE_LOG_JSON", "").lower() in (
            "true",
            "1",
            "yes",
        )

    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(StructuredJSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root_logger = logging.getLogger("Beagle")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates
    root_logger.handlers = [
        h for h in root_logger.handlers if not isinstance(h, logging.StreamHandler)
    ]
    root_logger.addHandler(handler)

    return handler
