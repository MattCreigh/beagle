# Beagle Steering System

## Overview

The **Steering System** provides mid-workflow control over agentic
execution. It allows operators to correct agent drift, inject new
constraints, adjust priorities, and change execution direction — all
**while a workflow is actively running**.

Steering operates as a **higher-priority overlay** injected into each
agent's prompt before execution. When a steering directive is active,
agents are explicitly instructed to honor it above their default
behavior.

### Problem It Solves

Without steering, a multi-step agentic workflow that deviates from its
intended path can only be stopped and restarted. Steering enables:

- **Course correction**: Redirect an agent that's going off-track
- **Constraint injection**: Add constraints discovered mid-execution
- **Priority shifts**: Elevate or suppress specific concerns
- **Model overrides**: Change the model mid-workflow
- **Context injection**: Provide domain knowledge discovered after the
  workflow started

---

## How Steering Works

Steering directives flow through the system as follows:

```text
Steering Sources → SteeringManager → Merge & Priority Sort → Inject into Prompt
                                                                         ↓
                                                                  Agent Execution
```

1. **Polling**: Before each node executes, the `SteeringManager` polls
   all registered sources.
2. **Merging**: Directives are merged using priority ordering
   (CRITICAL > HIGH > NORMAL > LOW).
3. **Injection**: The resulting `steering_prompt` is injected into the
   agent's system directive as a `<HIGH_PRIORITY_DIRECTIVE>` block.
4. **Enforcement**: Agents are instructed that steering directives
   override all prior instructions.

---

## Source Types

### 1. File Source

Steering directives from a file, typically `<config_root>/steering.md`.

```markdown
<!-- <config_root>/steering.md -->
# Steering Directives

## CRITICAL
- NO DATABASE MIGRATIONS
- Do not modify any production configuration files

## HIGH
- Focus on the authentication module only
- Prioritize security findings over style issues
```

**Features**:

- Auto-reloaded on change (file watcher)
- Supports markdown formatting with priority headers
- Persists across workflow restarts

**Configuration**:

```toml
[steering]
file_path = "<config_root>/steering.md"   # Default path
auto_reload = true                  # Watch for changes
```

### 2. Environment Source

Steering via environment variables. Useful for CI/CD pipelines.

```bash
# Set steering directives via environment
export BEAGLE_STEER_PRIORITY="CRITICAL"
export BEAGLE_STEER_DIRECTIVE="Focus on auth module only"
```

**Supported variables**:

| Variable | Description | Example |
|----------|-------------|---------|
| `BEAGLE_STEER_PRIORITY` | Priority level (CRITICAL, HIGH, NORMAL, LOW) | `CRITICAL` |
| `BEAGLE_STEER_DIRECTIVE` | Directive text | `Focus on auth module only` |

**Note**: Environment steering is evaluated once at workflow start. For
dynamic steering, use File or TUI sources.

### 3. TUI Source

Interactive steering through the Rich TUI dashboard during active workflows.

```bash
beagle run --workflow research --query "Audit auth module"
# TUI dashboard appears — press 'S' to open steering panel
# Type directive and press Enter — applied to next node
```

**Features**:

- Real-time directive injection
- Visual indicator when steering is active
- Directives shown with priority selector
- History of previous directives

### 4. API Source

Programmatic steering via REST/MCP endpoint.

```bash
# MCP tool call
curl -X POST http://localhost:8080/steering/update \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_id": "wf-12345",
    "priority": "HIGH",
    "directive": "Switch to security audit mode for the payment module",
    "source": "api"
  }'
```

**Features**:

- Can be called from CI/CD pipelines
- Supports JSON payload with all steering fields
- Integrated with OpenClaw task queue for deferred steering

---

## Priority & Merging

Directives are merged in priority order:

| Priority | Value | Use Case |
|----------|-------|----------|
| CRITICAL | 4 | Safety constraints, hard blocks ("NO PRODUCTION CHANGES") |
| HIGH | 3 | Important pivots, new constraints discovered mid-workflow |
| NORMAL | 2 | Additional context, suggested focus areas |
| LOW | 1 | Optional hints, preference suggestions |

When multiple sources provide directives at the same priority level,
they are **concatenated** (not overridden). The resulting steering
prompt follows this structure:

```text
<HIGH_PRIORITY_DIRECTIVE>
[CRITICAL] NO DATABASE MIGRATIONS
[CRITICAL] Do not modify production configuration files
[HIGH] Focus on the authentication module only
[NORMAL] Include performance benchmarks in the report
</HIGH_PRIORITY_DIRECTIVE>
```

---

## Examples

### Adding a Constraint Mid-Execution

```bash
# Via environment
export BEAGLE_STEER_PRIORITY="CRITICAL"
export BEAGLE_STEER_DIRECTIVE="Focus on auth module only"

# Via file
echo "## CRITICAL\n- Focus on auth module only" >> <config_root>/steering.md

# Via API
curl -X POST http://localhost:8080/steering/update \
  -d '{"priority": "CRITICAL", "directive": "Focus on auth module only"}'
```

### Changing Model Mid-Workflow

```bash
# Via steering file
echo "## HIGH\n- Switch to the configured model for verification phase" >> <config_root>/steering.md
```

### Injecting Domain Knowledge

```bash
# Via API — discovered that the project uses Flask, not Django
curl -X POST http://localhost:8080/steering/update \
  -d '{
    "priority": "HIGH",
    "directive": "This project uses Flask (not Django). Adjust all import paths and middleware patterns accordingly."
  }'
```

### Pivoting Research Direction

```python
# In TUI, press 'S' and type:
"Pivot: Focus on the payment processing module instead of authentication. Previous findings indicate auth is secure."
```

---

## Integration

### With Constraints

Steering and constraints work together but serve different purposes:

- **Constraints** are declared at workflow start and **persist across
  compaction boundaries**. They are stored in the `ConstraintRegistry`
  and injected into every node prompt.
- **Steering** is injected dynamically mid-workflow and can change
  between nodes. It is injected as a `<HIGH_PRIORITY_DIRECTIVE>` block.

When they conflict, **steering overrides constraints** (steering is
higher priority in the prompt).

### With Guardian

The Guardian approval system evaluates actions against risk levels.
Steering can elevate the risk level of actions that would normally be
auto-approved:

```text
Steering: CRITICAL — NO FILE WRITES

→ Guardian will deny all file write operations regardless of their base risk level.
```

### With Workflow Conditions

Conditional routing in workflows can reference steering state:

```yaml
# In workflow YAML
conditions:
  - if: "steering.priority >= HIGH"
    then: use_verification_model
```

---

## Developer Guide: Adding a Custom Steering Source

To create a custom steering source:

### 1. Subclass `SteeringSource`

```python
from beagle.steering.sources import SteeringSource, SteeringDirective

class WebhookSteeringSource(SteeringSource):
    """Steering source that receives directives via webhook."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._directives: list[SteeringDirective] = []

    def receive_webhook(self, payload: dict):
        """Called by webhook handler."""
        self._directives.append(SteeringDirective(
            priority=payload.get("priority", "NORMAL"),
            content=payload["directive"],
            source="webhook",
        ))

    def poll(self) -> list[SteeringDirective]:
        """Return pending directives and clear the buffer."""
        directives = self._directives.copy()
        self._directives.clear()
        return directives
```

### 2. Register with `SteeringManager`

```python
from beagle.steering.manager import SteeringManager

manager = SteeringManager()
webhook_source = WebhookSteeringSource(endpoint="/webhooks/steering")
manager.register_source("webhook", webhook_source)
```

### 3. Use in Workflow

The `SteeringManager` is automatically injected into the workflow
graph. Directives from your custom source will be polled and merged
before each node execution.

---

## Configuration Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `steering.file_path` | `<config_root>/steering.md` | Path to steering directives file |
| `steering.auto_reload` | `true` | Watch file for changes |
| `steering.max_directives` | `20` | Maximum active directives before pruning |
| `steering.priority_order` | `CRITICAL > HIGH > NORMAL > LOW` | Merging order |
| `BEAGLE_STEER_PRIORITY` | — | Environment variable for priority |
| `BEAGLE_STEER_DIRECTIVE` | — | Environment variable for directive text |
