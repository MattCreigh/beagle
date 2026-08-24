"""SARIF v2.1.0 exporter for Beagle findings."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

from .schema import WorkflowOutput


def _tool_version() -> str:
    """Return the installed Beagle version for the SARIF tool driver.

    Returns:
        The distribution version, or ``"unknown"`` when Beagle is not installed
        as a distribution (e.g. running straight from a source checkout).

    """
    try:
        return _dist_version("beagle")
    except PackageNotFoundError:
        return "unknown"


def to_sarif(output: WorkflowOutput) -> str:
    """Render WorkflowOutput as SARIF JSON string."""

    # Map Beagle severity to SARIF level
    severity_map = {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "note",
        "info": "note",
    }

    results = []
    for finding in output.findings:
        result = {
            "ruleId": f"Beagle-{finding.category.upper()}",
            "level": severity_map.get(finding.severity, "warning"),
            "message": {"text": f"{finding.title}: {finding.description}"},
            "locations": [],
        }

        if finding.file_path:
            location = {"physicalLocation": {"artifactLocation": {"uri": finding.file_path}}}
            if finding.line_start:
                location["physicalLocation"]["region"] = {"startLine": finding.line_start}  # type: ignore[dict-item]
            result["locations"].append(location)  # type: ignore[attr-defined]

        if finding.suggested_fix:
            result["fixes"] = [{"description": {"text": finding.suggested_fix}}]  # type: ignore[list-item]

        results.append(result)

    sarif_log = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Beagle",
                        "version": _tool_version(),
                        "informationUri": "https://github.com/MattCreigh/beagle",
                    }
                },
                "results": results,
            }
        ],
    }

    return json.dumps(sarif_log, indent=2)
