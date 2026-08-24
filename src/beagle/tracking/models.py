"""Database models and schema for run tracking."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class WorkflowRun:
    """Represents a full execution of a workflow."""

    id: str
    workflow_name: str
    query: str
    mode: str = "audit"
    started_at: float = 0.0
    completed_at: float | None = None
    success: bool = False
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_seconds: float | None = None
    nodes_completed: int = 0
    nodes_failed: int = 0
    budget_usd: float = 0.0
    config_snapshot: str | None = None
    error_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NodeRun:
    """Represents the execution of a single DAG node."""

    id: str
    workflow_run_id: str
    node_name: str
    skill_name: str
    model: str
    started_at: float
    completed_at: float | None = None
    success: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float | None = None
    attempts: int = 1
    result_hash: str | None = None
    error: str | None = None


@dataclass
class Finding:
    """A specific issue or insight found during a run."""

    id: str
    workflow_run_id: str
    node_name: str
    severity: str  # critical, high, medium, low, info
    category: str  # bug, security, performance, style
    title: str
    description: str
    file_path: str | None = None
    line_number: int | None = None
    suggested_fix: str | None = None
    first_seen_run_id: str | None = None
    resolved_in_run_id: str | None = None
    status: str = "open"  # open, resolved, wontfix, deferred


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    query TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'audit',
    started_at REAL NOT NULL,
    completed_at REAL,
    success INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_duration_seconds REAL,
    nodes_completed INTEGER NOT NULL DEFAULT 0,
    nodes_failed INTEGER NOT NULL DEFAULT 0,
    budget_usd REAL NOT NULL DEFAULT 0,
    config_snapshot TEXT,
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS node_runs (
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    node_name TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    model TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL,
    success INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    duration_seconds REAL,
    attempts INTEGER NOT NULL DEFAULT 1,
    result_hash TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    node_name TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('critical','high','medium','low','info')),
    category TEXT NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    suggested_fix TEXT,
    first_seen_run_id TEXT,
    resolved_in_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','wontfix','deferred'))
);

CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);
CREATE INDEX IF NOT EXISTS idx_node_runs_workflow ON node_runs(workflow_run_id);

CREATE TABLE IF NOT EXISTS model_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    node_type TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0,
    failure INTEGER NOT NULL DEFAULT 0,
    total_executions INTEGER NOT NULL DEFAULT 0,
    avg_latency_seconds REAL NOT NULL DEFAULT 0.0,
    avg_input_tokens REAL NOT NULL DEFAULT 0.0,
    avg_output_tokens REAL NOT NULL DEFAULT 0.0,
    avg_cost_usd REAL NOT NULL DEFAULT 0.0,
    last_failure_reason TEXT NOT NULL DEFAULT '',
    last_used_at REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_model_perf_model ON model_performance(model);
CREATE INDEX IF NOT EXISTS idx_model_perf_node ON model_performance(model, node_type);
"""
