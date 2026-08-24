"""Default configuration generation and saving for Goose Agentic Workflow.

Generates the default TOML configuration file content and writes it to disk.
"""

from __future__ import annotations

from pathlib import Path

from .loader import get_config_path


def generate_default_config() -> str:
    """Generate default config file content.

    Returns:
        TOML config string

    """
    return """# Goose Agentic Workflow Configuration (Beagle v12.0)

[orchestrator]
timeout_seconds = 300          # Max time per node execution
validation_timeout_seconds = 60 # Max time for EVH validation
max_retries = 3                # Retry attempts per node
retry_delay = 5.0              # Initial retry delay (exponential backoff)
max_backoff = 60.0             # Maximum backoff delay

[goose]
binary_path = "{GOOSE_BIN}"
# No provider presets — point at any OpenAI-compatible API.
# Examples: ollama_cloud | openai | vllm | litellm  (see README)
default_model = ""            # REQUIRED: your model name, e.g. from your provider
provider = ""                 # e.g. ollama_cloud, openai, ...

# Per-recipe model routing (recipe_name = "model_name")
# Precedence: GOOSE_MODEL env > orchestrator model= param > [models] > default_model
[models]
# Add per-recipe overrides here, e.g.:
# research-planner = "your-model-a"
# python-backend = "your-model-b"

[budget]
default_usd = 10.0             # Default budget per workflow
warn_threshold = 0.8           # Warn at 80% budget usage
hard_limit_usd = 50.0          # Absolute maximum budget

[cache]
enabled = true                 # Enable result caching
ttl_hours = 24                 # Cache TTL in hours
max_size_mb = 100              # Max file cache size
memory_max_entries = 100       # Max memory cache entries

[rate_limit]
requests_per_minute = 60       # Global request rate
tokens_per_minute = 100000     # Global token rate
burst_multiplier = 1.5         # Allow burst above limits

[mcp]
rag_server_binary = "python3"   # Interpreter for MCP server
rag_server_script = "infrastructure/mcp_rag_server.py"
transport = "stdio"             # ONLY stdio permitted (HTTP/SSE blocked)
knowledge_dir = "cache/knowledge_graph"
read_only_runtime = true        # Enforce read-only FDs on data dirs
max_vector_results = 10         # Hard cap on LanceDB top-K
max_graph_hops = 3              # Hard cap on Kùzu traversal depth
max_graph_results = 20          # Hard cap on graph result rows

[logging]
level = "INFO"                 # DEBUG, INFO, WARNING, ERROR
json_format = false            # Use JSON structured logging
log_to_file = true             # Write to log files
max_size_mb = 10               # Max log file size before rotation
backup_count = 5               # Number of rotated logs to keep

[node_timeout]
planning_seconds = 180         # Planning node timeout
execution_seconds = 180        # Execution node timeout
# v1.0.2 (P-fix3): 150 → 300. Rationale: the verification node runs the
# fact-checker / CVCP attacker pass against the synthesized execution
# context. For research/develop/deep-planning the synthesis output can
# reach 80+ KB of context the verifier must read+verify against the
# filesystem, and 150s was the root cause of the
# 'fact-checker: Timeout after 150s' error string surfacing in workflow
# outputs as 'completed_with_errors' even when synthesis succeeded.
# 300s aligns verification with synthesis's budget and matches the
# CVCP attacker fan-out (2 attackers x 150s each, FIX 5).
verification_seconds = 300     # Verification node timeout
synthesis_seconds = 300        # Synthesis node timeout (must scale with DAG breadth; self-improvement fans out to 5+ phases)

[pool]
max_workers = 8                # Max concurrent goose subprocesses
default_timeout_seconds = 300  # Default per-call timeout
backoff_base = 2.0             # Exponential backoff base delay
backoff_max = 60.0             # Maximum backoff delay

[context_threshold]
warning = 0.50                 # Warn at 50% context usage
pre_compact = 0.58             # Beagle-fold-first zone (before goose internal)
compact = 0.70                 # Auto-compact at 70% usage
hard_compact = 0.78            # Phase 4.4: fallback hard threshold
critical = 0.85                # Critical action at 85% usage
max_tokens = 128000            # Context window size
tokens_per_iteration = 8000    # Estimated tokens per iteration
watchdog_seconds = 600         # Phase 4.4: time-based fallback (seconds)

[memory]
working_memory_ttl = 3600      # Working memory TTL (seconds)
episodic_memory_max = 100      # Max episodic memory entries
index_token_budget = 2000      # Token budget for memory index (minimum: 500)
index_prune_strategy = "oldest_first"  # Prune strategy: oldest_first | relevance_weighted | hybrid

[security]
max_query_length = 50000       # Max query length

[output]
truncation_threshold = 40000   # Truncation threshold

[circuit_breaker]
max_circuits = 100             # Max circuits

[orpheus]
default_max_agent_calls = 5    # Max agent calls
max_cvcp_attempts = 3          # Max CVCP attempts
grpo_timeout_seconds = 300     # GRPO timeout

[paths]
# Dynamic path resolution — replaces hardcoded paths throughout the codebase
# Environment variables (WORKSPACE_ROOT, GOOSE_BIN, etc.) take precedence
workspace_root = "{WORKSPACE_ROOT}"
goose_bin = "{GOOSE_BIN}"
venv_root = ""                    # Defaults to workspace_root/.venv
data_root = ""                    # Defaults to ~/.beagle

[behavior]
auto_create_missing_files = true    # Auto-create missing files during workflow execution
safe_file_operations = true         # Use SafeFileWriter for all agent file operations

[mcp_auth]
# SECURITY: HTTP/SSE transport REQUIRES valid tokens. No fallback to insecure mode.
enabled = true                      # Enable auth checks (default: true, DO NOT disable)
# tokens = ["beagle-YOUR-SECURE-TOKEN-HERE"]  # Bearer tokens for HTTP transport
require_https = true                # Require HTTPS for HTTP transport
bind_address = "127.0.0.1"          # Bind to loopback only (REQUIRED)

[mcp_cors]
# SECURITY: No wildcard origins allowed. Empty list = deny all (recommended for stdio).
# allowed_origins = ["https://your-domain.com"]
"""


def save_default_config(path: Path | str | None = None) -> Path:
    """Save default configuration to file.

    Args:
        path: Path to save (uses default if None)

    Returns:
        Path to saved config

    """
    path = get_config_path() if path is None else Path(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_default_config())
    return path
