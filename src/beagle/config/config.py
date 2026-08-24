"""Config module — backward-compatible re-exports.

The configuration module has been split into focused sub-modules:
  - schema.py: All @dataclass class definitions
  - loader.py: Config file loading, validation, and caching
  - env_overrides.py: Environment variable overrides
  - model_routing.py: Model resolution and task complexity assessment
  - defaults.py: Default config generation and saving

This shim re-exports the public API so existing imports keep working.

SP-12: the two ``from .x import *`` statements are gone. A star import hides
which names this shim actually promises — ruff cannot tell whether a name is
re-exported deliberately or leaked by accident, which is why every line here
used to carry a suppression comment. The names are now listed explicitly and
repeated in ``__all__``, so the re-export contract is the code rather than a
comment, and adding a name to a submodule no longer silently widens this
module's public surface.
"""

# Schema: all dataclass definitions
# Defaults
from .defaults import (
    generate_default_config,
    save_default_config,
)

# Env overrides
from .env_overrides import (
    apply_env_overrides,
)

# Loader: config loading, validation, and cached access
# Loader: private name re-exported for the tests that assert on it
from .loader import (
    KNOWN_TOP_LEVEL,
    _validate_config_keys,
    get_config,
    get_config_path,
    load_config,
    reset_config_cache,
)

# Names that reached callers through the old ``from .loader import *`` and are
# genuine API rather than leaked imports. Listed explicitly so the re-export is
# a decision, not a side effect. (The star import also leaked `os`, `re`,
# `logging`, `Path`, `Any`, `dataclass` and friends; nothing imports those from
# here, so they are deliberately no longer re-exported.)
from .model_resolver import get_preset

# Model routing
from .model_routing import (
    COMPLEX_KEYWORDS,
    COMPLEX_PATTERNS,
    COMPLEX_TASK_UPGRADE,
    TRIVIAL_KEYWORDS,
    assess_task_complexity,
    resolve_model,
    resolve_model_for_complex_task,
    resolve_model_for_task,
)
from .paths import get_workspace_root
from .schema import (
    A2AConfig,
    BehaviorConfig,
    BudgetConfig,
    CacheConfig,
    CircuitBreakerConfig,
    ContextThresholdConfig,
    CoordConfig,
    DecompositionConfig,
    EmbedConfig,
    EnsembleConfig,
    EventBusConfig,
    GooseConfig,
    HardwareConfig,
    HealthConfig,
    LearnedRoutingConfig,
    LifecycleConfig,
    LLMConfig,
    LoggingConfig,
    MCPAuthConfig,
    MCPConfig,
    MCPCORSConfig,
    MemoryConfig,
    MemoryConsolidationConfig,
    NodeTimeoutConfig,
    OllamaCloudConfig,
    OrchestratorConfig,
    OrpheusConfig,
    OutputConfig,
    PathsConfig,
    PoolConfig,
    RAGConfig,
    RateLimitConfig,
    ReproducibilityConfig,
    SandboxMicroVMConfig,
    SecurityConfig,
    StateConfig,
    StreamingConfig,
    TimeoutConfig,
    TracingConfig,
    ValidationConfig,
    WorkflowConfig,
    WorkflowDiscoveryConfig,
)

__all__ = [
    "COMPLEX_KEYWORDS",
    "COMPLEX_PATTERNS",
    "COMPLEX_TASK_UPGRADE",
    "KNOWN_TOP_LEVEL",
    "TRIVIAL_KEYWORDS",
    "A2AConfig",
    "BehaviorConfig",
    "BudgetConfig",
    "CacheConfig",
    "CircuitBreakerConfig",
    "ContextThresholdConfig",
    "CoordConfig",
    "DecompositionConfig",
    "EmbedConfig",
    "EnsembleConfig",
    "EventBusConfig",
    "GooseConfig",
    "HardwareConfig",
    "HealthConfig",
    "LLMConfig",
    "LearnedRoutingConfig",
    "LifecycleConfig",
    "LoggingConfig",
    "MCPAuthConfig",
    "MCPCORSConfig",
    "MCPConfig",
    "MemoryConfig",
    "MemoryConsolidationConfig",
    "NodeTimeoutConfig",
    "OllamaCloudConfig",
    "OrchestratorConfig",
    "OrpheusConfig",
    "OutputConfig",
    "PathsConfig",
    "PoolConfig",
    "RAGConfig",
    "RateLimitConfig",
    "ReproducibilityConfig",
    "SandboxMicroVMConfig",
    "SecurityConfig",
    "StateConfig",
    "StreamingConfig",
    "TimeoutConfig",
    "TracingConfig",
    "ValidationConfig",
    "WorkflowConfig",
    "WorkflowDiscoveryConfig",
    "_validate_config_keys",
    "apply_env_overrides",
    "assess_task_complexity",
    "generate_default_config",
    "get_config",
    "get_config_path",
    "get_preset",
    "get_workspace_root",
    "load_config",
    "reset_config_cache",
    "resolve_model",
    "resolve_model_for_complex_task",
    "resolve_model_for_task",
    "save_default_config",
]
