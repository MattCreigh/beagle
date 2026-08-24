"""Rehydration and resume prompt generation after compaction.

AUTO-GENERATED from context_compaction_hook.py decomposition — DO NOT HAND-EDIT.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .checkpoint import CompactionCheckpoint
from .trigger import ContextMonitor, ContextStatus

logger = logging.getLogger("Beagle.context.rehydration")

DEFAULT_CONTEXT_FILES = [
    "CLAUDE.md",
    "GOOSE.md",
    "GEMINI.md",
    ".goose/standards.md",
    ".goose/format.md",
    ".goose/project.json",
    ".goosehints",
]

PROJECT_CONTEXT_DIRS = [
    Path.cwd(),
    Path.home() / "Dev" / "beagle",
    Path.home() / "Dev",
]


def discover_context_files(project_dir: Path | None = None) -> dict[str, Path]:
    """Discover project context files for post-compaction reloading.

    Searches in priority order:
    1. Specified project_dir
    2. Current working directory
    3. Beagle workspace
    4. Home Dev directory

    Args:
        project_dir: Optional explicit project directory

    Returns:
        Dict mapping file names to their discovered paths

    """
    search_dirs = []

    if project_dir:
        search_dirs.append(project_dir)

    search_dirs.extend(PROJECT_CONTEXT_DIRS)

    found = {}

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue

        for filename in DEFAULT_CONTEXT_FILES:
            if filename in found:
                continue  # Already found in higher priority dir

            filepath = search_dir / filename
            if filepath.exists():
                found[filename] = filepath

    return found


def load_context_file(filepath: Path, max_size: int = 50000) -> str | None:
    """Load a context file with size limit.

    Args:
        filepath: Path to context file
        max_size: Maximum file size to load (bytes)

    Returns:
        File content or None if too large/missing

    """
    try:
        if not filepath.exists():
            return None

        size = filepath.stat().st_size
        if size > max_size:
            # Return truncated version for large files
            with open(filepath, encoding="utf-8") as f:
                return f.read(max_size // 2) + f"\n\n... [truncated, {size} bytes total]"

        with open(filepath, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError, RuntimeError):  # catch: NARROWED
        return None


def create_resume_prompt(
    checkpoint: CompactionCheckpoint,
    project_dir: Path | None = None,
    load_context: bool = True,
    include_constraints: bool = True,
    include_rag: bool = True,
    rag_query: str | None = None,
) -> str:
    """Create a resume prompt after context compaction.

    This prompt includes automatic loading of context files to restore
    project knowledge after context truncation.

    Args:
        checkpoint: Saved state from before compaction
        project_dir: Project directory to search for context files
        load_context: Whether to include context file contents
        include_constraints: Whether to include persisted constraints
        include_rag: Whether to include RAG-derived context
        rag_query: Optional explicit RAG query (defaults to checkpoint task)

    Returns:
        Formatted resume prompt with context files and constraints

    """
    # Build files modified section
    files_section = ""
    if checkpoint.files_modified:
        files_section = "\n".join(f"- {f}" for f in checkpoint.files_modified[-5:])
    else:
        files_section = "(none recorded)"

    # Build commits section
    commits_section = ""
    if checkpoint.pending_commits:
        commits_section = "\n".join(f"- {c}" for c in checkpoint.pending_commits[-3:])
    else:
        commits_section = "(none recorded)"

    # Build next steps section
    steps_section = ""
    if checkpoint.next_steps:
        steps_section = "\n".join(f"- {s}" for s in checkpoint.next_steps)
    else:
        steps_section = f"- Continue from iteration {checkpoint.iteration + 1}"

    # Build constraints section
    constraints_section = ""
    if include_constraints:
        constraints_section = _build_constraints_section(checkpoint, project_dir)

    # Build RAG context section (Phase 5)
    rag_section = ""
    if include_rag:
        query = rag_query or checkpoint.current_task
        rag_section = _build_rag_section(query, project_dir)

    # Load context files
    context_section = ""
    if load_context:
        context_files = discover_context_file_paths(project_dir)
        for filename, filepath in context_files.items():
            content = load_context_file(filepath)
            if content:
                context_section += f"\n\n### {filename}\n```\n{content}\n```"

    # Determine primary CLAUDE.md path for instruction
    claude_md_path = ""
    if project_dir:
        claude_md_path = f"Project dir: {project_dir}"
    else:
        context_files = discover_context_file_paths()
        if "CLAUDE.md" in context_files:
            claude_md_path = f"CLAUDE.md found at: {context_files['CLAUDE.md']}"

    # Build memory trace section (H-MEM v13)
    memory_trace_section = ""
    if hasattr(checkpoint, "memory_trace") and checkpoint.memory_trace:
        memory_trace_section = f"\n\n{checkpoint.memory_trace}\n"

    # Build archived outputs section (H-MEM v13)
    archived_outputs_section = ""
    if hasattr(checkpoint, "archived_outputs") and checkpoint.archived_outputs:
        archived_outputs_section = "\n## Archived Outputs\n\n"
        archived_outputs_section += "Large outputs have been archived. Retrieve via URI:\n"
        for uri, key in checkpoint.archived_outputs.items():
            archived_outputs_section += f"- {key}: {uri}\n"
        archived_outputs_section += "\n"

    return f"""<context_resume>
Context compaction occurred during execution. Resume from checkpoint.

## Checkpoint State
- Last completed: iteration {checkpoint.iteration}/{checkpoint.total_iterations}
- Task: {checkpoint.current_task}
- Timestamp: {checkpoint.timestamp.isoformat()}
{constraints_section}
{rag_section}
{memory_trace_section}
{archived_outputs_section}
## Files Modified Recently
{files_section}

## Last Commits
{commits_section}

## Next Steps
{steps_section}
{context_section}

## Post-Compaction Instructions

CRITICAL: Do NOT stop executing. Context was compacted, not the task.
Your task is NOT complete. Continue from where you left off.

You must re-establish context by:

1. **Load Project Context Files** - The following files should be read:
   - CLAUDE.md (project-specific instructions)
   - GOOSE.md (goose-specific instructions)
   - .goose/standards.md (coding standards)
   - .goose/format.md (output format requirements)
   - .goose/project.json (project metadata)

2. **Verify Current State**
   - Check git status for uncommitted changes
   - Review modified files from checkpoint
   - Verify todo list state

3. **Continue Execution — DO NOT STOP**
   - Resume from iteration {checkpoint.iteration + 1}
   - Review current state and proceed with the plan
   - Update todo list with current progress
   - NEVER halt and ask for instructions — keep working autonomously

{claude_md_path}

</context_resume>"""


def discover_context_file_paths(project_dir: Path | None = None) -> dict[str, Path]:
    """Discover and return paths to context files.

    This is a convenience function that wraps discover_context_files
    for use in the resume prompt.

    Args:
        project_dir: Optional explicit project directory

    Returns:
        Dict mapping file names to their paths (only existing files)

    """
    return discover_context_files(project_dir)


def _build_constraints_section(
    checkpoint: CompactionCheckpoint,
    project_dir: Path | None = None,
) -> str:
    """Build the constraints and knowledge section for the resume prompt.

    Merges constraints, knowledge, and session episodes for context reconstruction:
    1. Constraints extracted and saved in the checkpoint
    2. Constraints persisted in the ConstraintRegistry
    3. Knowledge entries extracted from session
    4. Session episodes for episodic memory (Phase 3)

    Args:
        checkpoint: Saved checkpoint with extracted data
        project_dir: Project directory for registry lookup

    Returns:
        Formatted context section string

    """
    lines = ["", "## Active Constraints", ""]
    lines.append("The following constraints were active before compaction and MUST be respected:")
    lines.append("")

    # First, add constraints from checkpoint
    if checkpoint.extracted_constraints:
        lines.append("### Session Constraints")
        for constraint_data in checkpoint.extracted_constraints:
            desc = constraint_data.get(
                "description", constraint_data.get("content", "Unknown constraint")
            )
            priority = constraint_data.get("priority", 2)
            priority_name = {1: "CRITICAL", 2: "IMPORTANT", 3: "NICE_TO_HAVE"}.get(
                priority, "IMPORTANT"
            )
            lines.append(f"- [{priority_name}] {desc}")
        lines.append("")

    # Then, load persisted constraints from registry
    try:
        from beagle.infrastructure.constraint_extractor import (
            create_extractor,
        )

        # Use project basename for consistent registry lookup
        project = Path(project_dir).name if project_dir else Path.cwd().name

        extractor = create_extractor(project=project)
        extractor.registry.load()

        constraints = extractor.registry.get_active()
        if constraints:
            lines.append("### Persistent Constraints")
            for constraint in constraints[:10]:  # Limit to 10 to save tokens
                lines.append(f"- {constraint.format_for_context()}")

            if len(constraints) > 10:
                lines.append(f"- ... and {len(constraints) - 10} more constraints")
    except (TypeError, ValueError, AttributeError) as e:  # catch: NARROWED
        logger.debug(f"Could not load persisted constraints: {e}")

    # Add knowledge section
    if checkpoint.extracted_knowledge:
        lines.append("")
        lines.append("## Learned Knowledge")
        lines.append("")
        lines.append("The following knowledge was learned from previous session:")
        lines.append("")

        for knowledge_data in checkpoint.extracted_knowledge[:10]:  # Limit to 10
            title = knowledge_data.get("title", "Unknown")
            category = knowledge_data.get("category", "concept")
            lines.append(f"- [{category.upper()}] {title}")

        if len(checkpoint.extracted_knowledge) > 10:
            lines.append(f"- ... and {len(checkpoint.extracted_knowledge) - 10} more entries")

    # Add session episodes section (Phase 3)
    if checkpoint.session_episodes:
        lines.append("")
        lines.append("## Session Context")
        lines.append("")
        lines.append("Recent session activity before compaction:")
        lines.append("")

        for episode_data in checkpoint.session_episodes[:3]:  # Limit to 3 episodes
            phase = episode_data.get("phase", "unknown")
            summary = episode_data.get("summary", "")
            if summary:
                lines.append(f"- [{phase.upper()}] {summary[:200]}")

        if len(checkpoint.session_episodes) > 3:
            lines.append(f"- ... and {len(checkpoint.session_episodes) - 3} more episodes")

    # If no context found, add a note
    has_content = (
        checkpoint.extracted_constraints
        or checkpoint.extracted_knowledge
        or checkpoint.session_episodes
    )
    if not has_content:
        lines.append("(No constraints, knowledge, or session context recorded)")

    lines.append("")
    lines.append("---")
    return "\n".join(lines)


def _build_rag_section(
    query: str,
    project_dir: Path | None = None,
    max_tokens: int = 1000,
) -> str:
    """Build RAG-derived context section for the resume prompt.

    Phase 5: Queries the RAG system for relevant codebase context
    to enhance recovery after compaction.

    Args:
        query: Query string (usually the checkpoint task)
        project_dir: Project directory for context
        max_tokens: Maximum tokens for RAG section

    Returns:
        Formatted RAG context section string

    """
    import asyncio

    if not query:
        return ""

    lines = ["", "## Relevant Codebase Context", ""]
    lines.append(f"Query: {query[:200]}...")
    lines.append("")

    try:
        # Import RAG search function
        from beagle.infrastructure.mcp_rag_server import rag_search

        # Perform RAG search - handle async function properly
        try:
            # Try to get running loop first
            asyncio.get_running_loop()
            # If we have a running loop, we can't use asyncio.run()
            # Create a task and run it in the existing loop
            import concurrent.futures

            # Use thread pool to run async function
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(lambda: asyncio.run(rag_search(query, max_hops=2, top_k=5)))
                try:
                    results = future.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    results = {}
                    logger.debug("RAG query timed out after 10s")
        except RuntimeError:
            # No running loop, safe to use asyncio.run()
            results = asyncio.run(rag_search(query, max_hops=2, top_k=5))

        if not results:
            lines.append("(No relevant context found)")
            lines.append("")
            lines.append("---")
            return "\n".join(lines)

        # Format results
        token_estimate = 50  # Header overhead

        for result in results.get("results", []):
            if token_estimate > max_tokens:
                break

            # Extract file and snippet
            file_path = result.get("file", result.get("path", "unknown"))
            snippet = result.get("snippet", result.get("content", ""))
            relevance = result.get("relevance", result.get("score", 0))

            # Truncate snippet
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."

            lines.append(f"### {file_path}")
            lines.append(f"(Relevance: {relevance:.2f})")
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
            lines.append("")

            token_estimate += len(snippet) // 4 + 50

        lines.append("---")
        return "\n".join(lines)

    except ImportError:
        # RAG not available, return minimal section
        lines.append("(RAG context unavailable - run 'beagle rag ingest' first)")
        lines.append("")
        lines.append("---")
        return "\n".join(lines)

    except (ValueError, TypeError, RuntimeError, TimeoutError) as e:  # catch: NARROWED
        logger.debug(f"RAG query failed: {e}")
        lines.append(f"(RAG query error: {e})")
        lines.append("")
        lines.append("---")
        return "\n".join(lines)


def get_post_compaction_context_instructions(project_dir: Path | None = None) -> str:
    """Generate instructions for loading context after compaction.

    Call this immediately after context compaction to get the
    minimal set of files that must be reloaded.

    Args:
        project_dir: Project directory to search

    Returns:
        Instructions string with file paths

    """
    files = discover_context_files(project_dir)

    if not files:
        return "# No context files found - manual context restoration required"

    lines = ["# Post-Compaction Context Reload", ""]
    lines.append("Load the following files to restore context:")
    lines.append("")

    # Priority order
    priority_order = [
        ("CLAUDE.md", "Project instructions"),
        ("GOOSE.md", "Goose instructions"),
        (".goose/standards.md", "Coding standards"),
        (".goose/format.md", "Output format"),
        (".goose/project.json", "Project metadata"),
        (".goosehints", "Goose hints"),
        ("GEMINI.md", "Gemini instructions"),
    ]

    for filename, description in priority_order:
        if filename in files:
            lines.append(f"  {description}: {files[filename]}")

    return "\n".join(lines)


# Module-level monitor instance for convenience
_monitor: ContextMonitor | None = None


def get_monitor(total_iterations: int = 25) -> ContextMonitor:
    """Get or create singleton monitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = ContextMonitor(total_iterations=total_iterations)
    return _monitor


def pre_compact_check(iteration: int, task: str) -> ContextStatus:
    """Convenience function for pre-work context check.

    Call at the start of each iteration:
        status = pre_compact_check(6, "refactoring iteration")
        if status.is_critical:
            get_monitor().save_checkpoint()
            # Compaction will happen at 70%
        if status.should_compact:
            logger.info("Context at {status.percentage:.1%}, compact soon")
    """
    monitor = get_monitor()
    return monitor.check_before_work(iteration, task)
