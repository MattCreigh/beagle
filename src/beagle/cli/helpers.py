"""Shared CLI helpers — single implementation (SP-8 de-duplication).

beagle-spotless-phase2, work package SP-8: two previous modules
``cli/_helpers.py`` and ``cli/cli_helpers.py`` both defined the same trio of
helpers (``resolve_workflow``, ``persist_report``, ``show_estimate``) with
divergent signatures. Neither was imported anywhere. This module is the single
canonical implementation. The two duplicates were moved aside; see the SP-8
report for the reversion path.

Three pure helpers used by Typer commands:

  - resolve_workflow:  workflow name → YAML Path (workspace/metaprompts lookup)
  - persist_report:    write final markdown report to analysis_reports/
  - show_estimate:     print a per-phase cost estimate for a workflow
"""

from __future__ import annotations

import logging
import time as _time
from pathlib import Path
from typing import Any

from beagle.config.config import get_config

logger = logging.getLogger("Beagle.cli.helpers")


def resolve_workflow(workflow: str) -> Path:
    """Resolve a workflow name to its YAML path.

    Lookup order:
      1. Literal path on disk (Path(workflow).exists())
      2. Path with `/` or `.yaml` suffix
      3. metaprompts/<name> (canonical, e.g. "audit")
      4. metaprompts/<name>.yaml
      5. metaprompts/<name with _↔->.yaml
      6. YAML files whose `name:` field matches the request

    Raises:
        FileNotFoundError: If no matching workflow is found.

    """
    # v1.1.1 (S5): metaprompts moved to the canonical config root.
    from beagle.config._config_path import find_metaprompts_dir

    # 1. Literal path
    if Path(workflow).exists():
        return Path(workflow)

    metaprompts_dir = find_metaprompts_dir()

    # 2. Already-shaped path
    if "/" in workflow or workflow.endswith(".yaml"):
        path = Path(workflow)
        if path.exists():
            return path

    # 3-5. Canonical + extension + underscore/hyphen variants
    for candidate in (
        metaprompts_dir / workflow,
        metaprompts_dir / f"{workflow}.yaml",
        metaprompts_dir / f"{workflow.replace('-', '_')}.yaml",
        metaprompts_dir / f"{workflow.replace('_', '-')}.yaml",
    ):
        if candidate.exists():
            return candidate

    # 6. Match by `name:` field in YAML
    try:
        import yaml

        for yaml_path in metaprompts_dir.glob("*.yaml"):
            try:
                with open(yaml_path, encoding="utf-8") as f:
                    spec = yaml.safe_load(f)
                if spec and spec.get("name") == workflow:
                    return yaml_path
            except OSError as exc:
                logger.warning(
                    "Cannot read workflow spec %s (%s); skipping it, so a workflow "
                    "defined there will not be found.",
                    yaml_path,
                    exc,
                )
                continue
    except Exception as exc:  # ruff: ignore[BLE001]  # broad catch intentional — never let a glob failure block resolution
        logger.warning(
            "Cannot scan the metaprompts directory for workflow %r (%s); "
            "reporting the workflow as not found.",
            workflow,
            exc,
        )

    raise FileNotFoundError(f"Workflow not found: {workflow}")


def persist_report(workflow: str, report: str, mode: str, console: Any = None) -> Path | None:
    """Write the final report to the analysis_reports directory.

    Stamped filename: {safe_workflow}_{mode}_{YYYYMMDD_HHMMSS}.md

    Args:
        workflow: Workflow name
        report: Report content
        mode: Workflow mode (audit, develop, research)
        console: Optional Rich Console for user feedback. When None, no
            progress line is printed (import-safe for library callers).

    Returns:
        Path to saved report, or None on error.

    """
    from beagle.core.autonomous_orchestrator import get_output_dir

    output_dir = get_output_dir()
    timestamp = _time.strftime("%Y%m%d_%H%M%S")
    safe_name = workflow.replace("/", "_").replace(" ", "_")[:50]
    filename = f"{safe_name}_{mode}_{timestamp}.md"
    report_path = output_dir / filename

    try:
        report_path.write_text(report, encoding="utf-8")
        if console is not None:
            console.print(f"\n[bold green]Report saved:[/bold green] {report_path}")
        return report_path
    except OSError as e:
        if console is not None:
            console.print(f"[yellow]Warning: Could not save report to disk: {e}[/yellow]")
        return None


def show_estimate(workflow_path: Path, console: Any = None) -> None:
    """Show a per-phase cost estimate for the given workflow path.

    Rough heuristic: 2k input + 4k output tokens per phase. Real costs vary
    with prompt/response length; the estimate is a coarse pre-flight check.
    """
    from beagle.core.workflow_loader import load_workflow
    from beagle.cost_tracker import MODEL_PRICING

    try:
        dag = load_workflow(workflow_path)
    except Exception as e:  # broad catch intentional — surface as user-visible error
        if console is not None:
            console.print(f"[bold red]Error:[/bold red] {e}")
        import typer

        raise typer.Exit(1) from e

    config = get_config()
    model = config.goose.default_model

    pricing = MODEL_PRICING.get(model, MODEL_PRICING["default"])

    estimated_input_per_phase = 2000
    estimated_output_per_phase = 4000

    lines: list[str] = ["\n[bold]Cost Estimate[/bold]\n"]
    total_input = 0
    total_output = 0
    for node_name in dag.nodes:
        input_tokens = estimated_input_per_phase
        output_tokens = estimated_output_per_phase
        total_input += input_tokens
        total_output += output_tokens

        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        phase_cost = input_cost + output_cost

        lines.append(f"  {node_name}: ~${phase_cost:.4f} ({input_tokens + output_tokens:,} tokens)")

    total_input_cost = (total_input / 1_000_000) * pricing["input"]
    total_output_cost = (total_output / 1_000_000) * pricing["output"]
    total_cost = total_input_cost + total_output_cost

    lines.append(
        f"\n[bold]Total Estimate:[/bold] ~${total_cost:.4f} ({total_input + total_output:,} tokens)"
    )
    lines.append(f"[dim]Model: {model}[/dim]")
    lines.append("\n[yellow]Note: Actual costs may vary based on prompt/response length[/yellow]")

    if console is not None:
        for line in lines:
            console.print(line)
