"""Parser for extracting structured findings from agent output."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .schema import Finding, OutputMetrics, WorkflowOutput

logger = logging.getLogger("Beagle.output.parser")


class OutputParser:
    """Parses agent reports into structured WorkflowOutput."""

    def __init__(self, workflow_id: str, workflow_name: str, query: str):
        self.workflow_id = workflow_id
        self.workflow_name = workflow_name
        self.query = query

    async def parse(self, raw_report: str) -> WorkflowOutput:
        """Extract structured data using tiered strategies."""

        # Strategy 1: JSON Block Extraction
        structured_data = self._extract_json_block(raw_report)

        # Strategy 2: LLM-Assisted Extraction (Fallback)
        if not structured_data:
            structured_data = await self._llm_assisted_extraction(raw_report)

        # Strategy 3: Graceful Degradation (Final Fallback)
        if not structured_data:
            structured_data = self._minimal_fallback(raw_report)

        return self._build_output(structured_data, raw_report)

    def _extract_json_block(self, text: str) -> dict[str, Any] | None:
        """Try to find and parse a ```json block."""
        match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if not match:
            # Try plain { ... } if no backticks
            match = re.search(r"(\{.*\})", text, re.DOTALL)

        if match:
            try:
                return json.loads(match.group(1))  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                logger.warning("Found JSON block but failed to parse it")
        return None

    async def _llm_assisted_extraction(self, text: str) -> dict[str, Any] | None:
        """Use a cheap model to extract findings if parsing failed."""
        from ..core.nodes import execute_headless_goose

        prompt = f"""Extract structured findings from this report as JSON matching this schema:
{{
  "summary": "2-3 sentence executive summary",
  "findings": [
    {{
      "severity": "critical|high|medium|low|info",
      "category": "bug|security|performance|style|architecture",
      "title": "Short title",
      "description": "Details",
      "file_path": "optional/path.py",
      "line_start": 42,
      "suggested_fix": "How to fix"
    }}
  ]
}}

REPORT:
{text[:10000]}
"""
        # Resolve the cheap/fast model from config.toml rather than hardcoding
        # it. This call used a literal "gemma3:27b", which Ollama Cloud RETIRED
        # on 2026-07-15 — every LLM-assisted extraction had been failing with
        # "HTTP 410 Gone" and falling through to _minimal_fallback(), silently
        # degrading every report that needed this path. A hardcoded model name
        # cannot be caught by the [models.allowed] allowlist or the startup
        # catalogue check, which is exactly why it outlived the model refresh.
        from ..config.config import get_config

        cheap_model = get_config().llm.cheap_model
        try:
            result, _ = await execute_headless_goose(
                prompt=prompt,
                system_directive="You are a JSON extraction engine. Output ONLY raw JSON.",
                node_name="output-extractor",
                model=cheap_model,
            )
            return self._extract_json_block(result)
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.error(f"LLM-assisted extraction failed: {e}")
            return None

    def _minimal_fallback(self, raw_report: str) -> dict[str, Any]:
        """Create a minimal structured result when all parsing fails."""
        return {
            "summary": "Automated parsing failed. Refer to raw report.",
            "findings": [
                {
                    "severity": "info",
                    "category": "architecture",
                    "title": "Raw Output Report",
                    "description": (
                        "The system was unable to parse structured findings. "
                        "The full report is available in raw_report."
                    ),
                    "file_path": None,
                    "line_start": None,
                    "suggested_fix": "Review raw report manually.",
                }
            ],
        }

    def _build_output(self, data: dict[str, Any], raw_report: str) -> WorkflowOutput:
        """Construct the final WorkflowOutput object with calculated metrics."""
        findings_data = data.get("findings", [])
        findings = []

        by_severity = {}  # type: ignore[var-annotated]
        by_category = {}  # type: ignore[var-annotated]
        affected_files = set()

        for f in findings_data:
            finding = Finding(
                severity=f.get("severity", "info").lower(),
                category=f.get("category", "bug").lower(),
                title=f.get("title", "Untitled Finding"),
                description=f.get("description", ""),
                file_path=f.get("file_path"),
                line_start=f.get("line_start"),
                suggested_fix=f.get("suggested_fix"),
            )
            findings.append(finding)

            # Update metrics
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
            by_category[finding.category] = by_category.get(finding.category, 0) + 1
            if finding.file_path:
                affected_files.add(finding.file_path)

        metrics = OutputMetrics(
            total_findings=len(findings),
            by_severity=by_severity,
            by_category=by_category,
            files_affected=len(affected_files),
        )

        return WorkflowOutput(
            workflow_id=self.workflow_id,
            workflow_name=self.workflow_name,
            query=self.query,
            summary=data.get("summary", ""),
            findings=findings,
            metrics=metrics,
            raw_report=raw_report,
        )
