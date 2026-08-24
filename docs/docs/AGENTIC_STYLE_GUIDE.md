# The Architectonics of Agentic Orchestration

## A Style Guide for Programmatic and Evolvable Context Engineering

---

## How this guide applies to Beagle

This document is the canonical architectural style guide for the Beagle project.
It defines the field-wide norms — XML-as-substrate, Agentic Context Engineering,
the Tool-Environment-Agent protocol, FSM governance, System-2 deliberation,
Cross-Verification, Context Folding, and Skill modules — that the Beagle
codebase implements and must continue to align with.

When designing new modules, reviewing PRs, or auditing the codebase, treat the
principles below as load-bearing. Beagle's existing subsystems are concrete
instantiations of these patterns:

| Concept in this guide               | Beagle implementation                                                          |
| ----------------------------------- | ------------------------------------------------------------------------------ |
| XML as cognitive substrate          | `src/beagle/core/orchestrator/system_directive.py`, `src/beagle/style_guides/injector.py` |
| Agentic Context Engineering (ACE)   | `src/beagle/context/context_compaction_hook.py`, `src/beagle/context/post_compaction_rehydration.py`, `src/beagle/context/compressed_store.py` |
| Tool-Environment-Agent (TEA)        | `src/beagle/core/tool_pool.py`, MCP servers under `src/beagle/infrastructure/`, `src/beagle/blocks/` |
| System-2 deliberation               | DAG planning to execution to verification to synthesis pipeline in `src/beagle/core/` |
| FSM-governed loops (L-V-V-V-A-R-C)  | `src/beagle/core/autonomous_orchestrator.py`, `src/beagle/core/orchestrator/state_manager.py` |
| Cross-Verification Collaboration    | CVCP in `src/beagle/core/graph.py` (adversarial cross-validation node)         |
| Context Folding                     | `src/beagle/core/turboquant.py`, `src/beagle/context/context_integration.py`   |
| Skill modules (RSPL/SEPL)           | `src/beagle/style_guides/`, `src/beagle/blocks/`, `src/beagle/context/recipe_agent_bridge.py` |
| Edge-aware token discipline         | Per-model circuit breakers, `src/beagle/utils/subprocess_pool.py`, remote LLM routing |

For project-specific coding rules (line length, exception handling, datetime,
UUID), see `CLAUDE.md`. For runtime per-file-extension style injection rules,
see `src/beagle/style_guides/guides/*.toml`. This guide is the upstream
philosophy; those are the downstream enforcements.

---

## Paradigmatic Shifts in Cognitive Architecture and Large Language Model Interaction

The evolution of interaction with large language models has transitioned from
the heuristic adjustments of early prompt engineering to a rigorous,
multidisciplinary field defined as context engineering. Historically, the
primary mode of operation relied upon stochastic natural language strings to
elicit desired behaviors, a process often fraught with inconsistency and a
lack of deterministic control. However, the emergence of agentic workflows —
where multiple models collaborate to plan, execute, and refine tasks — demands
a move toward declarative programmatic logic and structured cognitive
scaffolds. This architectural pivot is driven by the recognition that for
an agent to perform reliably in production environments, it must function not
as a conversational partner, but as a stateful, autonomous system governed
by standardized protocols.

The current cutting edge in the domain of agentic prompting is characterized
by the intersection of formal logic, software engineering principles, and
advanced reinforcement learning techniques. Frameworks such as the Model
Context Protocol (MCP) and the Tool-Environment-Agent (TEA) protocol
represent the industry's drive toward modular expertise, where skills are
packaged as composable, portable, and dynamically loadable modules. This
development reflects a broader recognition that as agents move from research
prototypes to enterprise-grade deployments, the focus must shift from
monolithic intelligence to modular expertise that survives the transient
nature of context windows.

| Paradigm                | Interaction Mechanism      | State Management            | Optimization Target              |
| ----------------------- | -------------------------- | --------------------------- | -------------------------------- |
| Traditional Prompting   | Natural language prose     | Transient / Ephemeral       | Surface-level response quality   |
| Context Engineering     | Structured tokens / XML    | Managed / Persistent        | Utility-to-token efficiency      |
| Agentic Orchestration   | Declarative FSM / Protocols| Deterministic / Evolvable   | State mutation and task success  |

This transition is underpinned by the implementation of System-2 reasoning, a
deliberative mode of thought that mimics human logical processing by mapping
multiple trajectories before committing to an action. By integrating external
tool-using agents and structured memory, modern frameworks ensure coherence
in long reasoning chains, effectively addressing the limitations of
"System-1" thinking, which is prone to hallucination and logical drift.

---

## XML as the Primary Cognitive Substrate for Instruction Isolation

A fundamental requirement for high-performance agentic prompts is the use of
XML as a semantic separator. Research indicates that structured markup
provides superior context isolation and attention steering compared to plain
text or markdown. XML tags function as explicit boundaries that the model
can parse unambiguously, distinguishing between background information,
operational constraints, and user-supplied data. This isolation is critical
for preventing "context bleed," where instructions inadvertently merge with
input data, leading to sub-optimal execution or security vulnerabilities like
prompt injection.

### The Semantic Role of Namespaced Markup

The efficacy of XML in agentic prompting is not merely a matter of formatting
but is deeply rooted in the training data of frontier models. Models have been
explicitly fine-tuned to treat XML tags as semantic separators, allowing them
to pay closer attention to content inside specific tags relative to adjacent
free text. Furthermore, the use of namespaced XML (for example, `<invoke>`)
provides a layer of isolation that ensures user input containing similar tags
does not collide with the agent's internal tool-calling mechanics. This
structural hierarchy allows for the nesting of tags, such as placing
`<constraints>` inside a `<task>` block, which yields more predictable
outputs than a linear list of requirements.

### Functional Taxonomy of XML Tagging Strategies

To optimize the utility of the context window, tags should be descriptive and
consistent across a prompt library. The following table delineates the
functional purpose of standard XML tags used in SOTA agentic prompting.

| Tag Identifier            | Architectural Function | Contextual Impact                                                  |
| ------------------------- | ---------------------- | ------------------------------------------------------------------ |
| `<identity>`              | Role Definition        | Establishes base heuristics and behavioral altitude.               |
| `<directive>`             | Primary Goal           | Sets the immutable objective for the execution cycle.              |
| `<execution_paradigm>`    | Logic Framework        | Defines the governing principles (for example, TEA, FSM).          |
| `<deliberation_matrix>`   | Cognitive Workspace    | Segregates intermediate reasoning from final artifacts.           |
| `<guardrails>`            | Safety Rails           | Implements critical "MUST NOT" constraints and security policies.  |
| `<compiled_artifact>`     | Output Protection      | Encloses executable code to prevent parser collisions.             |

The move toward "schema-first" prompt engineering further enhances reliability
by specifying exact output contracts. By specifying a JSON schema within an
XML-delimited `<output_schema>` block, prompts can guarantee output shapes
and eliminate the "reliability tax" associated with manually parsed JSON.

---

## Agentic Context Engineering (ACE) and the Self-Improving Playbook

The most significant challenge in long-horizon agentic systems is the
phenomenon of context collapse, where iterative summarizing and rewriting
erode detailed, domain-specific knowledge over time. Agentic Context
Engineering (ACE) addresses this by treating context not as a static input
but as an evolving "playbook" that accumulates and refines strategies through
a modular process of generation, reflection, and curation.

### Mechanics of Structured Delta Updates

ACE avoids global context rewrites by implementing structured, incremental
"delta" updates. Instead of replacing the entire system prompt, the agent
generates localized insertions or replacements — often represented as
bullet-level modifications — that integrate new lessons learned from
execution feedback. This methodology ensures that high-value, repeatable
strategies are preserved while one-off noise or duplicate information is
ignored.

The ACE framework is typically decomposed into four specialized roles:

- **Generator** — produces candidate reasoning trajectories or
  problem-solving traces based on current queries.
- **Reflector** — acting as a critic, compares successful and unsuccessful
  trajectories to distill concrete, domain-specific insights.
- **Curator** — integrates distilled insights into the global playbook using
  deterministic merging and semantic de-duplication.
- **Pruner** — synthesizes and compresses rules to prevent the playbook from
  becoming bloated, merging overlapping strategies into concise master rules.

### Efficiency Metrics and Causal Implications

Empirical evaluations of the ACE framework demonstrate significant gains in
both accuracy and efficiency. By focusing on localized updates, ACE reduces
adaptation latency by an average of 86.9% compared to traditional prompt
optimization methods. Furthermore, the use of semantic embedding similarity
to de-duplicate rules ensures that the playbook remains scalable, preventing
the cognitive overhead associated with an over-inflated prompt.

| Benchmark Task              | Latency Reduction | Rollout Savings    | Accuracy Gain |
| --------------------------- | ----------------- | ------------------ | ------------- |
| Agent Tasks (AppWorld)      | 82.3%             | 75.1%              | +17.1%        |
| Financial Reasoning (FiNER) | 91.5%             | 83.6% (Tokens)     | +8.6%         |
| General Agent Baseline      | 86.9% (Avg)       | Significant        | +10.6%        |

The causal relationship between ACE and system performance is rooted in its
ability to handle "negative evidence" — the systematic analysis of failures.
Traditional prompts often focus only on desired behaviors, but ACE's
reflector module explicitly identifies pitfalls, allowing the agent to
develop robust error-handling routines that are persisted across session
boundaries.

---

## The Tool-Environment-Agent (TEA) Protocol: A Unified Abstraction

A foundational element of modern agent construction is the Tool-Environment-
Agent (TEA) protocol. This unified abstraction models environments, agents,
and tools as first-class, versioned resources with explicit lifecycles. The
TEA protocol addresses the fragmentation of context across prompts and logs
by standardizing cross-entity lifecycle semantics and run-indexed context
capture.

### Principal Transformations and Action Spaces

The power of the TEA protocol lies in its ability to perform "protocol
transformations," allowing computational entities to adapt their functional
scope based on task demands. These transformations enable a dynamic
orchestration of resources where a collection of tools can be elevated into a
coherent environment, or an entire multi-agent workflow can be encapsulated
as an atomic tool.

The identified fundamental categories of protocol transformations include:

- **Tool-to-Environment (T2E)** — encapsulating a development toolkit as a
  programming environment governed by shared state.
- **Agent-to-Tool (A2T)** — transforming a complex agent capable of deep
  research into a single callable function for a higher-order orchestrator.
- **Environment-to-Agent (E2A)** — empowering a static environment with
  autonomous planning and management capabilities.

### Orchestration and Context Fragmentation

By organizing tools into coherent environments — for example, grouping
discrete file operations into a managed file system — the TEA protocol
reduces context fragmentation and management overhead. This structure allows
for "tree-structured expansion," where a central planner routes sub-tasks to
domain-specific agents that expose only a curated toolset and context
relevant to their specific domain. This localization of decisions ensures
that the orchestrator's context footprint remains bounded, preventing the
performance degradation associated with "context explosion" in complex
tasks.

---

## System-2 Deliberation and the Maximization of Test-Time Compute

Agentic reasoning is increasingly defined by the integration of "System-2"
thinking, a mode of operation that prioritizes deliberative compute over
immediate response. This involves the model allocating more tokens and time
to internal reasoning processes before committing to an external action or
environment mutation.

### Preference-Based Process Reward Models (PPRM) and GRPO

The optimization of System-2 reasoning is often achieved through
Preference-Based Process Reward Models (PPRM) and Group Relative Policy
Optimization (GRPO). These methods reward the model not just for the final
answer, but for the correctness and efficiency of each step in its internal
chain-of-thought. In an agentic prompt, this is implemented by enforcing a
"Divergent Generation" step, where the model must map a minimum of three
structurally distinct algorithmic or architectural approaches before
selecting the one with the maximum cumulative stepwise preference score.

### The Deliberation Matrix and Internal I/O Simulation

A critical design pattern in System-2 prompts is the isolation of logic
traces within a `<deliberation_matrix>` tag. This ensures that the model's
internal "brainstorming" — including potential pitfalls, variable assignments,
and loop initializations — does not contaminate the final output schema.
Furthermore, the model is instructed to perform an internal I/O simulation,
tracing the state of its highest-scoring algorithm using synthetic boundary
data to identify potential errors before they occur in the host environment.

The mathematical ensembling of reasoning paths — weighting candidate
trajectories by their associated process reward — significantly boosts the
reliability of the agent in complex domains such as coding, mathematics, and
logic reasoning.

---

## Finite State Machine (FSM) Governance and Loop Determinism

To ensure reliability in production environments, agentic workflows are
increasingly modeled as Finite State Machines (FSM). This approach moves
away from monolithic, "black-box" execution toward a structured and
predictable framework for managing state-dependent workflows.

### The L-V-V-V-A-R-C Deterministic Loop

SOTA agents frequently employ a deterministic loop, such as the
**Locate-Validate-Validate-Validate-Apply-Report-Cleanup** (L-V-V-V-A-R-C)
cycle. This loop enforces a rigorous sequence of checks before any persistent
file mutation is allowed:

1. **Locate** — parse headers and read initial content to establish a baseline.
2. **Validate Idempotency** — check if the desired changes are already present
   to avoid redundant operations.
3. **Validate Reconstruction** — algorithmic in-memory reconstruction of the
   target file content.
4. **Validate Extrinsic** — run external linters or validators on a temporary
   staged payload.
5. **Apply** — atomic write of the validated content using backup-and-replace
   mechanisms.
6. **Report and Cleanup** — final status aggregation and purging of temporary
   artifacts.

### FSM Transition Logic and Global Barriers

The operational logic of the FSM is governed by a transition function in
which the central controller manages transitions between phases such as
`REPLAN`, `GET_ACTION`, `QUALITY_CHECK`, and `EXECUTE_ACTION`. In multi-agent
settings, coordinator agents insert global barriers at phase boundaries to
ensure synchronization, preventing race conditions and maintaining the
integrity of the shared execution context.

| FSM State        | Trigger Condition       | Outcome / Transition                                  |
| ---------------- | ----------------------- | ----------------------------------------------------- |
| `PENDING`        | Task initiation         | Context construction and scaffold retrieval.          |
| `GENERATING`     | Context readiness       | Autoregressive action generation (trainable state).   |
| `INTERACTING`    | Action completed        | Execution via tool or environment interface.          |
| `QUALITY_CHECK`  | Observation received    | Determination of success; loop or terminate.          |
| `TERMINATED`     | Goal met or fatal error | Trajectory finalization and reward association.       |

This tiered reasoning architecture provides the flexibility and generalization
required for complex tasks like desktop automation while maintaining strict
quality control and predictable behavior.

---

## Programmatic Frameworks: DSPy Signatures and Modular Pipelines

The future of agentic prompting is moving from manual string manipulation
toward programmatic artifacts that are compiled and optimized against data.
DSPy (Declarative Self-improving Python) exemplifies this shift by separating
the program's flow from its parameters (prompts and weights).

### Signatures as Declarative Interfaces

A DSPy signature is a typed definition of a task's input and output fields,
accompanied by an optional instruction. By using a signature, the developer
defines what the task is (for example, `context, question -> answer`) without
prescribing how the model should be prompted. This declarative setup decouples
the task's intent from specific model quirks, allowing the system to scale
naturally across different language models and inference strategies.

### Composable Modules and Self-Improving Optimizers

DSPy modules (such as `Predict`, `ChainOfThought`, `ReAct`, or
`ProgramOfThought`) act as functional units that implement specific inference
strategies for a given signature. These modules serve as "learning targets"
for optimizers, which are algorithms designed to improve prompt performance
based on examples and metrics.

| DSPy Optimizer       | Optimization Mechanism                              | Use Case                                |
| -------------------- | --------------------------------------------------- | --------------------------------------- |
| `BootstrapFewShot`   | Generates few-shot examples from the teacher model. | Improving reasoning in small models.    |
| `COPRO`              | Iterative prompt refinement based on metric deltas. | Optimizing system instructions.         |
| `MIPROv2`            | Multi-prompt ensembling and Bayesian optimization.  | Complex, multi-stage pipelines.         |
| `BootstrapFinetune`  | Distills reasoning traces into model weights.       | Moving from prompt to fine-tuning.       |

This approach transforms prompts from immutable strings into optimizable
parameters, enabling AI engineering teams to build modular, testable, and
reliable systems that iterate far faster than "vibe-based" manual
prompting.

---

## Cross-Verification Protocols and Adversarial Testing

Reliability in agentic code generation and system mutation is enforced
through the Cross-Verification Collaboration Protocol (CVCP). This framework
addresses the shortcomings of single-agent systems by integrating symmetry
detection and adversarial testing into the validation cycle.

### Round-Trip Review Protocol (RTRP) and RTRP Audit Logs

The RTRP mechanism functions as a multi-agent feedback loop where a
secondary agent (the "Adversary") attempts to find flaws, security
vulnerabilities, or logical contradictions in the primary agent's proposed
trajectory. The results are captured in an `rtrp_audit_log`, which provides
detailed feedback on issues like cyclomatic complexity, insecure
serialization, or improper exception swallowing. If the audit log indicates
a failure, the System-2 deliberation module is triggered for a forced
revision cycle, typically hard-capped at 3 iterations to prevent livelocks.

### The POPPER Framework for Hypothesis Validation

For tasks involving free-form natural language hypotheses, the POPPER
framework provides a statistically rigorous method for validation. Inspired
by Karl Popper's principle of falsification, it employs an Experiment Design
Agent to identify measurable implications (sub-hypotheses) and an Experiment
Execution Agent to implement tests. This framework uses "e-values" to
aggregate evidence from multiple dependent tests while strictly controlling
the Type-I error rate, providing a scalable and automated approach to
rigorous validation.

This dynamic, statistically sound decision-making process ensures that agents
do not reject true null hypotheses or accept hallucinated claims.

---

## Context Folding and Memory Lifecycle Management

In long-horizon tasks, the rapid accumulation of interaction history can lead
to context saturation and degraded model performance. Context folding methods
are algorithmic techniques that compress sequential interaction history into
high-fidelity, actionable summaries at appropriate milestones.

### Active Context Folding vs. Passive Summary

Unlike passive document summarization, context folding is an active,
policy-driven modification of the agent's working context. The agent
proactively triggers folding at stage boundaries — for example, after
completing a file system search or a code compilation — and collapses the
raw execution logs into a mathematically condensed `<state_payload>`. This
payload is transmitted back to the root orchestrator, while the raw logs are
permanently purged from the active token window.

### Memory Substrates and Neural Long-Term Memory

The memory design for modern agents has shifted from isolated prompts to a
structured context workspace consisting of three tiers:

- **Sensory / Short-Term Memory** — high-fidelity interaction history of the
  current turn.
- **Working Memory** — condensed history of the current subtask or stage.
- **Procedural / Long-Term Memory** — stable task semantics and evolved
  strategies accumulated in the playbook.

Emerging hybrid architectures, such as "Titans" or "Memory as Context"
(MAC), integrate a neural long-term memory module that updates its own
parameters during inference, allowing the agent to retrieve historical
abstractions far beyond the limits of standard transformer attention blocks.

---

## Optimization for Edge Inference and Resource Bottlenecks

Production agents often operate on low-powered edge inference interfaces with
strict hardware bottlenecks. This environment necessitates a "ruthless"
architectural audit to identify project bloat and redundant logic that may
increase latency or token costs.

### Token Management and API Batching Strategies

To optimize resource usage, agents must minimize "chatter" — explanation or
conversational filler — and default to concise, structured responses. For
multi-turn tasks, the agent should spawn multiple sub-agents in the same turn
to fan out across items (for example, reading multiple files simultaneously),
rather than executing them sequentially. This parallelization, coupled with
aggressive context folding, ensures that long-horizon reasoning remains
stable and scalable under a bounded context budget.

### Hardware-Aware Context Construction

Edge deployment requires agents to be "Environment Aware," assuming no local
compute is available for heavy models except for local embedding models.
Consequently, all heavy LLM inference must rely on remote APIs, placing a
premium on payload size and API batching. The use of "deterministic delta
updates" (ACE) is particularly effective here, as it reduces adaptation
latency by up to 90% by only transmitting the necessary strategic
modifications rather than re-sending the entire context.

> **Beagle application:** This is the operating regime of this codebase.
> All heavy inference goes through a remote endpoint; only embeddings run
> locally. Bloat or redundant logic is a P0 issue, not a polish item. The
> project's hardware contract is captured in `CLAUDE.md`.

---

## Advanced Orchestration Patterns: AgentGraphs and Skill Modules

By late 2025, agentic prompting has evolved from simple "ReAct" loops into
sophisticated agent graphs where prompts define node behaviors, edge
conditions, and state transitions. This represents the maturation of "skill
engineering," where procedural expertise is organized into standardized,
composable patterns.

### Skills as Composable Modules

A "skill" is a higher-order abstraction that bundles instructions, workflow
guidance, executable scripts, and metadata into a dynamically loadable
module. This allows organizations to encode institutional knowledge in a form
that survives personnel turnover, serving as a digital analogue of standard
operating procedures. Skills are managed via the Resource Substrate Protocol
Layer (RSPL), which treats agent components (prompts, tools, memory) as
versioned resources with explicit lifecycles.

| Skill Component   | Role in Orchestration                | Lifecycle Phase           |
| ----------------- | ------------------------------------ | ------------------------- |
| Prompt Module     | Defines task intent and persona.     | Initialized / Retrieved   |
| Tool Bundle       | Provides executable capabilities.    | Bound to Environment      |
| Workflow DAG      | Orchestrates node transitions.       | Executed / Monitored      |
| Observation Log   | Captures execution artifacts.        | Folded / Purged           |
| Feedback Hook     | Triggers self-evolution loop.        | Committed / Versioned     |

### Self-Evolution Protocol Layer (SEPL)

The SEPL layer provides a formal operator algebra that governs how an agent
reflects on its performance and proposes changes to its internal state. If
an execution trace indicates a failure, the SEPL loop is triggered to
autonomously refine the reasoning prompt or adjust the global plan. Because
these changes are managed as versioned resources, the agent can try multiple
implementations in parallel, selecting the one that performs best on a
specific task through competitive ensembling.

---

## Conclusion: The Architecture of Future Agentic Standards

The transition from stochastic prompt engineering to deterministic agentic
orchestration is a fundamental shift in how large language models are
deployed in complex, real-world environments. The convergence on standards
such as the TEA protocol, ACE-driven context playbooks, and System-2
deliberative loops marks the end of "vibe-based" development and the
beginning of rigorous AI engineering. By treating agents as stateful,
evolving systems and utilizing XML as a structured cognitive substrate,
developers can create architectures that are not only more reliable but
also capable of autonomous self-improvement.

The causal link between structured, programmatic prompting and task success
is now well-established. As models continue to evolve toward larger context
windows and more sophisticated reasoning capabilities, the ability to manage
those resources — through proactive context folding, deterministic state
mutation, and adversarial validation — will be the primary differentiator of
high-performance agentic systems. The future of the field lies in the
"delicate art and science" of context engineering, where the goal is to
provide the model with the minimal set of information that fully outlines
expected behavior while maintaining the flexibility to adapt to dynamic,
long-horizon task demands.

---

## Works Cited

1. Effective context engineering for AI agents — Anthropic.
   [https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
2. Mastering Prompt Engineering in 2026 — Coditude.
   [https://www.coditude.com/insights/mastering-prompt-engineering-in-2026/](https://www.coditude.com/insights/mastering-prompt-engineering-in-2026/)
3. ProRefine: Inference-Time Prompt Refinement with Textual Feedback — arXiv.
   [https://arxiv.org/html/2506.05305v3](https://arxiv.org/html/2506.05305v3)
4. LLM-based Agentic Reasoning Frameworks: A Survey from Methods to Scenarios — arXiv.
   [https://arxiv.org/html/2508.17692v1](https://arxiv.org/html/2508.17692v1)
5. Agent Skills for Large Language Models: Architecture, Acquisition, Security, and the
   Path Forward — arXiv. [https://arxiv.org/html/2602.12430v3](https://arxiv.org/html/2602.12430v3)
6. Autogenesis: A Self-Evolving Agent Protocol — alphaXiv.
   [https://www.alphaxiv.org/overview/2604.15034](https://www.alphaxiv.org/overview/2604.15034)
7. AgentOrchestra: Orchestrating Multi-Agent Intelligence with the Tool-Environment-Agent
   (TEA) Protocol — arXiv. [https://arxiv.org/html/2506.12508v5](https://arxiv.org/html/2506.12508v5)
8. Rethinking Memory Mechanisms of Foundation Agents in the Second Half: A Survey —
   arXiv. [https://arxiv.org/html/2602.06052v3](https://arxiv.org/html/2602.06052v3)
9. A Streamlined Framework for Enhancing LLM Reasoning with Agentic Tools — ACL
   Anthology. [https://aclanthology.org/2025.acl-long.1383/](https://aclanthology.org/2025.acl-long.1383/)
10. open-thought/system-2-research: System 2 Reasoning Link Collection — GitHub.
    [https://github.com/open-thought/system-2-research](https://github.com/open-thought/system-2-research)
11. Agentic Context Engineering: Evolving Contexts for Self-Improving Language
    Models — arXiv. [https://arxiv.org/html/2510.04618v3](https://arxiv.org/html/2510.04618v3)
12. Agentic Context Engineering (ACE): Building Self-Correcting AI Workflows — Medium.
    [https://medium.com/@anvesha6496/agentic-context-engineering-ace-building-self-correcting-ai-workflows-942ea406f9db](https://medium.com/@anvesha6496/agentic-context-engineering-ace-building-self-correcting-ai-workflows-942ea406f9db)
13. Agentic Context Engineering (ACE) — Emergent Mind.
    [https://www.emergentmind.com/topics/agentic-context-engineering-ace](https://www.emergentmind.com/topics/agentic-context-engineering-ace)
14. Evolve your language agent with Agentic Context Engineering (ACE) — GitHub.
    [https://github.com/ace-agent/ace](https://github.com/ace-agent/ace)
15. AgentOrchestra: Orchestrating Hierarchical Multi-Agent Intelligence with the
    Tool-Environment-Agent (TEA) Protocol — arXiv.
    [https://arxiv.org/html/2506.12508v4](https://arxiv.org/html/2506.12508v4)
16. AgentOrchestra: Orchestrating Hierarchical Multi-Agent Intelligence with the
    Tool-Environment-Agent (TEA) Protocol — OpenReview.
    [https://openreview.net/forum?id=YcnKdeI9pp](https://openreview.net/forum?id=YcnKdeI9pp)
17. OpenTinker: Separating Concerns in Agentic Reinforcement Learning — arXiv.
    [https://arxiv.org/html/2601.07376v1](https://arxiv.org/html/2601.07376v1)
18. Agentic Lybic: Multi-Agent Execution System with Tiered Reasoning and
    Orchestration — arXiv. [https://arxiv.org/html/2509.11067v1](https://arxiv.org/html/2509.11067v1)
19. Design Patterns + Spring AI + Apache Camel: Building Configurable, Generic Java
    Workflows — Medium.
    [https://medium.com/@akshat.available/design-patterns-spring-ai-apache-camel-building-configurable-generic-java-workflows-a55d84a7f1f1](https://medium.com/@akshat.available/design-patterns-spring-ai-apache-camel-building-configurable-generic-java-workflows-a55d84a7f1f1)
20. Anyprefer: An Agentic Framework for Preference Data Synthesis — OpenReview.
    [https://openreview.net/forum?id=WpZyPk79Fu](https://openreview.net/forum?id=WpZyPk79Fu)
21. Agentic Tool Use in Large Language Models — arXiv.
    [https://arxiv.org/html/2604.00835v1](https://arxiv.org/html/2604.00835v1)
22. An Agentic Flow for Finite State Machine Extraction using Prompt Chaining — arXiv.
    [https://arxiv.org/html/2507.11222v1](https://arxiv.org/html/2507.11222v1)
23. Prompting with DSPy: A New Approach — DigitalOcean.
    [https://www.digitalocean.com/community/tutorials/prompting-with-dspy](https://www.digitalocean.com/community/tutorials/prompting-with-dspy)
24. Prompt Engineering Is Dead. Long Live DSPy. — DZone.
    [https://dzone.com/articles/prompt-engineering-is-dead-long-live-dspy](https://dzone.com/articles/prompt-engineering-is-dead-long-live-dspy)
25. DSPy. [https://dspy.ai/](https://dspy.ai/)
26. From PoC to Production: Why DSPy Is Becoming Essential for Prompt Engineering —
    Medium.
    [https://medium.com/towardsdev/from-poc-to-production-why-dspy-is-becoming-essential-for-prompt-engineering-597687ba8c46](https://medium.com/towardsdev/from-poc-to-production-why-dspy-is-becoming-essential-for-prompt-engineering-597687ba8c46)
27. Helpful Agent Meets Deceptive Judge: Understanding Vulnerabilities in Agentic
    Workflows — arXiv. [https://arxiv.org/html/2506.03332v1](https://arxiv.org/html/2506.03332v1)
28. Natural Language to Code: How Far Are We? — ResearchGate.
    [https://www.researchgate.net/publication/373141125_Natural_Language_to_Code_How_Far_Are_We](https://www.researchgate.net/publication/373141125_Natural_Language_to_Code_How_Far_Are_We)
29. Two Birds with One Stone: Boosting Code Generation and Code Search via a
    Generative Adversarial Network — ResearchGate.
    [https://www.researchgate.net/publication/374168102_Two_Birds_with_One_Stone_Boosting_Code_Generation_and_Code_Search_via_a_Generative_Adversarial_Network](https://www.researchgate.net/publication/374168102_Two_Birds_with_One_Stone_Boosting_Code_Generation_and_Code_Search_via_a_Generative_Adversarial_Network)
30. ICML Poster: Automated Hypothesis Validation with Agentic Sequential
    Falsifications. [https://icml.cc/virtual/2025/poster/44356](https://icml.cc/virtual/2025/poster/44356)
31. Context Folding Methods: Techniques & Applications — Emergent Mind.
    [https://www.emergentmind.com/topics/context-folding-methods](https://www.emergentmind.com/topics/context-folding-methods)
32. Context as a Tool: Context Management for Long-Horizon SWE-Agents — arXiv.
    [https://arxiv.org/html/2512.22087v1](https://arxiv.org/html/2512.22087v1)
33. Are We in a Continual Learning Overhang? — LessWrong.
    [https://www.lesswrong.com/posts/Lby4gMvKcLPoozHfg/are-we-in-a-continual-learning-overhang-1](https://www.lesswrong.com/posts/Lby4gMvKcLPoozHfg/are-we-in-a-continual-learning-overhang-1)
34. Prompting best practices — Anthropic API Docs.
    [https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
