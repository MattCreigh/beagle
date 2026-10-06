# beagle config — drift diagnostic

## Model-identifier drift (I1)
- distinct model identifiers: 30
- identifiers in >1 file: 5
  - `deepseek-v3.2` -> beagle_core_config/config.toml, coding_agent_config/metaprompts/_archive/self-improvement.yaml, coding_agent_config/metaprompts/self-improvement.yaml
  - `glm-5` -> coding_agent_config/metaprompts/_archive/self-improvement.yaml, coding_agent_config/metaprompts/self-improvement.yaml
  - `minimax-m2.7` -> coding_agent_config/metaprompts/_archive/self-improvement.yaml, coding_agent_config/metaprompts/self-improvement.yaml
  - `minimax-m3` -> beagle_core_config/config.toml, style_guides/guides/beagle_core_directives.toml
  - `minimax-m3:cloud` -> coding_agent_config/metaprompts/tasks/goose_cli_feature_parity.toml, coding_agent_config/metaprompts/tasks/host_optimization.toml, coding_agent_config/metaprompts/tasks/openclaw_controller_refactor.toml, coding_agent_config/metaprompts/tasks/research_comparison.toml, coding_agent_config/metaprompts/tasks/self_improvement_loop_25.toml, coding_agent_config/metaprompts/tasks/skills_audit.toml, coding_agent_config/metaprompts/tasks/zeus_docker_improvements.toml, coding_agent_config/metaprompts/templates/develop.toml, coding_agent_config/metaprompts/templates/research.toml, coding_agent_config/metaprompts/templates/self-improvement.toml, style_guides/guides/_archive/llm_bridge_contract.toml, style_guides/guides/beagle_core_directives.toml

## Allowlist coherence
  - all routing models are in [models.allowed]

## Agent-profile statistics (coding_agent_config/agents.toml)
- profiles: 61
- differing only by prose: 5
- identical to modal: 0

## Credentials on disk
  - none (memory-only delivery upheld)
