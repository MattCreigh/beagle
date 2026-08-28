"""Terminal User Interface for Beagle using Textual.

v13.5.2 enhancements:
- HardwareStatus widget: live ramdisk usage, SSD writes saved, CPU governor
- Periodic (2s) hardware metric refresh via set_interval
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Digits, Footer, Header, Label, ProgressBar, RichLog, Static

from beagle.config._config_path import find_config_toml

from ..events import (
    BeagleEvent,
    ContextWarning,
    NodeCompleted,
    NodeFailed,
    NodeOutput,
    NodeStarted,
    WorkflowStarted,
    get_event_bus,
)
from .widgets.slo_indicator import SLOIndicator

logger = logging.getLogger("Beagle.tui")


class SLOUpdate(Message):
    """Internal message to update SLO indicator."""

    def __init__(
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
        super().__init__()


class NodeUpdate(Message):
    """Internal message to update node status."""

    def __init__(self, node_name: str, status: str, error: str | None = None) -> None:
        self.node_name = node_name
        self.status = status
        self.error = error
        super().__init__()


class OutputUpdate(Message):
    """Internal message for agent output."""

    def __init__(self, node_name: str, text: str) -> None:
        self.node_name = node_name
        self.text = text
        super().__init__()


class MetricUpdate(Message):
    """Internal message for cost/token updates."""

    def __init__(self, cost: float, tokens: int, budget: float) -> None:
        self.cost = cost
        self.tokens = tokens
        self.budget = budget
        super().__init__()


class ContextUpdate(Message):
    """Internal message for context window updates."""

    def __init__(self, utilization: float) -> None:
        self.utilization = utilization
        super().__init__()


class WorkflowSetup(Message):
    """Internal message to setup the workflow nodes."""

    def __init__(self, nodes: list[str]) -> None:
        self.nodes = nodes
        super().__init__()


class DAGStatus(Static):
    """Widget to display DAG node status."""

    nodes: dict[str, str] = reactive({})  # type: ignore[assignment]

    def render(self) -> str:
        if not self.nodes:
            return "No nodes loaded"

        lines = ["[bold]DAG Execution Status[/bold]\n"]
        for name, status in self.nodes.items():
            icon = "⏳"  # pending
            if status == "running":
                icon = "⠋"
            elif status == "completed":
                icon = "✅"
            elif status == "failed":
                icon = "❌"
            elif status == "retrying":
                icon = "🔄"

            lines.append(f"{icon} {name}")

        return "\n".join(lines)

    def update_node(self, name: str, status: str) -> None:
        # Create a new dict to trigger reactive update
        new_nodes = self.nodes.copy()
        new_nodes[name] = status
        self.nodes = new_nodes

    def setup_nodes(self, node_names: list[str]) -> None:
        self.nodes = dict.fromkeys(node_names, "pending")


# ── Hardware Status Widget (v13.5.2) ──────────────────────────────────────────


class HardwareStatus(Static):
    """Widget to display real-time hardware stats.

    Shows ramdisk usage, SSD writes saved, and current CPU governor.
    Values are updated reactively from BeagleApp's periodic refresh.
    """

    ramdisk_usage: float = reactive(0.0)  # type: ignore[assignment]
    ssd_writes_saved_mb: float = reactive(0.0)  # type: ignore[assignment]
    cpu_governor: str = reactive("unknown")  # type: ignore[assignment]

    def render(self) -> str:
        ram_bar_len = 20
        ram_filled = int(self.ramdisk_usage / 100 * ram_bar_len)
        ram_bar = "█" * ram_filled + "░" * (ram_bar_len - ram_filled)

        return (
            "[bold]Hardware Status[/bold]\n\n"
            f"  💾 Ramdisk: [{ram_bar}] {self.ramdisk_usage:.1f}%\n"
            f"  🗑️  SSD Saved: {self.ssd_writes_saved_mb:.1f} MB\n"
            f"  ⚡ CPU Gov: {self.cpu_governor}\n"
        )


class BeagleApp(App):
    """The main Beagle TUI Dashboard."""

    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 3;
        grid-columns: 1fr 1fr;
        grid-rows: 1fr 1fr auto;
    }

    #dag-status {
        border: solid green;
        padding: 1;
    }

    #live-output {
        border: solid blue;
        column-span: 2;
        row-span: 1;
    }

    #metrics {
        border: solid yellow;
        padding: 1;
    }

    #context-view {
        border: solid magenta;
        padding: 1;
    }

    #hardware-status {
        border: solid cyan;
        padding: 1;
        column-span: 2;
        height: 7;
    }
    """

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [  # type: ignore[assignment]
        ("q", "quit", "Quit"),
        ("p", "toggle_pause", "Pause/Resume"),
        ("d", "toggle_details", "Toggle Details"),
        ("s", "steer", "Steer"),
    ]

    total_cost = reactive(0.0)
    total_tokens = reactive(0)
    budget = reactive(10.0)
    context_utilization = reactive(0.0)

    # Hardware reactive attributes (v13.5.2)
    ramdisk_usage = reactive(0.0)
    ssd_writes_saved_mb: float = reactive(0.0)  # type: ignore[assignment]
    cpu_governor = reactive("unknown")

    def __init__(self, workflow_id: str, query: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.workflow_id = workflow_id
        self.query = query  # type: ignore[assignment,method-assign]
        self.sub_id: Any = None
        self.title = f"Beagle: {self.workflow_id}"
        self.sub_title = self.query[:60] + "..." if len(self.query) > 60 else self.query  # type: ignore[arg-type,assignment,index]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DAGStatus(id="dag-status")
        with Vertical(id="metrics"):
            yield Label("Cost Metrics")
            yield Digits(id="cost-display")
            yield ProgressBar(id="budget-progress", total=100, show_percentage=True)
            yield Label(id="token-display")
        with Vertical(id="context-view"):
            yield Label("Context Window Utilization")
            yield ProgressBar(id="context-progress", total=100, show_percentage=True)
        yield SLOIndicator(id="slo-indicator")
        yield RichLog(id="live-output", max_lines=50, highlight=True, markup=True)
        yield HardwareStatus(id="hardware-status")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = f"Beagle: {self.workflow_id}"
        self.sub_title = self.query[:60] + "..." if len(self.query) > 60 else self.query  # type: ignore[arg-type,assignment,index]

        bus = get_event_bus()
        self.sub_id = bus.subscribe("*", self._handle_bus_event)  # type: ignore[assignment]

        # Initial state for progress bars
        self.query_one("#budget-progress").update(progress=0)  # type: ignore[attr-defined]
        self.query_one("#context-progress").update(progress=0)  # type: ignore[attr-defined]
        self.query_one("#cost-display").update("$0.0000")  # type: ignore[attr-defined]
        self.query_one("#token-display").update("Tokens: 0")  # type: ignore[attr-defined]

        # Start periodic hardware stats refresh (every 2 seconds)
        self.set_interval(2, self._refresh_hardware_stats)

    # ── Hardware Stats Refresh (v13.5.2) ───────────────────────────────────

    def _refresh_hardware_stats(self) -> None:
        """Periodically fetch hardware metrics and update reactive vars."""
        # 1. Ramdisk usage
        try:
            import tomllib

            config_path = find_config_toml()
            if config_path.exists():
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
                hw = data.get("hardware", {})
                if hw.get("ramdisk_enabled", True):
                    ramdisk_path = hw.get("ramdisk_path", "/mnt/beagle_rag_staging")
                    rp = Path(ramdisk_path)
                    if rp.exists():
                        stat = os.statvfs(str(rp))
                        total = stat.f_blocks * stat.f_frsize
                        used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
                        if total > 0:
                            self.ramdisk_usage = round((used / total) * 100, 1)
                        else:
                            self.ramdisk_usage = 0.0
                    else:
                        self.ramdisk_usage = 0.0
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
            self.ramdisk_usage = 0.0

        # 2. SSD writes saved
        try:
            from ..infrastructure.cast_ingestion import (
                _ssd_writes_saved_bytes,
            )

            self.ssd_writes_saved_mb = round(_ssd_writes_saved_bytes / (1024 * 1024), 1)
        except ImportError:
            self.ssd_writes_saved_mb = 0
        except (AttributeError, TypeError, ValueError, ZeroDivisionError) as exc:
            logger.warning(
                "Cannot compute the SSD-writes-saved figure (%s); the panel keeps its "
                "previous value of %s MB.",
                exc,
                self.ssd_writes_saved_mb,
            )

        # 3. CPU governor
        try:
            from ..infrastructure.cpu_governor import (
                get_current_governor,
            )

            self.cpu_governor = get_current_governor()
        except ImportError:
            self.cpu_governor = "unknown"
        except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
            self.cpu_governor = "unknown"

        # Propagate to the HardwareStatus widget
        hw_widget = self.query_one(HardwareStatus)
        hw_widget.ramdisk_usage = self.ramdisk_usage
        hw_widget.ssd_writes_saved_mb = self.ssd_writes_saved_mb
        hw_widget.cpu_governor = self.cpu_governor

    def _handle_bus_event(self, event: BeagleEvent) -> None:
        """Subscriber callback from EventBus thread."""
        if event.workflow_id != self.workflow_id and self.workflow_id != "*":
            return

        if isinstance(event, NodeStarted):
            self.post_message(NodeUpdate(event.node_name, "running"))
        elif isinstance(event, NodeCompleted):
            self.post_message(NodeUpdate(event.node_name, "completed"))
            self.post_message(MetricUpdate(event.cost, event.tokens, self.budget))
        elif isinstance(event, NodeFailed):
            self.post_message(NodeUpdate(event.node_name, "failed", event.error))
        elif isinstance(event, NodeOutput):
            self.post_message(OutputUpdate(event.node_name, event.content))
        elif isinstance(event, WorkflowStarted):
            self.budget = event.budget_usd
            if event.nodes:
                self.post_message(WorkflowSetup(event.nodes))
        elif isinstance(event, ContextWarning):
            self.post_message(ContextUpdate(event.utilization))

    @on(WorkflowSetup)
    def on_workflow_setup(self, message: WorkflowSetup) -> None:
        self.query_one(DAGStatus).setup_nodes(message.nodes)

    @on(NodeUpdate)
    def on_node_update(self, message: NodeUpdate) -> None:
        self.query_one(DAGStatus).update_node(message.node_name, message.status)
        if message.error:
            self.query_one("#live-output").write(  # type: ignore[attr-defined]
                f"[bold red]ERROR in {message.node_name}: {message.error}[/bold red]"
            )

    @on(OutputUpdate)
    def on_output_update(self, message: OutputUpdate) -> None:
        # Assign a color based on node name hash
        colors = ["cyan", "magenta", "green", "yellow", "blue", "white"]
        color = colors[hash(message.node_name) % len(colors)]
        self.query_one("#live-output").write(  # type: ignore[attr-defined]
            f"[{color}]{message.node_name}[/{color}] | {message.text.strip()}"
        )

    @on(MetricUpdate)
    def on_metric_update(self, message: MetricUpdate) -> None:
        self.total_cost += message.cost
        self.total_tokens += message.tokens
        self.query_one("#cost-display").update(f"${self.total_cost:.4f}")  # type: ignore[attr-defined]
        self.query_one("#token-display").update(f"Tokens: {self.total_tokens:,}")  # type: ignore[attr-defined]

        if message.budget > 0:
            perc = (self.total_cost / message.budget) * 100
            self.query_one("#budget-progress").update(progress=min(100, perc))  # type: ignore[attr-defined]

    @on(ContextUpdate)
    def on_context_update(self, message: ContextUpdate) -> None:
        self.context_utilization = message.utilization
        self.query_one("#context-progress").update(progress=min(100, message.utilization))  # type: ignore[attr-defined]

    @on(SLOUpdate)
    def on_slo_update(self, message: SLOUpdate) -> None:
        self.query_one(SLOIndicator).update_indicator(
            status=message.status,
            compliance_pct=message.compliance_pct,
            budget_pct=message.budget_pct,
            worst_sli=message.worst_sli,
        )

    def action_quit(self) -> None:  # type: ignore[override]
        """Exit the application."""
        if self.sub_id:
            get_event_bus().unsubscribe(self.sub_id)
        self.exit()

    def action_toggle_pause(self) -> None:
        """Pause/Resume the current node execution."""
        # This requires process management logic in autonomous_orchestrator
        self.query_one("#live-output").write(  # type: ignore[attr-defined]
            "[yellow]System: Pause requested (SIGSTOP/SIGCONT simulation)[/yellow]"
        )

    def action_steer(self) -> None:
        """Open steering input and write to steer.md."""
        from textual.containers import Vertical
        from textual.screen import ModalScreen
        from textual.widgets import Input

        class SteeringModal(ModalScreen):
            def compose(self) -> ComposeResult:
                with Vertical(id="modal-container"):
                    yield Label("Mid-Workflow Steering Guidance:")
                    yield Input(placeholder="e.g. Skip node 'verification'", id="steer-input")
                    yield Label("Press Enter to apply, Esc to cancel")

            @on(Input.Submitted)
            def on_input_submitted(self, event: Input.Submitted) -> None:
                guidance = event.value
                if guidance:
                    # Write to steer.md

                    from ..utils.env_manager import get_workspace_root

                    workspace = get_workspace_root()
                    steer_file = workspace / "steer.md"
                    content = f"# Steering Guidance\n{guidance}\n"

                    # Simple heuristic: if 'skip' in guidance, add a skip section
                    if "skip" in guidance.lower():
                        import re

                        match = re.search(r"skip node ['\"]([^'\"]+)['\"]", guidance.lower())
                        if match:
                            content += f"\n# Skip Nodes\n{match.group(1)}\n"

                    try:
                        steer_file.write_text(content, encoding="utf-8")
                        self.app.query_one("#live-output").write(  # type: ignore[attr-defined]
                            f"[blue]Steering applied:[/blue] {guidance}"
                        )
                    except OSError as e:
                        self.app.query_one("#live-output").write(  # type: ignore[attr-defined]
                            f"[red]Failed to apply steering:[/red] {e}"
                        )

                self.app.pop_screen()

        self.push_screen(SteeringModal())

    def action_toggle_details(self) -> None:
        """Toggle detail view."""
        # Placeholder for toggling layout ratios
        pass
