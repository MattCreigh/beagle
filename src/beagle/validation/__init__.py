"""Beagle Output Validation — run tests, lints, and regression checks on code Beagle produces."""

from __future__ import annotations

from .analyzer import Regression, RegressionDetector
from .feedback import FeedbackLoop, get_feedback_loop, run_validation
from .runner import ToolResult, ValidationResult, ValidationRunner

__all__ = [
    "FeedbackLoop",
    "Regression",
    "RegressionDetector",
    "ToolResult",
    "ValidationResult",
    "ValidationRunner",
    "get_feedback_loop",
    "run_validation",
]
