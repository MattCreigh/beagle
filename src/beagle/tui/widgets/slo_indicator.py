"""SLO compliance indicator widget for the Beagle TUI."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


class SLOIndicator(Static):
    """Widget showing current SLO compliance status."""

    status: str = reactive("unknown")  # type: ignore[assignment]
    compliance_pct: float = reactive(0.0)  # type: ignore[assignment]
    budget_pct: float = reactive(0.0)  # type: ignore[assignment]
    worst_sli: str = reactive("")  # type: ignore[assignment]

    def render(self) -> str:
        if self.status == "unknown":
            return "[dim]SLO: No data yet[/dim]"

        emoji = {
            "healthy": "✅",
            "caution": "⚠️",
            "burning": "🔥",
            "exhausted": "🚨",
        }.get(self.status, "❓")

        color = {
            "healthy": "green",
            "caution": "yellow",
            "burning": "red",
            "exhausted": "bold red",
        }.get(self.status, "dim")

        lines = [
            f"[bold {color}]{emoji} SLO Status: {self.status.upper()}[/bold {color}]",
            f"  Compliance: {self.compliance_pct:.1f}%",
            f"  Budget: {self.budget_pct:.1f}%",
        ]
        if self.worst_sli:
            lines.append(f"  Worst SLI: {self.worst_sli}")
        return "\n".join(lines)

    def update_indicator(
        self,
        status: str,
        compliance_pct: float,
        budget_pct: float,
        worst_sli: str = "",
    ) -> None:
        self.status = status
        self.compliance_pct = compliance_pct
        self.budget_pct = budget_pct
        self.worst_sli = worst_sli
