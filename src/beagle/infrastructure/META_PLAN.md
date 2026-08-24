# Meta Plan: Docker Containerization of beagle

> **Generated:** 2026-03-26  
> **Status:** IMPLEMENTED

## Executive Summary

This document defines the architecture for containerizing the beagle Beagle system using Docker, with Orpheus ring buffers for high-speed IPC between agent containers.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      SKYLON DEV STACK                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │
│  │  PLANNER     │────▶│  EXECUTOR    │────▶│  VERIFIER    │     │
│  │  AGENT       │◀────│  AGENT       │◀────│  AGENT       │     │
│  │  (Container) │     │  (Container) │     │  (Container) │     │
│  └──────┬───────┘     └──────┬───────┘     └──────┬───────┘     │
│         │                    │                    │              │
│         │    O R P H E U S   R I N G   B U F F E R S             │
│         │    (Shared Memory IPC - Zero Copy)                      │
│         │                    │                    │              │
│         └────────────────────┼────────────────────┘              │
│                              │                                   │
│                    ┌─────────▼─────────┐                        │
│                    │   ORCHESTRATOR     │                        │
│                    │   (Container)      │                        │
│                    └─────────────────────┘                        │
│                                                                 │
│  ┌──────────────┐     ┌──────────────┐                           │
│  │  SYNTHESIZER │     │   ORPHEUS    │                           │
│  │  AGENT       │     │   DAEMON     │                           │
│  │  (Container) │     │  (Ring Mgmt) │                           │
│  └──────────────┘     └──────────────┘                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## DAG Node to Container Mapping

| DAG Node | Container | Role | Orpheus Ring |
|----------|-----------|------|--------------|
| `PlanningPhase` | `beagle-planner` | Research planning | `planner→executor` |
| `ExecutionPhase` | `beagle-executor` | Code search/execution | `executor→verifier` |
| `VerificationPhase` | `beagle-verifier` | Fact checking | `verifier→synthesizer` |
| `SynthesisPhase` | `beagle-synthesizer` | Report generation | `synthesizer→orchestrator` |
| `DAGOrchestrator` | `beagle-orchestrator` | DAG execution control | `orchestrator→all` |

## Phase 1: Analysis & Architecture ✅

### 1.1 Agent Structure Analysis
- **DAG Nodes Identified:** PlanningPhase, ExecutionPhase, VerificationPhase, SynthesisPhase
- **State Object:** `AgentState` dataclass with query, research_plan, raw_execution_context, verified_facts, final_report
- **Transitions:** Sequential DAG with conditional branching

### 1.2 A2A Protocol Mapping
```python
# A2AMessage types map to Orpheus ring operations:
SEND_TASK → Ring write (non-blocking)
GET_TASK  → Ring read (blocking with timeout)
SUBSCRIBE → Ring poll (event-driven)
CANCEL    → Ring clear (signal)
```

### 1.3 Orpheus Ring Buffer Topology
```
Ring Name Format: {workflow_id}:{from_agent}→{to_agent}

Rings:
  beagle:orchestrator→planner      # DAG start signal
  beagle:planner→executor            # Plan delivery
  beagle:executor→verifier          # Execution results
  beagle:verifier→synthesizer        # Verified facts
  beagle:synthesizer→orchestrator   # Final report
  beagle:heartbeat                   # Health monitoring
```

## Phase 2: Docker Image Definitions ✅

### 2.1 Base Agent Image
```dockerfile
FROM python:3.13-slim
# Shared dependencies for all agents
```

### 2.2 Agent Images
- `beagle-base`: Shared dependencies, recipes, skills
- `beagle-planner`: PlanningPhase agent
- `beagle-executor`: ExecutionPhase agent  
- `beagle-verifier`: VerificationPhase agent
- `beagle-synthesizer`: SynthesisPhase agent
- `beagle-orchestrator`: DAG execution control

### 2.3 Shared Volume Strategy
```yaml
volumes:
  - /run/orpheus/{instance}/nexus:/run/orpheus/nexus:rw
  - ./recipes:/app/recipes:ro
  - ./skills:/app/skills:ro
  - ./ai/analysis_reports:/app/output:rw
```

## Phase 3: Orpheus Integration ✅

### 3.1 Ring Buffer Naming Convention
```python
RING_PREFIX = "beagle"


def ring_name(workflow_id: str, from_agent: str, to_agent: str) -> str:
    return f"{RING_PREFIX}:{workflow_id}:{from_agent}→{to_agent}"
```

### 3.2 Message Serialization
- Protocol Buffers or MessagePack for efficient serialization
- FlatBuffers for zero-copy reads (Orpheus native)

## Phase 4: Skylon Dev Stack Integration ✅

### 4.1 docker-compose.yml Structure
- Service definitions for each agent container
- Health checks using Orpheus ring presence
- Dependency ordering via `depends_on`
- Environment injection for instance_name, ring_dir

## Phase 5: Testing & Validation ✅

### 5.1 Test Scenarios
1. Single query through full DAG
2. Concurrent workflow execution
3. Agent failure recovery
4. Orpheus IPC latency benchmarks

## Implementation Files

| File | Purpose |
|------|---------|
| `infrastructure/docker-compose.yml` | Container orchestration |
| `infrastructure/Dockerfile.base` | Base agent image |
| `infrastructure/Dockerfile.agent` | Agent image template |
| `infrastructure/orpheus_agent.py` | Orpheus ring IPC wrapper |
| `infrastructure/agent_entrypoint.sh` | Container startup script |
| `infrastructure/skylon-dev.toml` | Skylon stack configuration |
| `infrastructure/health_check.py` | Container health verification |

## Security Considerations

- **No external firewall needed** - containers on same Docker network
- **Orpheus IPC group** - `orpheus-ipc` group for ring access
- **Read-only recipes/skills** - mounted as `:ro`
- **Secret injection** - via environment variables only
