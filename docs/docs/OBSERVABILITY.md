# Observability & Monitoring Guide

## Overview

Beagle provides comprehensive observability features including structured
logging, metrics collection, health checks, and distributed tracing. All MCP
servers instrument tool calls with correlation IDs for end-to-end request
tracking.

## Correlation IDs

Every request is assigned a unique correlation ID that flows through all
operations and log messages.

### Format

```text
%(asctime)s [%(name)s] [%(correlation_id)s] %(levelname)s: %(message)s
```

### Example Log Output

```text
2026-04-14 13:17:17,484 [BEAGLE_HYBRID_RAG_SERVER] [a3f5c2d1] INFO: RAG search: investigate authentication... (hops=2, top_k=5)
2026-04-14 13:17:17,892 [BEAGLE_HYBRID_RAG_SERVER] [a3f5c2d1] INFO: RAG search completed in 0.4082s
```

## Metrics Collection

Both MCP servers collect metrics automatically for all tool calls.

### Metrics Tracked

| Metric | Description |
|--------|-------------|
| `requests.total` | Total number of requests |
| `requests.success` | Successful requests |
| `requests.error` | Failed requests |
| `latency.sum` | Sum of all latencies (seconds) |
| `latency.count` | Number of timed requests |
| `latency.min` | Minimum latency |
| `latency.max` | Maximum latency |

### Retrieving Metrics

**MCP Workflow Server:**

```bash
# Via MCP tool
get_metrics()
```

**MCP RAG Server:**

```bash
# Via MCP tool
get_metrics()
```

### Metrics Response Format

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

### Success Rate Calculation

```python
success_rate = success / max(total, 1) * 100
```

## Health Checks

Comprehensive health checks are available on both MCP servers.

### Workflow Server Health Check

**Tool:** `health_check()`

**Checks Performed:**

1. **Config** - Configuration loading and provider/model settings
2. **Workflows** - Workflow loader availability and count
3. **Router** - Routing table status
4. **Memory** - Process memory usage (RSS, shared, unshared)
5. **Metrics** - Request totals and success rate

**Response Format:**

```json
{
  "status": "healthy",
  "timestamp": 1744635437.484,
  "correlation_id": "a3f5c2d1",
  "checks": {
    "config": {
      "status": "ok",
      "provider": "your-provider",
      "model": "your-configured-model"
    },
    "workflows": {
      "status": "ok",
      "count": 7
    },
    "router": {
      "status": "ok",
      "routable_workflows": 5
    },
    "memory": {
      "status": "ok",
      "max_rss_mb": 245.32,
      "shared_mb": 12.45,
      "unshared_mb": 232.87
    },
    "metrics": {
      "status": "ok",
      "total_requests": 150,
      "success_rate": 98.0
    },
    "health_check_latency": "0.0023s"
  }
}
```

### RAG Server Health Check

**Tool:** `health_check()`

**Checks Performed:**

1. **LanceDB** - Vector database connectivity and row count
2. **Kùzu** - Graph database connectivity and mode
3. **Embeddings** - Embedding model availability
4. **Cache** - Cache entry count and utilization
5. **Memory** - Process memory usage
6. **Metrics** - Request totals and success rate

**Response Format:**

```json
{
  "status": "healthy",
  "timestamp": 1744635437.484,
  "correlation_id": "b7e9f1a2",
  "checks": {
    "lancedb": {
      "status": "ok",
      "table": "ast_code_chunks",
      "row_count": 15234
    },
    "kuzu": {
      "status": "ok",
      "mode": "read-only",
      "path": "<rag_graph_path>"
    },
    "embeddings": {
      "status": "ok",
      "model": "your-embedding-model"
    },
    "cache": {
      "status": "ok",
      "entries": 23,
      "max_size": 100,
      "utilization_pct": 23.0
    },
    "memory": {
      "status": "ok",
      "max_rss_mb": 312.45,
      "shared_mb": 15.23,
      "unshared_mb": 297.22
    },
    "metrics": {
      "status": "ok",
      "total_requests": 89,
      "success_rate": 97.75
    },
    "health_check_latency": "0.0156s"
  }
}
```

### Health Status Levels

| Status | Meaning |
|--------|---------|
| `healthy` | All checks passed |
| `degraded` | Some checks warning/non-critical |
| `unhealthy` | Critical check failed |

## Distributed Tracing

### OpenTelemetry Integration

Beagle includes optional OpenTelemetry tracing via `utils/tracing.py`.

**Features:**

- Span decorators for async functions
- Context managers for manual spans
- Configurable exporters (OTLP, console)
- Integration with major observability backends

**Usage:**

```python
from beagle.utils.tracing import trace_async

@trace_async("workflow.execution")
async def run_workflow(query: str):
    ...
```

### Manual Tracing

```python
from beagle.utils.tracing import tracer

with tracer.start_as_current_span("custom.operation") as span:
    span.set_attribute("custom.attr", "value")
    # ... operation ...
```

## Orpheus Event Bus

Beagle dispatches structured events via the Orpheus ring buffer for real-time
observability. All node execution paths — including failure paths — publish
events with full operational context.

### Key Event Types

| Event | Fields | Purpose |
|-------|--------|---------|
| `NodeFailed` | `model`, `error_category`, `stderr_snippet`, `duration_seconds`, `node_phase` | Captures operational telemetry for failed node executions |
| `NodeCompleted` | `model`, `duration_seconds`, `tokens_used` | Tracks successful node completions |
| `BudgetWarning` | `current_cost`, `budget_limit`, `percentage` | Alerts when workflow spending approaches limits |
| `DaemonStarted` | `pid`, `coroutine_count` | Signals daemon process initialization |

### Event Consumption

Events are consumed via `get_event_bus().subscribe()` or the MCP
`openclaw_subscribe_task` tool. The ring buffer supports event-driven
notification patterns, eliminating the need for polling.

---

## Monitoring Dashboard Integration

### Exposed Metrics

All metrics are exposed via MCP tools for integration with dashboards:

1. **get_metrics()** - Real-time metrics on both servers
2. **health_check()** - Comprehensive health status
3. **rag_status()** - RAG subsystem status

### Prometheus Integration (Future)

Metrics can be exported to Prometheus via:

- Custom exporter scraping MCP tool output
- Direct Prometheus client integration (planned)

### Alerting Thresholds

Recommended alerting thresholds:

| Metric | Warning | Critical |
|--------|---------|----------|
| Success Rate | < 95% | < 90% |
| Avg Latency | > 2s | > 5s |
| Memory RSS | > 500MB | > 1GB |
| Cache Utilization | > 80% | > 95% |
| Health Check Latency | > 100ms | > 500ms |

## Log Aggregation

### Structured Logging

All logs include:

- Timestamp (ISO 8601)
- Logger name
- Correlation ID
- Log level
- Message

### Log Collection

Logs can be collected via:

- Stdout/stderr capture (stdio transport)
- File logging (configure in basicConfig)
- Centralized logging (Fluentd, Logstash)

### Example Log Aggregation Config (Fluentd)

```xml
<match beagle.*>
  @type elasticsearch
  host elasticsearch.local
  port 9200
  index_name beagle-logs
  type_name _doc
</match>
```

## Performance Monitoring

### Key Performance Indicators (KPIs)

| KPI | Target | Measurement |
|-----|--------|-------------|
| RAG Search Latency | < 500ms | `rag_search` metric |
| Workflow Execution | < 30s | `run_beagle_workflow` metric |
| Cache Hit Rate | > 60% | Manual calculation |
| Success Rate | > 98% | `requests.success / requests.total` |

### Cache Performance

**RAG Cache:**

- Size: 100 entries (LRU)
- TTL: 300 seconds
- Hit rate: Monitor via metrics

**Embedding Cache:**

- Size: 512 entries (LRU)
- Shared across instances
- Hit rate: Monitor via metrics

## Troubleshooting

### High Latency

1. Check cache hit rates
2. Review RAG ingestion status
3. Monitor embedding API latency
4. Check database connection health

### High Error Rate

1. Review error logs with correlation IDs
2. Check dependency availability (LanceDB, Kùzu)
3. Verify configuration
4. Monitor resource usage (memory, CPU)

### Memory Issues

1. Call `clear_rag_cache()` to clear RAG cache
2. Call `clear_embedding_cache()` to clear embedding cache
3. Monitor `health_check().checks.memory`
4. Review garbage collection triggers

## Related Documentation

- [SECURITY.md](SECURITY.md) - Security features and validation
- [API.md](API.md) - Complete API reference
- [ARCHITECTURE.md](ARCHITECTURE.md) - System architecture
- [PERFORMANCE.md](PERFORMANCE.md) - Performance optimization guide
