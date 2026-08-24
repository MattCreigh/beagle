# Beagle API Reference

## Overview

Beagle provides multiple APIs for workflow execution, memory management, and monitoring.

## Table of Contents

- [MCP Tools (Goose Extensions)](#mcp-tools-goose-extensions)
- [OpenClaw Task Queue API](#openclaw-task-queue-api)
- [RAG Server API](#rag-server-api)
- [Workflow Server API](#workflow-server-api)
- [Tracking Database API](#tracking-database-api)

---

## MCP Tools (Goose Extensions)

Beagle exposes MCP (Model Context Protocol) tools that can be used from Goose.

### OpenClaw Tools

#### `openclaw_create_task`

Create a new OpenClaw task and optionally start it immediately.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_type` | string | Yes | "workflow", "skill", or "delegate" |
| `spec` | object | Yes | Task specification |
| `constraints` | object | No | Execution constraints |
| `audit_config` | object | No | Audit settings |

**Example:**

```python
result = await openclaw_create_task(
    task_type="workflow",
    spec={
        "workflow": "self-improvement",
        "query": "Optimize memory retrieval performance",
        "model": "your-configured-model"
    },
    constraints={
        "timeout_seconds": 600,
        "budget_usd": 5.0,
        "auto_start": true
    }
)
# Returns: {"task_id": "20260406_123", "status": "running"}
```

#### `openclaw_wait_for_task`

Wait for task completion using event-driven notifications.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID to wait for |
| `timeout_seconds` | int | No | Maximum wait time (default: 300) |
| `poll_interval` | float | No | Fallback polling interval (default: 2.0) |

**Example:**

```python
result = await openclaw_wait_for_task(
    task_id="20260406_123",
    timeout_seconds=600
)
# Returns: {"status": "completed", "result": {...}}
```

#### `openclaw_cancel_task`

Cancel a running or pending task.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `task_id` | string | Yes | Task ID to cancel |
| `reason` | string | No | Reason for cancellation |

**Example:**

```python
await openclaw_cancel_task(
    task_id="20260406_123",
    reason="User requested cancellation"
)
```

#### `openclaw_list_tasks`

List OpenClaw tasks, optionally filtered by status.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `status` | string | No | "pending", "running", "completed", "failed", "cancelled", "all" |
| `limit` | int | No | Maximum results (default: 50) |

**Example:**

```python
tasks = await openclaw_list_tasks(status="running", limit=10)
# Returns: [{"task_id": "...", "workflow": "...", "status": "running"}, ...]
```

---

### RAG Tools

#### `rag_ingest`

Ingest a codebase into RAG for semantic search.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `target_directory` | string | Yes | Path to codebase directory |

**Example:**

```python
result = await rag_ingest("/home/user/myproject")
# Returns: {"files": 42, "chunks": 150, "nodes": 500}
```

#### `rag_search`

Execute hybrid RAG search (vector + graph).

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Natural language query |
| `max_hops` | int | No | Graph traversal depth (default: 2) |
| `top_k` | int | No | Number of results (default: 10) |

**Example:**

```python
results = await rag_search(
    query="how authentication works",
    max_hops=2,
    top_k=5
)
# Returns: {
#   "semantic_anchors": [...],
#   "structural_relations": [...]
# }
```

---

### Workflow Tools

#### `run_beagle_workflow`

Execute an Beagle multi-agent workflow.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Task or query to process |
| `workflow_name` | string | No | Workflow name (default: "research") |
| `budget_usd` | float | No | Maximum budget (default: 10.0) |
| `steering_prompt` | string | No | High-priority directive |

**Available Workflows:**

| Name | Best For | Mode |
|------|----------|------|
| `research` | Investigations, analysis | read-only |
| `develop` | Implementation, fixes | read-write |
| `audit` | Code quality/security audit | read-only |
| `security` | Security hardening | read-write |

**Example:**

```python
result = await run_beagle_workflow(
    query="Implement user authentication with JWT",
    workflow_name="develop",
    budget_usd=25.0
)
# Returns: {"report": "...", "findings": [...], "metrics": {...}}
```

#### `estimate_workflow_cost`

Estimate token usage and cost for a workflow.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Query to estimate |
| `workflow_name` | string | No | Workflow name |

**Example:**

```python
estimate = await estimate_workflow_cost(
    query="Audit authentication module",
    workflow_name="audit"
)
# Returns: {
#   "estimated_tokens": 50000,
#   "estimated_cost_usd": 2.50,
#   "per_phase_breakdown": [...]
# }
```

---

## OpenClaw Task Queue API

REST API for task management.

### Endpoints

#### `POST /api/tasks`

Create a new task.

**Request Body:**

```json
{
  "task_type": "workflow",
  "spec": {
    "workflow": "develop",
    "query": "Fix authentication bug"
  },
  "constraints": {
    "budget_usd": 15.0,
    "timeout_seconds": 1800
  }
}
```

**Response:**

```json
{
  "task_id": "20260406_123",
  "status": "pending",
  "created_at": "2026-04-06T21:00:00Z"
}
```

#### `GET /api/tasks/{task_id}`

Get task status.

**Response:**

```json
{
  "task_id": "20260406_123",
  "status": "completed",
  "result": {
    "report": "...",
    "findings": [...]
  },
  "metrics": {
    "total_tokens": 45000,
    "total_cost_usd": 2.25,
    "duration_seconds": 180
  }
}
```

#### `POST /api/tasks/{task_id}/cancel`

Cancel a task.

**Request Body:**

```json
{
  "reason": "User requested cancellation"
}
```

#### `GET /api/tasks`

List tasks.

**Query Parameters:**

| Name | Type | Description |
|------|------|-------------|
| `status` | string | Filter by status |
| `limit` | int | Max results |
| `offset` | int | Pagination offset |

---

## RAG Server API

### MCP Tools

The RAG server exposes the following MCP tools for use in Goose sessions:

#### `rag_search`

Execute a hybrid RAG search combining vector retrieval and graph traversal.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | string | Yes | — | Natural language search query |
| `max_hops` | int | No | 1 | Graph traversal depth (1-3) |
| `top_k` | int | No | 5 | Number of vector results (1-10) |

**Response:**

```json
{
  "status": "ok",
  "query": "authentication flow",
  "semantic_anchors": [
    {
      "ast_entity_id": "node_123",
      "file": "auth/login.py",
      "node_name": "authenticate",
      "node_type": "FunctionDef",
      "start_line": 45,
      "end_line": 78,
      "content": "def authenticate(user, password): ...",
      "distance": 0.08
    }
  ],
  "structural_relations": [
    {
      "source_node": "authenticate",
      "relationship": "CALLS",
      "target_node": "verify_password",
      "filepath": "auth/utils.py",
      "context_snippet": "def verify_password(...) -> bool: ..."
    }
  ],
  "metadata": {
    "vector_results": 5,
    "graph_relations": 12,
    "max_hops": 2
  }
}
```

#### `rag_status`

Check RAG subsystem health.

**Response:**

```json
{
  "lancedb_available": true,
  "kuzu_available": true,
  "embeddings_available": true,
  "lance_table_loaded": true,
  "kuzu_connected": true,
  "embed_model_loaded": true,
  "data_path": "<rag_data_path>",
  "transport": "stdio",
  "indexed_chunks": 15234
}
```

#### `rag_ingest`

Ingest a codebase into the RAG knowledge base.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `target_directory` | string | Yes | Path to codebase directory |

**Response:**

```json
{
  "status": "ok",
  "files_processed": 42,
  "chunks_created": 150,
  "relations_extracted": 500,
  "errors": [],
  "elapsed_seconds": 12.34
}
```

#### `health_check`

Comprehensive health check of the RAG server.

**Response:**

```json
{
  "status": "healthy",
  "timestamp": 1744635437.484,
  "correlation_id": "b7e9f1a2",
  "checks": {
    "lancedb": {"status": "ok", "table": "ast_code_chunks", "row_count": 15234},
    "kuzu": {"status": "ok", "mode": "read-only", "path": "<rag_graph_path>"},
    "embeddings": {"status": "ok", "model": "your-embedding-model"},
    "cache": {"status": "ok", "entries": 23, "max_size": 100, "utilization_pct": 23.0},
    "memory": {"status": "ok", "max_rss_mb": 312.45, "shared_mb": 15.23, "unshared_mb": 297.22},
    "metrics": {"status": "ok", "total_requests": 89, "success_rate": 97.75},
    "health_check_latency": "0.0156s"
  }
}
```

#### `get_metrics`

Retrieve real-time performance metrics.

**Response:**

```json
{
  "requests": {
    "total": 150,
    "success": 147,
    "error": 3
  },
  "latency": {
    "avg_seconds": 0.2341,
    "min_seconds": 0.0012,
    "max_seconds": 2.4521,
    "total_count": 150
  }
}
```

### HTTP Endpoints (Legacy)

> ⚠️ **Note:** HTTP endpoints are deprecated. Use MCP tools via stdio transport.

#### `POST /api/rag/ingest`

Ingest a codebase.

**Request Body:**

```json
{
  "target_directory": "/home/user/myproject"
}
```

**Response:**

```json
{
  "status": "completed",
  "stats": {
    "files_processed": 42,
    "chunks_created": 150,
    "nodes_created": 500
  }
}
```

#### `POST /api/rag/search`

Search the knowledge base.

**Request Body:**

```json
{
  "query": "authentication flow",
  "max_hops": 2,
  "top_k": 10
}
```

**Response:**

```json
{
  "results": [
    {
      "id": "node_123",
      "content": "def authenticate(user, password): ...",
      "file_path": "auth/login.py",
      "line_start": 45,
      "score": 0.92,
      "related_nodes": ["node_124", "node_125"]
    }
  ]
}
```

#### `GET /api/rag/status`

Check RAG health.

**Response:**

```json
{
  "status": "healthy",
  "lancedb": {"connected": true, "entries": 50000},
  "kuzu": {"connected": true, "nodes": 10000, "edges": 25000},
  "embedding_model": "your-embedding-model"
}
```

---

## Workflow Server API

### MCP Tools

The Workflow server exposes the following MCP tools for use in Goose sessions:

#### `run_beagle_workflow`

Execute an Beagle multi-agent workflow.

**Parameters:**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | string | Yes | — | Task description or query |
| `workflow_name` | string | No | "research" | Workflow to execute |
| `budget_usd` | float | No | 10.0 | Maximum budget in USD |
| `steering_prompt` | string | No | null | High-priority directive |

**Available Workflows:**

- `research` - Multi-phase investigation and synthesis
- `develop` - Implementation and refactoring
- `audit` - Code quality and security review
- `incident` - Debugging and error resolution
- `security` - Security hardening
- `db-migration` - Database migration planning
- `devops` - DevOps orchestration
- `deep-planning` - Complex planning tasks
- `self-improvement` - Codebase self-improvement
- `verify` - Verification and validation
- `delegate-example` - Example delegation workflow

**Response:**

```json
{
  "status": "completed",
  "final_report": "## Research Report\n\n...",
  "completed_nodes": ["planner", "researcher", "synthesizer"],
  "cost_summary": {
    "total_cost_usd": 2.45,
    "total_tokens": 15000
  },
  "errors": []
}
```

#### `list_available_workflows`

List all available workflows with descriptions.

**Response:**

```json
[
  {
    "name": "research",
    "description": "Multi-phase research workflow",
    "phase_count": 5
  },
  {
    "name": "develop",
    "description": "Development workflow with testing",
    "phase_count": 4
  }
]
```

#### `list_agents`

List all available Beagle agents.

**Response:**

```json
[
  {
    "name": "research-planner",
    "description": "Plans research strategy and decomposes tasks"
  },
  {
    "name": "synthesis-writer",
    "description": "Synthesizes findings into comprehensive report"
  }
]
```

#### `route_query_to_workflow`

Analyze a query and recommend the best workflow.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | User query or task |

**Response:**

```json
{
  "recommended_workflow": "develop",
  "confidence": 0.92,
  "reasoning": "Query contains implementation keywords",
  "alternatives": ["audit", "self-improvement"]
}
```

#### `estimate_workflow_cost`

Estimate token usage and cost for a workflow.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query` | string | Yes | Task query |
| `workflow_name` | string | Yes | Workflow name |

**Response:**

```json
{
  "estimated_tokens": 15000,
  "estimated_cost_usd": 2.45,
  "breakdown": {
    "planner": 3000,
    "researcher": 8000,
    "synthesizer": 4000
  }
}
```

#### `get_agent_recipe`

Retrieve the recipe/prompt template for an agent.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `agent_name` | string | Yes | Agent name (e.g., "research-planner") |

**Response:**

```yaml
# Agent Recipe: research-planner
role: Research Strategy Planner
instructions: |
  Analyze the query and decompose into research phases...
```

#### `health_check`

Comprehensive health check of the Workflow server.

**Response:**

```json
{
  "status": "healthy",
  "timestamp": 1744635437.484,
  "correlation_id": "a3f5c2d1",
  "checks": {
    "config": {"status": "ok", "provider": "your-provider", "model": "your-configured-model"},
    "workflows": {"status": "ok", "count": 7},
    "router": {"status": "ok", "routable_workflows": 5},
    "memory": {"status": "ok", "max_rss_mb": 245.32, "shared_mb": 12.45, "unshared_mb": 232.87},
    "metrics": {"status": "ok", "total_requests": 150, "success_rate": 98.0},
    "health_check_latency": "0.0023s"
  }
}
```

#### `get_metrics`

Retrieve real-time performance metrics.

**Response:**

```json
{
  "requests": {
    "total": 150,
    "success": 147,
    "error": 3
  },
  "latency": {
    "avg_seconds": 0.2341,
    "min_seconds": 0.0012,
    "max_seconds": 2.4521,
    "total_count": 150
  }
}
```

### HTTP Endpoints (Legacy)

> ⚠️ **Note:** HTTP endpoints are deprecated. Use MCP tools via stdio transport.

#### `POST /api/workflow/run`

Run a workflow.

**Request Body:**

```json
{
  "query": "Implement user authentication",
  "workflow_name": "develop",
  "budget_usd": 25.0,
  "steering_prompt": null
}
```

**Response:**

```json
{
  "workflow_id": "wf_123",
  "status": "running",
  "estimated_duration_seconds": 300
}
```

#### `GET /api/workflows`

List available workflows.

**Response:**

```json
{
  "workflows": [
    {
      "name": "research",
      "description": "Multi-phase research workflow",
      "phase_count": 5
    },
    {
      "name": "develop",
      "description": "Development workflow with testing",
      "phase_count": 4
    }
  ]
}
```

#### `GET /api/agents`

List available agents.

**Response:**

```json
{
  "agents": [
    {
      "name": "research-planner",
      "description": "Plans research strategy"
    },
    {
      "name": "synthesis-writer",
      "description": "Synthesizes findings into report"
    }
  ]
}
```

---

## Tracking Database API

### Python API

```python
from beagle.tracking import TrackingDatabase, WorkflowRun, NodeRun

# Initialize
db = TrackingDatabase("/data/tracking.db")

# Record a workflow run
run = WorkflowRun(
    id="run_123",
    workflow_name="develop",
    query="Fix authentication bug",
    mode="read-write",
    started_at=time.time()
)
db.run_start(run)

# Record node execution
node = NodeRun(
    id="node_456",
    workflow_run_id="run_123",
    node_name="planner",
    skill_name="develop_planner",
    model="your-configured-model",
    started_at=time.time()
)
db.node_start(node)

# Complete node
node.completed_at = time.time()
node.success = True
node.input_tokens = 1000
node.output_tokens = 500
node.cost_usd = 0.05
db.node_end(node)

# Complete workflow
run.completed_at = time.time()
run.success = True
run.total_cost_usd = 1.25
db.run_end(run)

# Get recent runs
recent = db.get_workflow_runs(limit=10)

# Get run details
run = db.get_workflow_run("run_123")
```

---

## Models Reference

### WorkflowRun

```python
@dataclass
class WorkflowRun:
    id: str
    workflow_name: str
    query: str
    mode: str  # "audit", "develop", "research"
    started_at: float
    completed_at: float | None = None
    success: bool = False
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    node_count: int = 0
```

### NodeRun

```python
@dataclass
class NodeRun:
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
```

### Finding

```python
@dataclass
class Finding:
    id: str
    workflow_run_id: str
    node_name: str
    severity: str  # "critical", "high", "medium", "low", "info"
    category: str  # "bug", "security", "performance", "style"
    title: str
    description: str
    file_path: str | None = None
    line_number: int | None = None
    suggested_fix: str | None = None
    status: str = "open"  # "open", "resolved", "wontfix", "deferred"
```

---

## Error Handling

### Error Response Format

```json
{
  "error": {
    "code": "WORKFLOW_NOT_FOUND",
    "message": "Workflow 'invalid' not found",
    "details": {
      "available_workflows": ["research", "develop", "audit", "security", "incident"]
    }
  }
}
```

### Common Error Codes

| Code | Description |
|------|-------------|
| `WORKFLOW_NOT_FOUND` | Specified workflow doesn't exist |
| `TASK_TIMEOUT` | Task exceeded timeout |
| `BUDGET_EXCEEDED` | Task exceeded budget |
| `INVALID_SPEC` | Task specification invalid |
| `RAG_NOT_INITIALIZED` | RAG system not ready |
| `MODEL_UNAVAILABLE` | Requested model not available |

---

## Webhooks

### Task Completion Webhook

```python
# Configure in config.yaml
webhooks:
  task_completed: "https://your-server.com/webhooks/task"
```

**Payload:**

```json
{
  "event": "task_completed",
  "task_id": "20260406_123",
  "workflow": "develop",
  "status": "completed",
  "metrics": {
    "total_cost_usd": 1.25,
    "total_tokens": 25000
  },
  "timestamp": "2026-04-06T21:05:00Z"
}
```

---

## Rate Limits

| Endpoint | Rate Limit |
|----------|------------|
| `POST /api/tasks` | 10/minute |
| `GET /api/tasks/{id}` | 100/minute |
| `POST /api/rag/search` | 50/minute |
| `POST /api/workflow/run` | 10/minute |

---

## See Also

- [ARCHITECTURE.md](./ARCHITECTURE.md) - System architecture
- [USAGE_EXAMPLES.md](./USAGE_EXAMPLES.md) - Usage examples
- [STEERING.md](./STEERING.md) - Steering system
