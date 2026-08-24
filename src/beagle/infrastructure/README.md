# Beagle Docker Infrastructure

Containerized deployment of the Beagle (Autonomous Execution & Cognitive Architecture) workflow using Docker and Orpheus ring buffers for zero-copy IPC.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      SKYLON DEV STACK                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     │
│  │  PLANNER     │────▶│  EXECUTOR    │────▶│  VERIFIER    │     │
│  │  AGENT       │     │  AGENT       │     │  AGENT       │     │
│  │  (Container) │     │  (Container) │     │  (Container) │     │
│  └──────┬───────┘     └──────┬───────┘     └──────┬───────┘     │
│         │     O R P H E U S   R I N G   B U F F E R S            │
│         │                    │                    │              │
│         └────────────────────┼────────────────────┘              │
│                              │                                   │
│                    ┌─────────▼─────────┐                        │
│                    │   ORCHESTRATOR     │                        │
│                    │   (Container)      │                        │
│                    └─────────────────────┘                        │
│                              │                                   │
│                    ┌─────────▼─────────┐                        │
│                    │  Beagle RAG SERVER   │                        │
│                    │  (Hybrid GraphRAG)  │                        │
│                    └─────────────────────┘                        │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Via Skylon (Recommended)

```bash
# Start Beagle orchestration (dev stack)
sk ln app start dev --profile beagle_rag --profile beagle_orchestrator

# Start Beagle agents (agent stack)
sk ln app start agent --profile beagle_agents
```

### Direct Docker Compose

```bash
# Build base image
cd infrastructure
docker build -f Dockerfile.base -t beagle-base:latest .

# Build specific agent
docker build -f Dockerfile.agent \
    --build-arg AGENT_TYPE=planner \
    -t beagle-planner:latest .

# Start orchestrator
docker compose -f /home/server/Servers/server_1/docker_dev_stack/docker-compose.yml \
    --profile beagle_orchestrator --profile beagle_rag up -d

# Start agents
docker compose -f /home/server/Servers/server_1/docker_agent_stack/docker-compose.yml \
    --profile beagle_agents up -d
```

## Stacks

| Stack | Path | Services | Profile |
|-------|------|----------|---------|
| **Dev Stack** | `/home/server/Servers/server_1/docker_dev_stack/` | RAG Server, Orchestrator, VS Code, OpenWebUI | `beagle_rag`, `beagle_orchestrator` |
| **Agent Stack** | `/home/server/Servers/server_1/docker_agent_stack/` | 4 Beagle Agents (planner, executor, verifier, synthesizer) | `beagle_agents` |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKFLOW_ID` | `beagle-server1` | Workflow instance ID |
| `ORPHEUS_INSTANCE` | `beagle-server1` | Orpheus instance name |
| `ORPHEUS_RING_DIR` | `/run/orpheus/nexus` | Ring buffer directory (shared memory) |
| `GOOSE_PROVIDER` | `ollama_cloud` | Goose LLM provider |
| `GOOSE_MODEL` | `gemma3:27b` | Default LLM model |
| `EXECUTION_MODE` | `wrapper` | Agent execution mode: wrapper, goose_direct, server, sleep |

## File Structure

```
infrastructure/
├── Dockerfile.base              # Base image (MCP, langgraph, goose)
├── Dockerfile.agent             # Agent image template
├── agent_entrypoint.sh          # Multi-mode container startup
├── docker_agent_wrapper.py      # Node function wrapper (+ RAG logging)
├── docker_rag_logger.py         # Centralized RAG capture system
├── orpheus_ring_manager.py      # Ring buffer lifecycle mgmt
├── mcp_rag_server.py            # MCP GraphRAG server (LanceDB + Kùzu)
├── mcp_utility_server.py       # Consolidated utility server (code+web+workflow)
├── health_check.py              # Container health verification
└── test_docker_cluster.py       # Integration test suite
```

## Orpheus Ring Buffers

Zero-copy IPC via shared memory ring buffers:

| Ring Name | Purpose |
|-----------|---------|
| `plan_out` | Planner → Executor |

| `execute_out` | Executor → Verifier |
| `verify_out` | Verifier → Synthesizer |
| `synthesize_out` | Synthesizer → Orchestrator |

## RAG Integration

All agent operations are automatically logged to RAG via `docker_rag_logger.py`:

- **Architecture decisions**: Design choices and trade-offs
- **Performance metrics**: Token counts, timing, cost
- **Troubleshooting**: Error patterns and resolutions
- **Configuration changes**: Env var modifications
- **Container lifecycle**: Startup/shutdown events

RAG storage:
- Instance tier: `/opt/beagle/data/instance_rag`
- Main tier: `/opt/beagle/data/main_rag`

## Health Checks

Each container verifies:
1. Goose binary is accessible
2. Orpheus ring directory is writable
3. Agent can write to `/app/state` and `/app/output`
4. Agent-specific recipe files exist (`/app/recipes/`)

## Testing

```bash
# Run integration tests
python3 infrastructure/test_docker_cluster.py

# Test individual health checks
docker exec server_1_beagle_planner python3 /app/infrastructure/health_check.py --agent planner

# Test ring buffer creation
python3 infrastructure/orpheus_ring_manager.py --action init
```

## MCP Servers

### RAG Server (`mcp_rag_server.py`)
- Transport: stdio
- Stores: LanceDB (vector), Kùzu (graph)
- Tools: dense retrieval, AST traversal
- Security: secret scrubbing on all responses

### Utility Server (`mcp_utility_server.py`)
- Transport: stdio
- Tools: run research workflow, build graph
- Orchestrates: planner → executor → verifier → synthesizer

## Deployment Stack Locations

| Component | Skylon Stack | Container Name |
|-----------|--------------|----------------|
| RAG Server | dev | `server_1_beagle_rag_server` |
| Orchestrator | dev | `server_1_beagle_orchestrator` |
| Planner | agent | `server_1_beagle_planner` |
| Executor | agent | `server_1_beagle_executor` |
| Verifier | agent | `server_1_beagle_verifier` |
| Synthesizer | agent | `server_1_beagle_synthesizer` |

## Documentation

- `docs/DOCKER_DEPLOYMENT.md` - Complete deployment guide
- `/home/server/Servers/server_1/STACK_SUMMARY.md` - Stack status overview
- `/home/server/Servers/server_1/config/dockerConfig.toml` - Skylon stack definitions

## Troubleshooting

### Container won't start
```bash
# Check health check
docker exec server_1_beagle_planner python3 /app/infrastructure/health_check.py --agent planner

# Check logs
docker logs server_1_beagle_planner --tail=50

# Verify ring dir
ls -la /run/orpheus/nexus/
```

### RAG server not responding
```bash
# Check Orpheus socket
ls -la /run/orpheus/docker.sock/

# Verify MCP SDK
python3 -c "import mcp; print('MCP OK')"

# Test RAG init
docker exec server_1_beagle_rag_server python3 infrastructure/mcp_rag_server.py --test
```

## License

MIT