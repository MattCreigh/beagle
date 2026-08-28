"""Smart query-to-workflow router for Goose Agentic Workflow.

Analyzes user queries and routes them to the most appropriate workflow
based on keyword matching and intent detection.

Workflow names MUST match the YAML ``name`` field and the filename
(stem) in metaprompts/.  The canonical set is:

    audit, research, develop, incident, security, db-migration,
    deep-planning, devops, self-improvement, verify
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RouteResult:
    """Result of query routing."""

    workflow: str
    confidence: float
    reasoning: str
    alternatives: list[tuple[str, float]] | None = None

    def __post_init__(self):
        if self.alternatives is None:
            self.alternatives = []


# ── Workflow routing rules ────────────────────────────────────────────────────
# Keys match the YAML ``name`` field and filename (stem) in metaprompts/.

WORKFLOW_PATTERNS: dict[str, dict[str, Any]] = {
    "audit": {
        "keywords": [
            "audit",
            "analyze",
            "review",
            "assess",
            "evaluate",
            "architecture",
            "technical debt",
            "code quality",
        ],
        "patterns": [
            r"audit\s+(?:the\s+)?code",
            r"analyze\s+(?:the\s+)?(?:code|architecture)",
            r"review\s+(?:the\s+)?(?:code|codebase)",
            r"what\s+is\s+the\s+architecture",
            r"find\s+(?:technical\s+)?debt",
        ],
        "description": "Security audit with verified findings only",
    },
    "research": {
        "keywords": [
            "research",
            "understand",
            "explore",
            "how does",
            "what is",
            "explain",
            "learn about",
            "find out",
            "discover",
            "study",
        ],
        "patterns": [
            r"how\s+does\s+.+\s+work",
            r"what\s+is\s+.+",
            r"explain\s+.+",
            r"research\s+.+",
            r"understand\s+.+",
        ],
        "description": "Multi-phase research with verified source citations",
    },
    "develop": {
        "keywords": [
            "implement",
            "add",
            "create",
            "build",
            "develop",
            "feature",
            "functionality",
            "capability",
            "write code",
        ],
        "patterns": [
            r"implement\s+.+",
            r"add\s+(?:a\s+)?(?:new\s+)?(?:feature|functionality)",
            r"create\s+.+",
            r"build\s+.+",
            r"write\s+(?:code|a\s+function|a\s+module)",
        ],
        "description": "Core feature development with ensemble coding and security review",
    },
    "incident": {
        "keywords": [
            "fix",
            "debug",
            "broken",
            "error",
            "bug",
            "incident",
            "crash",
            "failure",
            "issue",
            "not working",
            "problem",
            "troubleshoot",
            "root cause",
            "postmortem",
            "investigate",
            "diagnose",
            "diagnostics",
        ],
        "patterns": [
            r"fix\s+(?:the\s+)?(?:bug|error|issue)",
            r"debug\s+.+",
            r"why\s+is\s+.+\s+(?:not\s+working|broken|failing)",
            r"troubleshoot\s+.+",
            r".+\s+is\s+(?:broken|crashing|failing)",
            r"(?:p0|p1|p2)\s+(?:incident|outage)",
            r"investigate\s+(?:the\s+)?(?:system|service|issue|problem)",
            r"diagnose\s+(?:the\s+)?(?:system|service|issue|problem)",
            r"system\s+(?:investigation|diagnostics|health)",
        ],
        "description": "Production incident response and root cause analysis",
    },
    "security": {
        "keywords": [
            "secure",
            "harden",
            "vulnerability",
            "security",
            "penetration",
            "pentest",
            "owasp",
            "cve",
            "threat",
            "attack",
            "exploit",
            "compliance",
        ],
        "patterns": [
            r"secure\s+.+",
            r"harden\s+.+",
            r"find\s+(?:security\s+)?vulnerabilities",
            r"security\s+(?:audit|review|scan|harden)",
            r"check\s+for\s+(?:security\s+)?(?:issues|vulnerabilities)",
        ],
        "description": "Security hardening with compliance verification",
    },
    "db-migration": {
        "keywords": [
            "migrate",
            "migration",
            "database",
            "schema",
            "table",
            "column",
            "sql",
            "postgresql",
            "mysql",
            "alembic",
        ],
        "patterns": [
            r"migrate\s+(?:the\s+)?database",
            r"add\s+(?:a\s+)?(?:new\s+)?(?:table|column)",
            r"change\s+(?:the\s+)?schema",
            r"update\s+(?:the\s+)?database",
        ],
        "description": "Database schema migration with rollback verification",
    },
    "deep-planning": {
        "keywords": [
            "plan",
            "planning",
            "strategy",
            "roadmap",
            "design",
            "architect",
            "deep plan",
            "comprehensive plan",
        ],
        "patterns": [
            r"plan\s+(?:a\s+)?(?:new\s+)?(?:feature|system|architecture)",
            r"deep\s+plan(?:ning)?",
            r"create\s+(?:a\s+)?(?:roadmap|strategy)",
            r"design\s+(?:the\s+)?(?:architecture|system)",
            r"comprehensive\s+plan",
        ],
        "description": "Multi-phase deep planning with adversarial critique",
    },
    "devops": {
        "keywords": [
            "devops",
            "ci/cd",
            "pipeline",
            "deploy",
            "deployment",
            "infrastructure",
            "docker",
            "kubernetes",
            "terraform",
            "ansible",
        ],
        "patterns": [
            r"(?:set\s+up|create|build)\s+(?:a\s+)?(?:ci.?cd|pipeline)",
            r"deploy\s+.+",
            r"(?:infrastructure|infra)\s+(?:as\s+code)?",
            r"docker\s+(?:compose|file|build)",
            r"kubernetes\s+(?:manifest|deploy|helm)",
        ],
        "description": "DevOps workflow for CI/CD, deployments, and infrastructure",
    },
    "self-improvement": {
        "keywords": [
            "self-improve",
            "self improve",
            "improve itself",
            "improve beagle",
            "improve goose",
            "auto-improve",
            "recursive improvement",
            "meta-improvement",
            # v13.21.3: Catch "Beagle tool itself is broken" queries that
            # previously routed to "incident" because of generic words like
            # "fix" / "broken" / "error". A user reporting "`beagle --help`
            # shows the wrong version" wants the Beagle codebase patched,
            # not an incident-response workflow investigating a runtime
            # service outage.
            "beagle shim",
            "beagle symlink",
            "beagle version mismatch",
            "wrong beagle version",
            "stale beagle install",
            "beagle install",
            "beagle setup",
        ],
        "patterns": [
            r"improve\s+(?:the\s+)?(?:beagle|goose|itself|codebase)",
            r"self[\s-]improv",
            r"auto[\s-]improv",
            r"recursive\s+improvement",
            r"meta[\s-]improvement",
            # v13.21.3: Direct "fix the Beagle tool" signals. Patterns are
            # weighted 2x keywords, so a single match here is worth two
            # keyword hits, dominating generic "fix" / "broken" patterns
            # from "incident".
            r"fix\s+(?:beagle|goose)(?:\s+(?:shim|symlink|install|cli|version|binary))?",
            r"(?:beagle|goose)\s+(?:--help|version|cli|shim|symlink|install)\s+(?:is\s+)?(?:broken|wrong|missing|fails?|failing|stale|outdated|shows?)",
            r"(?:beagle|goose)\s+(?:is\s+)?(?:broken|wrong|fails?|failing|crashing)",
            r"(?:my\s+)?beagle\s+(?:shim|symlink|install|binary|cli)\s+(?:is\s+)?(?:broken|wrong|stale|outdated|missing)",
            r"~/.local/bin/(?:beagle|goose(?:-\w+)?)",
            r"(?:wrong|broken|stale|outdated|missing)\s+(?:beagle|goose)\s+(?:shim|symlink|install|version|cli)",
            r"beagle\s+(?:progress|context|workflow|agents|router|cost_tracker|list|info|stats)\s+(?:\w+\s+)?(?:is\s+)?(?:broken|wrong|missing|fails?|failing|stopped|stale|shows?|returns?)",
            r"(?:beagle|goose)\s+(?:context\s+)?(?:percentage\s+)?bar\s+(?:has\s+|is\s+|are\s+)?(?:broken|stopped|not\s+working|missing)",
            # Catch "beagle Event loop is closed"-style phrasing: beagle followed
            # by an error-y noun is almost always a tool-internal bug, not a
            # downstream service incident. Permissive on the noun so we catch
            # "beagle cost_tracker bug", "beagle router misclassifies", "beagle
            # workflow loader path resolution is broken", etc. The optional
            # "is/are" lets us match both "beagle foo broken" and
            # "beagle foo is broken".
            r"(?:beagle|goose(?:-\w+)?)(?:\s+[a-z_][a-z0-9_]*){0,5}\s+(?:is\s+|are\s+)?(?:RuntimeError|Exception|Traceback|warning|warn|error|err|fail(?:ed|ing|s|ure)?|crash(?:ed|ing|s)?|hang(?:s|ed)?|stuck|broken|bug|regression|wrong|missing|fails?|misclassif|wrong|misroute)",
            r"(?:beagle|goose(?:-\w+)?)\s+(?:\w+\s+){0,3}(?:install|path|version|shim|symlink)\s+(?:is\s+|are\s+)?(?:broken|wrong|missing|stale|outdated|fails?)",
            r"fix\s+(?:the\s+|a\s+)?follow[\s-]?up",
        ],
        # v13.21.3: bare "beagle" / "goose-workflow" noun in the query —
        # when these are present at all, the query is almost always about
        # the Beagle tool itself, not a downstream service. Weighted as a
        # keyword_boost (0.5pt) so it breaks ties with "incident" without
        # overwhelming genuine service incidents (where "beagle" never
        # appears as a subject).
        "keyword_boost": {
            # If any of these bare nouns appear, add a flat +1.5 to
            # self-improvement score (on top of any keyword/pattern hits).
            # This breaks ties with "incident" for Beagle-tool queries
            # like "fix beagle cost_tracker bug" (4.5 vs 3.0) without
            # overwhelming genuine service incidents where beagle/goose
            # never appear as a subject. The bare word "broken" is an
            # incident keyword and a near-universal modifier, so without
            # this boost "fix beagle X broken" would tie 3-3 and fall
            # to whichever workflow is iterated first.
            "beagle": 1.5,
            "goose-workflow": 1.5,
        },
        "description": "Recursive self-improvement of Beagle's own codebase (includes fixing the Beagle tool's own CLI, shim, install, or runtime config)",
    },
    "verify": {
        "keywords": [
            "verify",
            "lint",
            "test",
            "check",
            "validate",
            "ci check",
            "pre-commit",
        ],
        "patterns": [
            r"verify\s+(?:the\s+)?(?:code|changes|build)",
            r"run\s+(?:the\s+)?(?:tests|lint|checks)",
            r"check\s+(?:that\s+)?(?:the\s+)?(?:code|tests|build)\s+passes",
            r"lint\s+(?:the\s+)?code",
            r"validate\s+(?:the\s+)?changes",
        ],
        "description": "Verify code changes with linting and tests",
    },
}

# Default workflow for unmatched queries
DEFAULT_WORKFLOW = "research"


def route_query(query: str) -> RouteResult:
    """Route a query to the most appropriate workflow.

    Args:
        query: The user's query

    Returns:
        RouteResult with workflow recommendation

    """
    query_lower = query.lower()
    scores: dict[str, float] = {}

    for workflow, config in WORKFLOW_PATTERNS.items():
        score = 0.0

        # Check keywords (word-boundary matching to avoid false positives)
        keywords = config.get("keywords", [])
        for keyword in keywords:
            if re.search(r"\b" + re.escape(keyword) + r"\b", query_lower):
                score += 1.0

        # Check patterns
        patterns = config.get("patterns", [])
        for pattern in patterns:
            if re.search(pattern, query_lower):
                score += 2.0  # Patterns are worth more than keywords

        # v13.21.3: Apply bare-noun "keyword_boost" — a flat per-noun bonus
        # when the query mentions a project name (beagle / goose-workflow)
        # that is the target of the workflow rather than the subject of an
        # external service. Used to break ties with "incident" for queries
        # like "fix beagle cost_tracker bug" where beagle is the noun being
        # fixed, not a downstream dependency.
        keyword_boost = config.get("keyword_boost", {})
        for noun, boost in keyword_boost.items():
            if re.search(r"\b" + re.escape(noun) + r"\b", query_lower):
                score += float(boost)

        if score > 0:
            scores[workflow] = score

    if not scores:
        # No matches, use default
        return RouteResult(
            workflow=DEFAULT_WORKFLOW,
            confidence=0.3,
            reasoning="No specific keywords matched, defaulting to research workflow",
            alternatives=[],
        )

    # Sort by score
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_workflow, best_score = sorted_scores[0]

    # Calculate confidence based on absolute strength and dominance over runner-up.
    # This does NOT depend on the winning workflow's vocabulary size, preventing
    # confidence regressions when keywords/patterns are added.
    # SATURATION_SCORE: score at which absolute strength saturates (1.0).
    SATURATION_SCORE = 4.0
    abs_strength = min(1.0, best_score / SATURATION_SCORE)

    # Dominance: margin over runner-up, in [0, 1]. No competitor -> full margin.
    runner_up_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0
    margin = (best_score - runner_up_score) / best_score if best_score > 0 else 0.0

    # Blend: 60% absolute strength, 40% dominance. Ceiling at 0.95.
    confidence = min(0.95, 0.6 * abs_strength + 0.4 * margin)

    # Get alternatives
    alternatives = [(wf, round(score / best_score, 2)) for wf, score in sorted_scores[1:4]]

    reasoning = f"Matched {int(best_score)} indicators for {best_workflow}"
    if best_score >= 3:
        reasoning = f"Strong match ({int(best_score)} indicators) for {best_workflow}"

    return RouteResult(
        workflow=best_workflow,
        confidence=round(confidence, 2),
        reasoning=reasoning,
        alternatives=alternatives,
    )


def suggest_workflow(query: str) -> str | None:
    """Get workflow suggestion message if routing is not confident.

    Args:
        query: The user's query

    Returns:
        Suggestion message or None if confident

    """
    result = route_query(query)

    if result.confidence >= 0.7:
        return None

    workflow_desc = WORKFLOW_PATTERNS.get(result.workflow, {}).get("description", "workflow")

    lines = [
        f"Your query looks like a {workflow_desc} task.",
        "",
        f"Suggested: goose-workflow run "
        f"{result.workflow} "
        f'"{query[:50] + "..." if len(query) > 50 else query}"',
        "",
        "This workflow includes:",
    ]

    # Add workflow phases description
    phase_descriptions = {
        "audit": [
            "Discovery phase (search-executor)",
            "Synthesis phase (synthesis-writer)",
        ],
        "research": [
            "Search phase (search-executor)",
            "Synthesis phase (synthesis-writer)",
            "Ground-truth validation",
        ],
        "develop": [
            "Requirements analysis (research-planner)",
            "Architecture design (architecture-auditor)",
            "Implementation (sota-dev with ensemble)",
            "Security review (security-auditor)",
        ],
        "incident": [
            "Triage (root-cause-analyst)",
            "Investigation (protocol-debugger)",
            "Root cause analysis",
            "Remediation (sota-dev)",
            "Postmortem (synthesis-writer)",
        ],
        "security": [
            "Threat modeling (security-auditor)",
            "Vulnerability scan (security-auditor)",
            "Compliance check",
            "Hardening plan and implementation",
        ],
        "db-migration": [
            "Planning and diff analysis",
            "Migration generation",
            "Safety and lock verification",
            "Dry run and rollback verification",
        ],
        "deep-planning": [
            "Initial discovery (research-planner)",
            "Deep codebase scan (search-executor)",
            "Dependency and architecture scan",
            "Adversarial critique (fact-checker)",
            "Master plan synthesis (synthesis-writer)",
        ],
        "devops": [
            "Pipeline planning",
            "Pipeline generation",
            "Security and syntax validation",
            "Documentation (synthesis-writer)",
        ],
        "self-improvement": [
            "Self-audit",
            "Improvement planning",
            "Implementation (sota-dev)",
            "Verification and testing",
            "Documentation update",
        ],
        "verify": [
            "Lint check (search-executor)",
            "Test check (search-executor)",
        ],
    }

    phases = phase_descriptions.get(result.workflow, ["Multiple coordinated phases"])
    for phase in phases:
        lines.append(f"  - {phase}")

    if result.alternatives:
        lines.append("")
        lines.append("Other options:")
        for alt_wf, _alt_conf in result.alternatives[:2]:
            alt_desc = WORKFLOW_PATTERNS.get(alt_wf, {}).get("description", "")
            lines.append(f"  - {alt_wf}: {alt_desc}")

    return "\n".join(lines)


def get_workflow_keywords(workflow: str) -> list[str]:
    """Get keywords for a workflow.

    Args:
        workflow: Workflow name

    Returns:
        List of keywords

    """
    config = WORKFLOW_PATTERNS.get(workflow, {})
    return config.get("keywords", [])


def list_routable_workflows() -> list[dict[str, Any]]:
    """List all workflows that can be routed to.

    Returns:
        List of workflow info dicts

    """
    return [
        {
            "name": workflow,
            "description": config.get("description", ""),
            "keywords": config.get("keywords", [])[:5],
        }
        for workflow, config in WORKFLOW_PATTERNS.items()
    ]


async def route_query_llm(query: str) -> RouteResult:
    """Route a query using LLM-based semantic classification.

    Uses instructor + OpenAI for structured output extraction.
    Falls back to keyword routing on failure.

    Requires config.toml [router] use_llm = true to activate.
    """
    try:
        from ..ai.structured_output import classify_query

        available = list(WORKFLOW_PATTERNS.keys())
        classification = await classify_query(query, available)

        if classification is not None:
            # Validate workflow name
            if classification.workflow not in WORKFLOW_PATTERNS:
                classification.workflow = DEFAULT_WORKFLOW

            return RouteResult(
                workflow=classification.workflow,
                confidence=classification.confidence,
                reasoning=f"[LLM] {classification.reasoning}",
            )

        # Fallback if instructor unavailable
        return route_query(query)

    except Exception:  # ruff: ignore[BLE001]  # broad catch intentional
        # Fall back to keyword routing on any failure
        return route_query(query)


if __name__ == "__main__":
    # Demo harness — run with: python -m beagle.core.router
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Test queries
    test_queries = [
        "Audit the codebase for security vulnerabilities",
        "How does the IPC ring buffer system work?",
        "Add a new feature for user authentication",
        "Fix the bug in the login flow",
        "Secure the API endpoints",
        "Migrate the database to add a new column",
        "What is the architecture of the system?",
        "Plan a complex microservices migration",
        "Set up a CI/CD pipeline for deployment",
        "Verify my code changes pass linting",
        "Improve Beagle's own codebase",
        "Hello world",  # Should default
    ]

    logger.info("Query Routing Tests\n" + "=" * 60 + "\n")

    for query in test_queries:
        result = route_query(query)
        logger.info(f"Query: {query[:50]}...")
        logger.info(f"  Workflow: {result.workflow}")
        logger.info(f"  Confidence: {result.confidence}")
        logger.info(f"  Reasoning: {result.reasoning}")
        if result.alternatives:
            logger.info(f"  Alternatives: {[a[0] for a in result.alternatives]}")
        logger.info("---")
