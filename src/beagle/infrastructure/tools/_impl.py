"""MCP tool implementations — extracted from mcp_utility_server.py (Phase 5.1).

This module contains all async tool function bodies.
The @mcp.tool() registration surface lives in mcp_utility_server.py.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from beagle.config.config import (
    get_config,
)
from beagle.core.graph import (
    run_workflow,
)
from beagle.core.transports import active as _transport
from beagle.utils.mcp_rate_limit import RateLimiter

# Project root for subprocess cwd and path resolution (not for sys.path).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

logger = logging.getLogger("Beagle.mcp.utility._impl")

# Module-level cache for the `fd` availability probe (B-21/B-22)
_HAS_FD: bool | None = None

# ── Workflow dependency imports ──────────────────────────────────────────────
from beagle.core.router import (  # ruff: ignore[E402]
    list_routable_workflows,
    route_query,
)
from beagle.core.workflow_loader import (  # ruff: ignore[E402]
    list_workflows,
    validate_workflow,
)
from beagle.cost_tracker import (  # ruff: ignore[E402]
    _get_pricing,
    estimate_tokens_agnostic,
)
from beagle.infrastructure.mcp_common import (  # ruff: ignore[E402]
    get_metrics_summary,
    is_path_within,
    record_metric,
    set_correlation_id,
)
from beagle.security import (  # ruff: ignore[E402]
    scrub_secrets,
    validate_file_path,
    validate_query_async,
)
from beagle.utils.env_manager import (  # ruff: ignore[E402]
    get_workspace_root,
)

_limiter = RateLimiter()
_FOLD_ID_RE = re.compile(r"^[0-9a-f]{12}$")
# M2 (audit 2026-08-15): subprocess calls in async MCP tools must not block
# the event loop. A single named timeout replaces the unit-conflated
# `max(30, max_results)` / `max(30, max_depth * 10)` expressions (a result
# count / directory depth used as a value in seconds).
_SUBPROCESS_TIMEOUT_S = 60
# S607 (audit 2026-08-15): resolve the git binary once so subprocess calls
# use an absolute path rather than a partial executable name.
_GIT_BIN = shutil.which("git") or "git"
# ══════════════════════════════════════════════════════════════════════════════
# Code Tools — code_search, file_discovery, code_context
# ══════════════════════════════════════════════════════════════════════════════


async def code_search(
    pattern: str,
    path: str = ".",
    file_glob: str = "*.py",
    max_results: int = 50,
    context_lines: int = 2,
    case_sensitive: bool = False,
) -> str:
    """Search codebase for a pattern with structured results using ripgrep.

    Args:
        pattern: Regex pattern to search for.
        path: Directory to search within.
        file_glob: Glob pattern for files (e.g. *.py).
        max_results: Cap on number of matches.
        context_lines: Number of lines around each match.
        case_sensitive: Whether to respect case.

    """
    await _limiter.check()

    is_valid, error = validate_file_path(str(path))
    if not is_valid:
        return json.dumps({"status": "error", "message": f"Invalid path: {error}"})

    # v1.2.0 (RG-5, BGL-006): the pattern is validated by the engine that
    # executes it — ripgrep's Rust regex crate — not by Python's `re`. The two
    # dialects disagree in both directions: `(?<=def )beagle` compiles in
    # Python but ripgrep rejects it (exit 2), and `\p{Greek}` is rejected by
    # Python but ripgrep accepts it (exit 0). A Python `re` pre-check therefore
    # both false-accepts (silent empty set) and false-rejects (valid query
    # blocked). The return-code path below reports the engine's verdict.
    cmd = ["rg", "--json", "-g", file_glob, "-C", str(context_lines)]
    if not case_sensitive:
        cmd.append("-i")
    cmd.extend([pattern, path])

    try:
        process = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )

        # v1.2.0 (RG-5, BGL-005): read the return code. ripgrep exit codes are
        # 0 = match found, 1 = no match, 2 = error (e.g. invalid pattern).
        # Exit 1 is a successful search with zero matches and must stay a
        # success. Exit 2 or higher is an error — report it with the stderr
        # text instead of returning an empty "ok" set that an agent would
        # misread as "the symbol does not exist".
        if process.returncode >= 2:
            stderr = (process.stderr or "").strip()
            logger.error(
                "code_search: ripgrep exited %d for pattern %r: %s",
                process.returncode,
                pattern,
                stderr,
            )
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Search failed: ripgrep exited {process.returncode}: {stderr}",
                }
            )

        results: list[dict[str, Any]] = []
        total_matches = 0
        # ripgrep emits one JSON object per line. An unparseable line means a
        # match was dropped, so the count is reported once after the loop.
        malformed_lines = 0
        last_parse_error: Exception | None = None

        for line in process.stdout.splitlines():
            try:
                data = json.loads(line)
                if data["type"] == "match":
                    total_matches += 1
                    if len(results) < max_results:
                        results.append(
                            {
                                "file": data["data"]["path"]["text"],
                                "line": data["data"]["line_number"],
                                "content": data["data"]["lines"]["text"].strip(),
                                "context_before": [
                                    line_t["text"].strip()
                                    for line_t in data["data"].get("context_before", [])
                                ],
                                "context_after": [
                                    line_t["text"].strip()
                                    for line_t in data["data"].get("context_after", [])
                                ],
                            }
                        )
            except (json.JSONDecodeError, KeyError) as exc:
                malformed_lines += 1
                last_parse_error = exc
                continue

        if malformed_lines:
            logger.warning(
                "code_search: skipped %d unparseable ripgrep output line(s); the "
                "result set may be incomplete. Last error: %s",
                malformed_lines,
                last_parse_error,
            )

        return json.dumps(
            {
                "status": "ok",
                "matches": results,
                "total_matches": total_matches,
                "truncated": total_matches > max_results,
            }
        )

    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as e:  # catch: NARROWED
        logger.error(f"code_search failed: {e}", exc_info=True)
        error_msg = scrub_secrets(str(e))[:500]
        return json.dumps({"status": "error", "message": f"Search failed: {error_msg}"})


async def file_discovery(
    pattern: str = "",
    path: str = ".",
    file_type: str = "file",
    extension: str = "",
    max_depth: int = 5,
    max_results: int = 100,
) -> str:
    """Discover files matching criteria with structured results.

    Args:
        pattern: Optional pattern in filename.
        path: Root directory to start search.
        file_type: "file", "directory", or "symlink".
        extension: Filter by extension (e.g. .py).
        max_depth: Max directory depth.
        max_results: Max number of files to return.

    """
    await _limiter.check()

    is_valid, error = validate_file_path(str(path))
    if not is_valid:
        return json.dumps({"status": "error", "message": f"Invalid path: {error}"})

    allowed_types = {"file", "directory", "symlink"}
    if file_type not in allowed_types:
        return json.dumps(
            {
                "status": "error",
                "message": (f"Invalid file_type: {file_type!r}. Allowed: {sorted(allowed_types)}"),
            }
        )

    # Prefer 'fd' if available, else 'find'. Cache the probe at module scope
    # so every file_discovery call does not spawn a subprocess.
    global _HAS_FD
    if _HAS_FD is None:
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["fd", "--version"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            _HAS_FD = True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            _HAS_FD = False

    if _HAS_FD:
        fd_type = {"file": "f", "directory": "d", "symlink": "l"}[file_type]
        cmd = ["fd", "--max-depth", str(max_depth), "--type", fd_type]
        if extension:
            cmd.extend(["-e", extension.lstrip(".")])
        if pattern:
            cmd.append(pattern)
        cmd.append(path)
    else:
        # find fallback
        cmd = ["find", path, "-maxdepth", str(max_depth)]
        if file_type == "file":
            cmd.extend(["-type", "f"])
        elif file_type == "directory":
            cmd.extend(["-type", "d"])
        elif file_type == "symlink":
            cmd.extend(["-type", "l"])
        if extension:
            cmd.extend(["-name", f"*{extension}"])
        if pattern:
            cmd.extend(["-name", f"*{pattern}*"])

    try:
        process = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_PROJECT_ROOT),
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        files = []
        lines = process.stdout.splitlines()

        for line in lines[:max_results]:
            fpath = Path(line)
            full_path = _PROJECT_ROOT / fpath
            if full_path.exists():
                stats = full_path.stat()
                files.append(
                    {
                        "path": str(fpath),
                        "size_bytes": stats.st_size,
                        "modified": stats.st_mtime,
                        "type": "directory" if full_path.is_dir() else "file",
                    }
                )

        return json.dumps(
            {
                "status": "ok",
                "files": files,
                "total_found": len(lines),
                "truncated": len(lines) > max_results,
            }
        )
    except (OSError, subprocess.SubprocessError, ValueError) as e:  # catch: NARROWED
        logger.error(f"file_discovery failed: {e}", exc_info=True)
        error_msg = scrub_secrets(str(e))[:500]
        return json.dumps({"status": "error", "message": f"Discovery failed: {error_msg}"})


async def code_context(
    file_path: str,
    query_type: str = "overview",
    symbol: str = "",
) -> str:
    """Get structured code context for a file or symbol using AST.

    Args:
        file_path: Path to the source file.
        query_type: "overview", "function", "class", "imports".
        symbol: Optional name of function/class to extract.

    """
    await _limiter.check()

    is_valid, error = validate_file_path(str(file_path))
    if not is_valid:
        return json.dumps({"status": "error", "message": f"Invalid path: {error}"})

    full_path = _PROJECT_ROOT / file_path
    if not full_path.exists() or not full_path.is_file():
        return json.dumps({"status": "error", "message": "File not found"})

    if not file_path.endswith(".py"):
        # Minimal fallback for non-python
        return json.dumps({"status": "ok", "content": full_path.read_text()[:5000]})

    try:
        tree = ast.parse(full_path.read_text())

        if query_type == "overview":
            defs = []
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    defs.append({"type": "function", "name": node.name, "line": node.lineno})
                elif isinstance(node, ast.ClassDef):
                    defs.append({"type": "class", "name": node.name, "line": node.lineno})
            return json.dumps({"status": "ok", "definitions": defs})

        if query_type == "imports":
            imports = []
            for node in ast.walk(tree):  # type: ignore[assignment]
                if isinstance(node, ast.Import):
                    for n in node.names:
                        imports.append(n.name)
                elif isinstance(node, ast.ImportFrom):
                    imports.append(f"{node.module}.*")
            return json.dumps({"status": "ok", "imports": imports})

        if query_type in ("function", "class") and symbol:
            for node in ast.walk(tree):  # type: ignore[assignment]
                if isinstance(node, ast.FunctionDef | ast.ClassDef) and node.name == symbol:
                    return json.dumps(
                        {
                            "status": "ok",
                            "name": node.name,
                            "content": ast.get_source_segment(full_path.read_text(), node),
                        }
                    )
            return json.dumps({"status": "error", "message": f"Symbol '{symbol}' not found"})

        return json.dumps({"status": "error", "message": "Invalid query type or missing symbol"})

    except (SyntaxError, ValueError, OSError, RuntimeError) as e:  # catch: NARROWED
        logger.error(f"code_context failed: {e}", exc_info=True)
        error_msg = scrub_secrets(str(e))[:500]
        return json.dumps({"status": "error", "message": f"Context extraction failed: {error_msg}"})


# ══════════════════════════════════════════════════════════════════════════════
# Web Search — web_search, arxiv_search, web_research
# ══════════════════════════════════════════════════════════════════════════════

# Import the web search functionality
try:
    from beagle.web_search import search as ddg_search

    WEB_SEARCH_AVAILABLE = True
except ImportError:
    WEB_SEARCH_AVAILABLE = False


async def web_search(
    query: str,
    max_results: int = 10,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    timelimit: str | None = None,
) -> str:
    """Search the web using DuckDuckGo.

    Args:
        query: Search query string (max 500 characters)
        max_results: Maximum number of results to return (1-50, default: 10)
        region: Search region code (default: 'wt-wt' for worldwide)
        safesearch: Safe search setting - 'on', 'moderate', or 'off' (default: 'moderate')
        timelimit: Time limit for results - 'day', 'week', 'month', 'year', or None

    Returns:
        JSON string containing search results with 'title', 'href', and 'body' for each result.

    Example:
        results = await web_search("python async programming", max_results=5)
        # Returns JSON: {"results": [{"title": "...", "href": "...", "body": "..."}], "count": 5}

    """
    await _limiter.check()

    if not WEB_SEARCH_AVAILABLE:
        return json.dumps(
            {
                "error": "Web search not available - ddgs package not installed",
                "query": query,
                "results": [],
            }
        )

    try:
        logger.info(f"Web search: '{query[:50]}...' with max_results={max_results}")
        # v1.2.0 (RG-4): the blocking ddg_search call must not stall the event
        # loop. Copy A (mcp_utility_server.py) held this asyncio.to_thread
        # correction; copy B (this module) made the blocking call inline. The
        # blocking call stalled the FastMCP server for the duration of the
        # search (audit B6). Port the correction here so the single
        # implementation holds it.
        results = await asyncio.to_thread(
            ddg_search,
            query=query,
            max_results=max_results,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
        )
        logger.info(f"Web search completed: {len(json.loads(results).get('results', []))} results")
        return results
    except (ConnectionError, OSError, TimeoutError, ValueError) as e:  # catch: NARROWED
        logger.exception(f"Web search failed: {e}")
        return json.dumps({"error": str(e), "query": query, "results": []})


async def arxiv_search(
    query: str,
    max_results: int = 10,
    sort_by: str = "submittedDate",
    sort_order: str = "descending",
) -> str:
    """Search arXiv for academic papers.

    Uses the arXiv API to find recent research papers.

    Args:
        query: Search query (keywords, author, title, etc.)
        max_results: Maximum number of papers to return (1-50, default: 10)
        sort_by: Sort field - 'submittedDate', 'relevance', 'lastUpdatedDate'
        sort_order: Sort order - 'ascending' or 'descending'

    Returns:
            "paper metadata including title, authors, arXiv ID, "
            "abstract, and PDF link."

    Example:
        papers = await arxiv_search("transformer attention mechanism", max_results=5)

    """
    await _limiter.check()

    import httpx
    from defusedxml import ElementTree as ET

    base_url = "https://export.arxiv.org/api/query"

    params: dict[str, str | int | bool | None] = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }

    try:
        logger.info(f"arXiv search: '{query}' with max_results={max_results}")

        async with _transport().async_client(
            timeout=httpx.Timeout(30.0, connect=10.0)
        ) as client:
            response = await client.get(base_url, params=params)
            response.raise_for_status()
            xml_data = response.text

        # Parse Atom feed
        root = ET.fromstring(xml_data)

        # Define namespaces
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }

        papers = []
        for entry in root.findall("atom:entry", ns):
            try:
                title = (
                    entry.find("atom:title", ns).text.strip()
                    if entry.find("atom:title", ns) is not None
                    else ""
                )

                authors = []
                for author in entry.findall("atom:author", ns):
                    name = author.find("atom:name", ns)
                    if name is not None:
                        authors.append(name.text)

                summary = entry.find("atom:summary", ns)
                abstract = summary.text.strip() if summary is not None else ""

                pdf_link = None
                link = None
                for link_elem in entry.findall("atom:link", ns):
                    href = link_elem.get("href", "")
                    if "pdf" in href:
                        pdf_link = href
                    elif link is None:
                        link = href

                arxiv_id = (
                    entry.find("atom:id", ns).text.split("/")[-1]
                    if entry.find("atom:id", ns) is not None
                    else ""
                )

                published = entry.find("atom:published", ns)
                pub_date = published.text if published is not None else ""

                papers.append(
                    {
                        "title": title,
                        "authors": authors,
                        "arxiv_id": arxiv_id,
                        "abstract": (abstract[:500] + "..." if len(abstract) > 500 else abstract),
                        "link": f"https://arxiv.org/abs/{arxiv_id}",
                        "pdf_link": pdf_link or f"https://arxiv.org/pdf/{arxiv_id}",
                        "published": pub_date,
                    }
                )
            except (AttributeError, KeyError, TypeError) as e:  # catch: NARROWED
                logger.warning(f"Failed to parse paper entry: {e}")
                continue

        logger.info(f"arXiv search completed: {len(papers)} papers found")
        return json.dumps(
            {"results": papers, "count": len(papers), "query": query},
            indent=2,
        )

    except (ConnectionError, OSError, TimeoutError, ValueError) as e:  # catch: NARROWED
        logger.exception(f"arXiv search failed: {e}")
        return json.dumps({"error": str(e), "query": query, "results": []})


async def web_research(
    topic: str,
    depth: str = "standard",
    max_sources: int = 5,
) -> str:
    """Perform a structured web research on a topic.

    Combines web search with deduplication and synthesis into a research report.

    Args:
        topic: Research topic or question
        depth: Research depth - 'shallow', 'standard', or 'deep'
        max_sources: Maximum number of sources to include (default: 5)

    Returns:
        Markdown-formatted research report with citations.

    """
    await _limiter.check()

    if not WEB_SEARCH_AVAILABLE:
        return json.dumps({"error": "Web search not available", "topic": topic})

    # Search strategies based on depth
    queries = []
    if depth == "shallow":
        queries = [topic]
    elif depth == "standard":
        queries = [
            topic,
            f"{topic} guide tutorial",
            f"{topic} best practices",
        ]
    else:  # deep
        queries = [
            topic,
            f"{topic} comprehensive guide",
            f"{topic} research paper",
            f"{topic} implementation",
            f"latest {topic} 2025",
        ]

    all_results = []
    seen_urls = set()

    for query in queries[:max_sources]:
        try:
            # v1.2.0 (RG-4): ported from copy A — the blocking ddg_search call
            # must not stall the event loop (audit B6).
            results_json = await asyncio.to_thread(ddg_search, query, max_results=5)
            results = json.loads(results_json).get("results", [])
            for r in results:
                if r.get("href") and r["href"] not in seen_urls:
                    seen_urls.add(r["href"])
                    all_results.append(r)
        except (
            ConnectionError,
            OSError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as e:  # catch: NARROWED
            logger.warning(f"Search failed for '{query}': {e}")

    # Format as research report
    lines = [
        f"# Research Report: {topic}",
        "",
        "## Sources",
        "",
    ]

    for i, r in enumerate(all_results[:max_sources], 1):
        title = r.get("title", "Untitled")
        href = r.get("href", "")
        body = r.get("body", "")

        lines.append(f"### {i}. [{title}]({href})")
        lines.append("")
        lines.append(body[:500] + "..." if len(body) > 500 else body)
        lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"Found {len(all_results)} unique sources for '{topic}'.")
    lines.append(f"Search depth: {depth}")
    lines.append(f"Sources included: {min(len(all_results), max_sources)}")

    return json.dumps(
        {
            "report": "\n".join(lines),
            "sources_count": len(all_results),
            "topic": topic,
            "depth": depth,
        },
        indent=2,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Workflow Tools (Part 1) — list_available_workflows, route_query_to_workflow,
#   validate_workflow_file, estimate_workflow_cost
# ══════════════════════════════════════════════════════════════════════════════


async def list_available_workflows() -> str:
    """List all available Beagle workflows with their descriptions and phase counts.

    Returns a JSON array of workflow objects with name, description, and phase count.
    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        workflows = list_workflows()
        result = json.dumps(workflows, indent=2)
        duration = time.monotonic() - start_time
        record_metric("list_available_workflows", duration, success=True)
        logger.debug(f"[{correlation_id}] list_available_workflows completed in {duration:.4f}s")
        return result
    except (ValueError, OSError, RuntimeError) as e:
        duration = time.monotonic() - start_time
        record_metric("list_available_workflows", duration, success=False)
        logger.exception(f"[{correlation_id}] list_available_workflows failed: {e}")
        raise


async def route_query_to_workflow(query: str) -> str:
    """Analyze a query and recommend the best workflow to handle it.

    Uses keyword matching and optional LLM-based semantic classification
    to route queries to the most appropriate workflow.

    Args:
        query: The user's query or task description

    Returns:
        JSON with recommended workflow, confidence score, reasoning, and alternatives.

    """
    await _limiter.check()

    # Live-config: keep the tom Top-of-Mind in sync with canonical TOMLs.
    # render_canonical() is mtime-staleness-guarded (no-ops when fresh).
    try:
        from beagle.style_guides.render import (
            GooseTopOfMindRenderer,
        )

        GooseTopOfMindRenderer().render_canonical()
    except (ImportError, OSError, RuntimeError, ValueError) as _exc:
        logger.warning(
            "live-config render_canonical failed in route_query_to_workflow: %s",
            _exc,
            exc_info=True,
        )
        record_metric("render_canonical_failure", 0.0, success=False)

    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        result = route_query(query)
        duration = time.monotonic() - start_time
        record_metric("route_query_to_workflow", duration, success=True)
        logger.debug(f"[{correlation_id}] route_query_to_workflow completed in {duration:.4f}s")
        return json.dumps(
            {
                "workflow": result.workflow,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "alternatives": [
                    {"name": a[0], "score": round(a[1], 2)} for a in (result.alternatives or [])
                ],
            },
            indent=2,
        )
    except (ValueError, OSError, RuntimeError) as e:
        duration = time.monotonic() - start_time
        record_metric("route_query_to_workflow", duration, success=False)
        logger.exception(f"[{correlation_id}] route_query_to_workflow failed: {e}")
        raise


async def validate_workflow_file(workflow_name: str) -> str:
    """Validate a workflow YAML file without executing it.

    Checks for structural correctness: required fields, valid dependencies,
    no duplicate phases, no circular dependencies.

    Args:
        workflow_name: Workflow filename (e.g., 'research.yaml')

    Returns:
        JSON with validation status and any errors found.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        errors = validate_workflow(workflow_name)
        duration = time.monotonic() - start_time
        record_metric("validate_workflow_file", duration, success=True)
        logger.debug(f"[{correlation_id}] validate_workflow_file completed in {duration:.4f}s")
        return json.dumps(
            {
                "valid": len(errors) == 0,
                "errors": errors,
            },
            indent=2,
        )
    except (ValueError, OSError, RuntimeError) as e:
        duration = time.monotonic() - start_time
        record_metric("validate_workflow_file", duration, success=False)
        logger.exception(f"[{correlation_id}] validate_workflow_file failed: {e}")
        raise


async def estimate_workflow_cost(query: str, workflow_name: str = "research") -> str:
    """Estimate the token usage and cost for running a workflow.

    Provides a rough estimate based on query length, number of phases,
    and typical agent token usage patterns.

    Args:
        query: The query to estimate for
        workflow_name: Name of the workflow to estimate

    Returns:
        JSON with estimated tokens, cost, and per-phase breakdown.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        config = get_config()
        workflows = list_workflows()
        workflow_info = next((w for w in workflows if workflow_name in w["name"]), None)

        num_phases = workflow_info["phases"] if workflow_info else 4
        query_tokens = estimate_tokens_agnostic(query)

        # Rough estimates per phase
        avg_input_per_phase = query_tokens + 2000  # query + recipe + context
        avg_output_per_phase = 3000
        total_input = avg_input_per_phase * num_phases
        total_output = avg_output_per_phase * num_phases

        # Use default model pricing
        model = config.goose.default_model
        pricing = _get_pricing(model)
        estimated_cost = (total_input / 1_000_000) * pricing["input"] + (
            total_output / 1_000_000
        ) * pricing["output"]

        duration = time.monotonic() - start_time
        record_metric("estimate_workflow_cost", duration, success=True)
        logger.debug(f"[{correlation_id}] estimate_workflow_cost completed in {duration:.4f}s")

        return json.dumps(
            {
                "workflow": workflow_name,
                "model": model,
                "phases": num_phases,
                "estimated_input_tokens": total_input,
                "estimated_output_tokens": total_output,
                "estimated_total_tokens": total_input + total_output,
                "estimated_cost_usd": round(estimated_cost, 4),
                "note": "Rough estimate. Actual usage depends on agent output length.",
            },
            indent=2,
        )
    except (ValueError, OSError, RuntimeError) as e:
        duration = time.monotonic() - start_time
        record_metric("estimate_workflow_cost", duration, success=False)
        logger.exception(f"[{correlation_id}] estimate_workflow_cost failed: {e}")
        raise


# ══════════════════════════════════════════════════════════════════════════════
# Workflow Tools (Part 2) — run_beagle_workflow, get_agent_recipe, list_agents
# ══════════════════════════════════════════════════════════════════════════════


async def run_beagle_workflow(
    query: str,
    workflow_name: str = "research",
    budget_usd: float = 10.0,
    steering_prompt: str = "",
) -> str:
    """Execute an Beagle multi-agent workflow.

    Runs the specified workflow through the LangGraph orchestrator,
    coordinating multiple specialized agents to process the query.

    Available workflows: audit, research, develop, incident,
    security, db-migration, deep-planning, devops, self-improvement, verify.

    Args:
        query: The query or task to process
        workflow_name: Which workflow to run (default: research)
        budget_usd: Maximum budget in USD (default: 10.0)
        steering_prompt: Optional high-priority directive injected into all agents

    Returns:
        JSON with final report, completed nodes, cost summary, and any errors.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    logger.info(f"[{correlation_id}] Starting workflow: {workflow_name}")

    try:
        result = await _run_workflow_impl(query, workflow_name, budget_usd, steering_prompt)
        duration = time.monotonic() - start_time
        record_metric("workflow_run", duration, success=True)
        logger.info(f"[{correlation_id}] Workflow completed in {duration:.2f}s")
        return result
    except (ValueError, OSError, RuntimeError) as e:
        duration = time.monotonic() - start_time
        record_metric("workflow_run", duration, success=False)
        logger.error(f"[{correlation_id}] Workflow failed: {e}", exc_info=True)
        raise


async def _run_workflow_impl(
    query: str,
    workflow_name: str,
    budget_usd: float,
    steering_prompt: str,
) -> str:
    """Internal implementation of workflow execution."""
    # Validate query
    is_valid, error = await validate_query_async(query, mock_firewall=True)
    if not is_valid:
        return json.dumps(
            {
                "status": "error",
                "code": "INVALID_INPUT",
                "error": f"Query validation failed: {error}",
            }
        )

    try:
        # v13.21: Raise default cap from 600s to 1800s. See mcp_utility_server.py:1290
        # for full rationale. This is the orchestrator-side mirror; the MCP server
        # reads the same env var and the binding constraint is whichever fires first.
        _WORKFLOW_TIMEOUT_SECONDS = int(os.environ.get("BEAGLE_WORKFLOW_TIMEOUT", "1800"))
        result = await asyncio.wait_for(
            run_workflow(
                query=query,
                workflow_name=workflow_name,
                budget=budget_usd,
                steering=steering_prompt,
            ),
            timeout=_WORKFLOW_TIMEOUT_SECONDS,
        )

        # Scrub secrets from output
        final_report = scrub_secrets(result.get("final_report", ""))
        execution_ctx = scrub_secrets(result.get("raw_execution_context", ""))

        # v13.22.4 (P3-4): propagate the synthesis-failure check to the
        # MCP layer. autonomous_orchestrator.py runs the structural guard
        # on its own code path; the LangGraph code path (graph.py) does
        # not, so the MCP layer repeats the check at this boundary. A
        # synthesis failure is a distinct terminal status from
        # ``completed_with_errors`` so downstream consumers can react
        # without parsing the error strings.
        import re as _re_p34

        _nonempty_lines = [ln for ln in final_report.splitlines() if ln.strip()]
        _has_citation = bool(
            _re_p34.search(
                r"[A-Za-z0-9_./-]+\.(?:py|yaml|yml|md|json|toml):\d+",
                final_report,
            )
        )
        _synthesis_has_content = len(_nonempty_lines) >= 3 and (
            len(final_report) > 200 or _has_citation
        )
        _synthesis_failed = bool(final_report.strip()) and not _synthesis_has_content
        # Respect the orchestrator's own flag if it set one.
        if result.get("synthesis_failed"):
            _synthesis_failed = True

        if _synthesis_failed:
            _status = "synthesis_failed"
        elif result.get("errors"):
            _status = "completed_with_errors"
        else:
            _status = "completed"

        return json.dumps(
            {
                "status": _status,
                "synthesis_failed": _synthesis_failed,
                "final_report": final_report,
                "research_plan": result.get("research_plan", ""),
                "verified_facts": result.get("verified_facts", ""),
                "execution_context_preview": execution_ctx[:2000],
                "completed_nodes": result.get("completed_nodes", []),
                "total_tokens": result.get("total_tokens", 0),
                "total_cost_usd": result.get("total_cost", 0.0),
                "errors": result.get("errors", []),
            },
            indent=2,
        )

    except TimeoutError:
        logger.error(
            f"Workflow timed out after {_WORKFLOW_TIMEOUT_SECONDS}s: "
            f"query={query[:100]!r}, workflow={workflow_name}"
        )
        return json.dumps(
            {
                "status": "error",
                "code": "TIMEOUT",
                "error": (
                    f"Workflow execution timed out after {_WORKFLOW_TIMEOUT_SECONDS} seconds"
                ),
                "query": query[:200],
                "workflow": workflow_name,
            }
        )
    except (ValueError, OSError, RuntimeError) as e:
        logger.error(f"Workflow execution failed: {e}", exc_info=True)
        # Security: Don't leak internal error details to client
        error_msg = str(e)
        # Scrub any potential secrets from error message
        error_msg = scrub_secrets(error_msg)
        # Truncate long error messages to prevent information leakage
        if len(error_msg) > 500:
            error_msg = error_msg[:500] + "... (truncated)"
        return json.dumps(
            {
                "status": "error",
                "code": "EXECUTION_FAILED",
                "error": "Workflow execution failed",
                "details": error_msg,
            }
        )


async def get_agent_recipe(agent_name: str) -> str:
    """Retrieve the recipe/prompt template for a specific Beagle agent.

    Useful for understanding what each agent does and how it's configured.

    Args:
        agent_name: Agent name (e.g., 'research-planner', 'synthesis-writer')

    Returns:
        The agent recipe content or an error message.

    """
    await _limiter.check()
    # Validate agent_name — prevent path traversal
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$", agent_name):
        return json.dumps(
            {
                "status": "error",
                "code": "INVALID_INPUT",
                "error": (
                    f"Invalid agent_name: {agent_name!r} "
                    "(alphanumeric, hyphens, underscores only, max 64 chars)"
                ),
            }
        )

    workspace = get_workspace_root()
    recipe_path = (workspace / "recipes" / f"{agent_name}.xml").resolve()

    # Ensure resolved path is still under recipes/ (prevent traversal via symlinks)
    recipes_dir = (workspace / "recipes").resolve()
    if not is_path_within(recipe_path, recipes_dir):
        return json.dumps(
            {
                "status": "error",
                "code": "INVALID_PATH",
                "error": f"Unauthorized path for agent: {agent_name}",
            }
        )

    if not recipe_path.exists():
        available = [p.stem for p in (workspace / "recipes").glob("*.xml")]
        return json.dumps(
            {
                "status": "error",
                "code": "NOT_FOUND",
                "error": f"Recipe not found: {agent_name}",
                "available_agents": sorted(available),
            },
            indent=2,
        )

    content = recipe_path.read_text(encoding="utf-8")
    return json.dumps(
        {
            "agent": agent_name,
            "recipe": content,
        },
        indent=2,
    )


async def list_agents() -> str:
    """List all available Beagle agents with their descriptions.

    Returns:
        JSON array of agent objects with name and description.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    try:
        result = _list_agents_impl()
        duration = time.monotonic() - start_time
        record_metric("list_agents", duration, success=True)
        logger.debug(f"[{correlation_id}] list_agents completed in {duration:.4f}s")
        return result
    except (ValueError, OSError, RuntimeError) as e:
        duration = time.monotonic() - start_time
        record_metric("list_agents", duration, success=False)
        logger.exception(f"[{correlation_id}] list_agents failed: {e}")
        raise


def _list_agents_impl() -> str:
    """Internal implementation of agent listing.

    Returns agents from BOTH the agents.toml configuration AND the recipes
    directory, combining them with deduplication. Agents from agents.toml
    take priority (they have model/temperature overrides).
    """
    agents_by_name: dict[str, dict] = {}

    # 1. Load agents from agents.toml (has model/provider/temp overrides)
    try:
        from beagle.config.agent_config import list_agents as list_config_agents

        config_agents = list_config_agents()
        for name, profile in config_agents.items():
            agents_by_name[name] = {
                "name": profile.name,
                "description": profile.description or f"Agent: {profile.name}",
                "model": profile.model,
                "provider": profile.provider,
                "temperature": profile.temperature,
                "source": "agents.toml",
            }
    except (ValueError, OSError, RuntimeError) as e:
        logger.debug(f"Could not load agents from config: {e}")

    # 2. Load agents from recipes/ (supplementary, doesn't override agents.toml)
    workspace = get_workspace_root()
    recipes_dir = workspace / "recipes"

    for recipe_path in sorted(recipes_dir.glob("*.xml")):
        name = recipe_path.stem
        if name in agents_by_name:
            # Already covered by agents.toml — add recipe source tag
            agents_by_name[name]["source"] = "agents.toml+recipe"
            continue

        content = recipe_path.read_text(encoding="utf-8")
        # Extract description from YAML frontmatter
        description = ""
        if content.startswith("---"):
            try:
                import yaml

                end = content.index("---", 3)
                frontmatter = yaml.safe_load(content[3:end])
                description = frontmatter.get("description", "")
            except ImportError as exc:
                logger.warning(
                    "PyYAML is not installed (%s); recipe front matter cannot be "
                    "parsed and the description is left empty.",
                    exc,
                )

        # Infer model from recipe name/phase
        try:
            from beagle.context.recipe_agent_bridge import (
                _infer_model_for_phase,
                _infer_phase_from_name,
                _infer_temperature_for_phase,
            )

            phase = _infer_phase_from_name(name)
            model = _infer_model_for_phase(phase)
            temperature = _infer_temperature_for_phase(phase)
        except ImportError:
            model = "glm-5.1:cloud"
            temperature = 0.4

        agents_by_name[name] = {
            "name": name,
            "description": description or f"Agent: {name}",
            "model": model,
            "provider": "ollama_cloud",
            "temperature": temperature,
            "source": "recipe",
        }

    agents = list(agents_by_name.values())
    return json.dumps(agents, indent=2)


# ── Observability Tools ──────────────────────────────────────────────────────


async def get_metrics() -> str:
    """Return server metrics including request counts and latency statistics.

    Returns:
        JSON with request totals, success/error rates, and latency percentiles.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    logger.debug(f"[{correlation_id}] Metrics requested")
    return json.dumps(get_metrics_summary(), indent=2)


async def health_check() -> str:
    """Perform a comprehensive health check of the workflow server.

    Checks:
    - MCP server connectivity
    - Workflow loader availability
    - Config accessibility
    - Memory usage

    Returns:
        JSON with health status for each component.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    logger.info(f"[{correlation_id}] Health check initiated")

    health: dict[str, Any] = {
        "status": "healthy",
        "timestamp": time.time(),
        "correlation_id": correlation_id,
        "checks": {},
    }

    # Check 1: Config loading
    try:
        config = get_config()
        health["checks"]["config"] = {
            "status": "ok",
            "provider": config.goose.provider,
            "model": config.goose.default_model,
        }
    except (ValueError, OSError, RuntimeError) as e:
        health["checks"]["config"] = {"status": "error", "message": str(e)}
        health["status"] = "degraded"

    # Check 2: Workflow loader
    try:
        workflows = list_workflows()
        health["checks"]["workflows"] = {
            "status": "ok",
            "count": len(workflows),
        }
    except (ValueError, OSError, RuntimeError) as e:
        health["checks"]["workflows"] = {"status": "error", "message": str(e)}
        health["status"] = "degraded"

    # Check 3: Router
    try:
        routes = list_routable_workflows()
        health["checks"]["router"] = {
            "status": "ok",
            "routable_workflows": len(routes),
        }
    except (ValueError, OSError, RuntimeError) as e:
        health["checks"]["router"] = {"status": "error", "message": str(e)}
        health["status"] = "degraded"

    # Check 4: Memory usage
    try:
        import resource

        mem_usage = resource.getrusage(resource.RUSAGE_SELF)
        health["checks"]["memory"] = {
            "status": "ok",
            "max_rss_mb": round(mem_usage.ru_maxrss / 1024, 2),
            "shared_mb": round(mem_usage.ru_ixrss / 1024, 2),
            "unshared_mb": round(mem_usage.ru_idrss / 1024, 2),
        }
    except (ValueError, OSError, RuntimeError) as e:
        health["checks"]["memory"] = {"status": "unavailable", "message": str(e)}

    # Check 5: Metrics
    _ms = get_metrics_summary()
    health["checks"]["metrics"] = {
        "status": "ok",
        "total_requests": _ms["requests"]["total"],
        "success_rate": round(
            _ms["requests"]["success"] / max(_ms["requests"]["total"], 1) * 100,
            2,
        ),
    }

    duration = time.monotonic() - start_time
    health["checks"]["health_check_latency"] = f"{duration:.4f}s"
    record_metric("health_check", duration, success=True)
    logger.info(f"[{correlation_id}] Health check completed in {duration:.4f}s: {health['status']}")

    return json.dumps(health, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Context Fold Query — query_fold tool for semantic search in TurboQuant folds
# ══════════════════════════════════════════════════════════════════════════════


def _json_safe(obj: Any) -> Any:
    """Convert numpy types to JSON-serialisable Python natives."""
    import numpy as np

    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.float32 | np.float64):
        return float(obj)
    if isinstance(obj, np.int32 | np.int64):
        return int(obj)
    raise TypeError(f"Not serialisable: {type(obj)}")


async def query_fold(query: str, fold_id: str = "", top_k: int = 3) -> str:
    """Search compressed context folds for semantically relevant chunks.

    Uses sentence-transformers to embed the query and cosine similarity
    to rank chunks within a TurboQuant fold. Falls back to the most
    recent fold when fold_id is empty.

    Args:
        query: Natural language search query (max 1000 chars).
        fold_id: 12-hex-char fold ID. Empty = use most recent fold.
        top_k: Number of results to return (1-10).

    """
    await _limiter.check()
    start_time = time.monotonic()

    # Validate top_k
    if not isinstance(top_k, int) or top_k < 1 or top_k > 10:
        return json.dumps(
            {
                "status": "error",
                "message": f"top_k must be an integer in 1..10, got {top_k!r}",
            }
        )

    # Validate fold_id format
    if fold_id and not _FOLD_ID_RE.match(fold_id):
        return json.dumps(
            {
                "status": "error",
                "message": (
                    "fold_id must be exactly 12 lowercase hex characters "
                    f"(matching ^[0-9a-f]{{12}}$), got {fold_id!r}"
                ),
            }
        )

    # Sanitise query: strip control chars (keep newline/tab), cap length
    query = "".join(c for c in query if c in ("\n", "\t") or ord(c) >= 32)
    if len(query) > 1000:
        return json.dumps(
            {
                "status": "error",
                "message": f"query too long ({len(query)} chars, max 1000)",
            }
        )
    if not query.strip():
        return json.dumps(
            {
                "status": "error",
                "message": "query is empty or whitespace-only after sanitisation",
            }
        )

    # Resolve fold_id
    from beagle.context.compressed_store import (
        FoldNotFoundError,
        get_compressed_store,
    )

    store = get_compressed_store()

    target_fold_id = fold_id
    if not target_fold_id:
        available = store.list_folds()
        if not available:
            duration = time.monotonic() - start_time
            record_metric("query_fold", duration)
            return json.dumps(
                {
                    "status": "no_folds_available",
                    "message": (
                        "No compressed folds exist yet. Folds are created when "
                        "the compaction hook fires at 70% context utilisation."
                    ),
                }
            )
        target_fold_id = available[0]

    # Query the fold
    try:
        results = store.query_fold(query, target_fold_id, top_k)
    except FoldNotFoundError:
        available = store.list_folds()
        duration = time.monotonic() - start_time
        record_metric("query_fold", duration)
        return json.dumps(
            {
                "status": "fold_not_found",
                "requested_fold_id": target_fold_id,
                "available_fold_ids": available[:10],
            }
        )
    except (RuntimeError, OSError, ValueError) as exc:
        duration = time.monotonic() - start_time
        record_metric("query_fold", duration, success=False)
        return json.dumps(
            {
                "status": "error",
                "message": f"fold query failed: {exc}",
                "fold_id": target_fold_id,
            }
        )

    # Distinguish "fold exists but no matches" from "no folds"
    if not results:
        duration = time.monotonic() - start_time
        record_metric("query_fold", duration)
        return json.dumps(
            {
                "status": "no_matches",
                "fold_id": target_fold_id,
                "results": [],
            }
        )

    # Serialise with numpy-safe converter
    try:
        payload = json.dumps(
            {
                "status": "ok",
                "fold_id": target_fold_id,
                "results": results,
            },
            default=_json_safe,
        )
    except TypeError as exc:
        duration = time.monotonic() - start_time
        record_metric("query_fold", duration, success=False)
        return json.dumps(
            {
                "status": "error",
                "message": f"serialisation failed: {exc}",
            }
        )

    duration = time.monotonic() - start_time
    record_metric("query_fold", duration)
    return payload


# ══════════════════════════════════════════════════════════════════════════════
# Session Continuity — beagle_session_bootstrap, beagle_progress_update (v13.12.9)
# ══════════════════════════════════════════════════════════════════════════════
# Purpose: close Bug #3 (new sessions re-run completed work) by giving Goose a
# single tool call that surfaces .beagle/progress.md, .beagle/current_phase.md,
# recent commits, audit/ files, and running OpenClaw tasks. Pure read-only.
# beagle_progress_update replaces the fragile bash-heredoc pattern in the TOM
# prompt with an atomic single-call write via safe_file_ops.

_BEAGLE_DIR_NAME = ".beagle"
_AUDIT_DIR_NAME = "audit"
_PROGRESS_STALE_DAYS = 7
_GIT_LOG_LIMIT = 10
_AUDIT_PREVIEW_LINES = 20


def _repo_root() -> Path:
    """Return repository root — git toplevel if available, else CWD."""
    try:
        result = subprocess.run(
            [_GIT_BIN, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        logger.warning(
            "Cannot resolve the git repository root (%s); falling back to the "
            "current working directory %s.",
            exc,
            Path.cwd(),
        )
    return Path.cwd()


def _git_recent_commits(root: Path, limit: int = _GIT_LOG_LIMIT) -> list[dict[str, str]]:
    """Return up to *limit* recent commits as [{sha, subject}, ...]."""
    try:
        result = subprocess.run(
            [_GIT_BIN, "-C", str(root), "log", "--oneline", f"-{limit}", "--no-color"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return []
        commits: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                commits.append({"sha": parts[0], "subject": parts[1]})
        return commits
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return []


def _git_branch(root: Path) -> str:
    """Return current git branch, or 'unknown'."""
    try:
        result = subprocess.run(
            [_GIT_BIN, "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        logger.warning("Cannot resolve the current git branch (%s); reporting 'unknown'.", exc)
    return "unknown"


def _git_uncommitted(root: Path) -> list[str]:
    """Return list of files with uncommitted changes (porcelain format)."""
    try:
        result = subprocess.run(
            [_GIT_BIN, "-C", str(root), "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [line[3:] for line in result.stdout.splitlines() if line.strip()][:50]
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return []


def _read_beagle_artifact(root: Path, basename: str) -> dict[str, Any]:
    """Read .beagle/<basename>.xml preferentially, fall back to .md.

    Returns dict with `exists`, `path`, `format` (xml|md|none), `mtime_epoch`,
    `stale_days`, `is_stale`, and `content`. AI-authored artifacts are XML-
    first per v13.12.9 conversion; legacy MD files still read for back-compat.
    """
    beagle_dir = root / _BEAGLE_DIR_NAME
    xml_path = beagle_dir / f"{basename}.xml"
    md_path = beagle_dir / f"{basename}.md"
    if xml_path.exists():
        path, fmt = xml_path, "xml"
    elif md_path.exists():
        path, fmt = md_path, "md"
    else:
        return {"exists": False, "path": str(xml_path), "format": "none"}
    try:
        content = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
        stale_days = max(
            0,
            # wall-clock-ok: compares against a persisted file mtime (wall-clock
            # epoch). time.monotonic() would be WRONG here — the file mtime is
            # wall-clock, not monotonic. This is a timestamp comparison, which
            # the rule explicitly permits ("keep time.time() for timestamps").
            int((time.time() - mtime) / 86400),  # nosemgrep: aeca-walltime-for-interval
        )
        return {
            "exists": True,
            "path": str(path),
            "format": fmt,
            "mtime_epoch": int(mtime),
            "stale_days": stale_days,
            "is_stale": stale_days > _PROGRESS_STALE_DAYS,
            "content": content[:4000],
        }
    except (OSError, ValueError) as e:
        return {"exists": True, "path": str(path), "format": fmt, "error": str(e)}


def _read_progress_md(root: Path) -> dict[str, Any]:
    """Compat shim — read .beagle/progress.{xml,md}. Name kept for test stability."""
    return _read_beagle_artifact(root, "progress")


def _read_current_phase_md(root: Path) -> dict[str, Any]:
    """Compat shim — read .beagle/current_phase.{xml,md}. Name kept for test stability."""
    return _read_beagle_artifact(root, "current_phase")


def _list_audit_files(root: Path) -> list[dict[str, str]]:
    """Return audit/*.{xml,md} files with the first N lines preview."""
    audit_dir = root / _AUDIT_DIR_NAME
    if not audit_dir.is_dir():
        return []
    out: list[dict[str, str]] = []
    paths = sorted(audit_dir.glob("*.xml")) + sorted(audit_dir.glob("*.md"))
    for f in paths:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()[:_AUDIT_PREVIEW_LINES]
            out.append(
                {
                    "path": str(f.relative_to(root)),
                    "first_lines": "\n".join(lines),
                }
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "Cannot read audit artefact %s (%s); omitting it from the preview list.",
                f,
                exc,
            )
            continue
    return out


_NEXT_STEP_XML_RE = re.compile(r"<next_step>\s*(.*?)\s*</next_step>", re.IGNORECASE | re.DOTALL)


def _detect_resume_point(progress: dict[str, Any], phase: dict[str, Any]) -> str | None:
    """Extract next-step marker from XML <next_step> or legacy NEXT_STEP: line."""
    placeholders = {"(await user instruction)", "n/a", "none", "()", "(await)"}
    for source in (phase, progress):
        if not source.get("exists"):
            continue
        content = source.get("content", "")
        match = _NEXT_STEP_XML_RE.search(content)
        if match:
            raw_value = match.group(1)
            value = str(raw_value).strip() if raw_value is not None else ""
            if value and value.lower() not in placeholders:
                return value
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("NEXT_STEP:"):
                raw_value = stripped.split(":", 1)[1].strip()
                value = str(raw_value) if raw_value is not None else ""
                if value and value.lower() not in placeholders:
                    return value
    return None


async def beagle_session_bootstrap() -> str:
    """Single-call session resume kit — surfaces all in-progress Beagle state.

    Returns JSON containing: cwd, git branch + recent commits + uncommitted
    files, .beagle/progress.md content (with stale_days), .beagle/current_phase.md
    content, audit/*.md previews, and a heuristic resume_point. Goose should
    call this as the FIRST tool call of every new session to determine whether
    to resume an in-progress task or start fresh.

    Returns:
        JSON with keys: status, cwd, git_branch, git_recent_commits,
        uncommitted_files, progress_md, current_phase_md, audit_files,
        resume_point. resume_point is null if no actionable NEXT_STEP found.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    start_time = time.monotonic()
    logger.info(f"[{correlation_id}] beagle_session_bootstrap invoked")

    root = _repo_root()
    progress = _read_progress_md(root)
    phase = _read_current_phase_md(root)
    payload: dict[str, Any] = {
        "status": "ok",
        "correlation_id": correlation_id,
        "cwd": str(Path.cwd()),
        "repo_root": str(root),
        "git_branch": _git_branch(root),
        "git_recent_commits": _git_recent_commits(root),
        "uncommitted_files": _git_uncommitted(root),
        "progress_md": progress,
        "current_phase_md": phase,
        "audit_files": _list_audit_files(root),
        "resume_point": _detect_resume_point(progress, phase),
    }

    # Live-config: refresh the tom Top-of-Mind file from canonical TOMLs.
    # render_canonical() is mtime-staleness-guarded (no-ops when fresh).
    try:
        from beagle.style_guides.render import (
            GooseTopOfMindRenderer,
        )

        GooseTopOfMindRenderer().render_canonical()
    except (ImportError, OSError, RuntimeError, ValueError) as _exc:
        logger.warning(
            f"[{correlation_id}] live-config render_canonical failed in beagle_session_bootstrap: {_exc}",
            exc_info=True,
        )
        record_metric("render_canonical_failure", 0.0, success=False)

    # v13.15.7: Include the Beagle doctrine bundle so any MCP client gets
    # the contract on first call — independent of goose's tom extension.
    # The bundle is capped at 25 KB by the renderer.
    try:
        payload["doctrine"] = GooseTopOfMindRenderer().render_structured()
    except (ImportError, OSError, RuntimeError, ValueError) as _exc:
        logger.warning(f"[{correlation_id}] doctrine bundle render failed (non-fatal): {_exc}")

    # v13.15.5: Include post-compaction rehydration prompt if available.
    # After goose compacts, agents call bootstrap to reorient — including
    # the rehydration prompt in the response saves an extra tool call.
    _rehyd_path = Path.home() / ".beagle" / "post_compaction_rehydration.txt"
    try:
        if _rehyd_path.exists():
            _rehyd = _rehyd_path.read_text()
            if _rehyd.strip():
                payload["post_compaction_rehydration"] = {
                    "available": True,
                    "path": str(_rehyd_path),
                    "prompt_length_chars": len(_rehyd),
                    "prompt": _rehyd,
                    "instruction": (
                        "CONTEXT WAS COMPACTED. Read the rehydration prompt above "
                        "in full — it contains your task context, completed nodes, "
                        "and the AutonomousResumeDirective. Continue execution "
                        "immediately. Do NOT stop. Do NOT ask questions."
                    ),
                }
                logger.info(
                    f"[{correlation_id}] bootstrap: included {len(_rehyd)}-char rehydration prompt"
                )
    except OSError as exc:
        logger.warning(
            "Cannot read the rehydration prompt (%s); the bootstrap response omits it.",
            exc,
        )

    duration = time.monotonic() - start_time
    record_metric("beagle_session_bootstrap", duration, success=True)
    logger.info(f"[{correlation_id}] beagle_session_bootstrap completed in {duration:.4f}s")
    return json.dumps(payload, indent=2)


async def read_session_state() -> str:
    """Hallucination-rejector for the generic ``read`` tool name.

    v13.15.9: previously this was a silent alias to beagle_session_bootstrap.
    That was intended to gracefully redirect, but in practice the registered
    name *taught* models to call ``read`` as a file reader — exactly the
    behaviour the doctrine warns against.  The alias enabled the hallucination
    instead of preventing it.

    Now it returns an explicit directive payload so the model learns the
    correct routing on the spot: shell+cat for files, beagle_session_bootstrap
    for session state.  The response is still valid JSON (so goose's tool
    transport doesn't error) but carries a directive_violation key the
    orchestrator can detect.
    """
    return json.dumps(
        {
            "status": "directive_violation",
            "tool_called": "read",
            "directive": (
                "The 'read' tool is NOT a file reader. To read a file: use "
                "shell + cat or sed -n. To recover Beagle session state: call "
                "beagle_session_bootstrap. This call has been logged as a "
                "doctrine violation (CRITICAL_ROUTING_PROTOCOL.tool_name_compliance)."
            ),
            "remediation": (
                "If you intended to read a file, call shell with cmd='cat <path>' "
                "or sed -n. If you intended to recover session state, call "
                "beagle_session_bootstrap (no arguments)."
            ),
        },
        indent=2,
    )


async def post_compaction_rehydrate() -> str:
    """v13.15.5: Read the post-compaction rehydration sidecar file.

    After goose compacts its context window, Beagle's rehydration prompt
    is written to ~/.beagle/post_compaction_rehydration.txt. This tool
    reads that file and returns its contents — call this immediately
    after detecting that context was compacted (e.g., after calling
    report_context_usage and seeing percentage drop suddenly, or after
    reading session_state which triggers when a model is confused).

    The rehydration prompt contains:
    1. Beagle system identity and anti-stall rules
    2. Task context and completed nodes
    3. The AutonomousResumeDirective — CONTINUE, don't ask questions

    Returns:
        JSON with status, rehydration_prompt (the full text), and
        sidecar_path. The agent should INJECT this prompt as its
        next-turn context. Never stop or ask questions after reading this.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    logger.info(f"[{correlation_id}] post_compaction_rehydrate invoked")

    sidecar_path = Path.home() / ".beagle" / "post_compaction_rehydration.txt"

    result: dict[str, Any] = {
        "status": "ok",
        "sidecar_path": str(sidecar_path),
        "rehydration_prompt": "",
        "found": False,
    }

    if not sidecar_path.exists():
        result["status"] = "no_sidecar"
        result["message"] = (
            "No post-compaction rehydration file found. If context was just "
            "compacted, call beagle_session_bootstrap to reconstruct state from "
            "progress.xml."
        )
        record_metric("post_compaction_rehydrate", 0.0, success=True)
        return json.dumps(result)

    try:
        prompt = sidecar_path.read_text()
        if not prompt.strip():
            result["status"] = "empty_sidecar"
            result["message"] = "Rehydration file exists but is empty."
            record_metric("post_compaction_rehydrate", 0.0, success=True)
            return json.dumps(result)

        result["found"] = True
        result["rehydration_prompt"] = prompt
        result["prompt_length_chars"] = len(prompt)
        result["instruction"] = (
            "The rehydration_prompt field contains your post-compaction "
            "context. Read it in full. It contains your task context, "
            "completed nodes, and the AutonomousResumeDirective. Continue "
            "execution immediately — do NOT stop, do NOT ask questions."
        )

        logger.info(
            f"[{correlation_id}] post_compaction_rehydrate: "
            f"returned {len(prompt)}-char rehydration prompt"
        )
    except OSError as exc:
        result["status"] = "error"
        result["message"] = f"Failed to read rehydration file: {exc}"

    record_metric("post_compaction_rehydrate", 0.0, success=result["found"])
    return json.dumps(result)


def _xml_escape(text: str) -> str:
    """XML-escape `&`, `<`, `>`, `"`. Sufficient for element bodies + attrs."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


async def beagle_progress_update(
    step_just_done: str,
    next_step: str,
    remaining: list[str] | None = None,
    critical_context: str = "",
) -> str:
    """Atomically write .beagle/progress.xml with structured progress markers.

    Replaces both the fragile bash-heredoc pattern AND the prior MD format
    (v13.12.9): AI-authored AND AI-consumed artifacts now use XML for
    parser-friendliness. The watchdog (`scripts/beagle_watchdog.py`) reads
    this file's mtime to detect compliance failures (sessions over 5 minutes
    that never call this tool are logged as drift).

    Args:
        step_just_done: One-line description of the work just completed.
        next_step: One-line description of the next planned step.
        remaining: Optional list of pending steps after next_step.
        critical_context: Optional free-form notes (paths, values to remember).

    Returns:
        JSON with status, path, bytes_written.

    """
    from beagle.utils.safe_file_ops import safe_write

    await _limiter.check()

    # Live-config: keep the tom Top-of-Mind in sync with canonical TOMLs.
    # render_canonical() is mtime-staleness-guarded (no-ops when fresh).
    try:
        from beagle.style_guides.render import (
            GooseTopOfMindRenderer,
        )

        GooseTopOfMindRenderer().render_canonical()
    except (ImportError, OSError, RuntimeError, ValueError) as _exc:
        logger.warning(
            "live-config render_canonical failed in beagle_progress_update: %s",
            _exc,
            exc_info=True,
        )
        record_metric("render_canonical_failure", 0.0, success=False)
    correlation_id = set_correlation_id()
    logger.info(f"[{correlation_id}] beagle_progress_update invoked")

    if not isinstance(step_just_done, str) or not step_just_done.strip():
        return json.dumps({"status": "error", "message": "step_just_done is required"})
    if not isinstance(next_step, str) or not next_step.strip():
        return json.dumps({"status": "error", "message": "next_step is required"})

    remaining_list = remaining if isinstance(remaining, list) else []
    remaining_block = (
        "\n".join(f"    <step>{_xml_escape(s)}</step>" for s in remaining_list)
        if remaining_list
        else ""
    )

    root = _repo_root()
    target = root / _BEAGLE_DIR_NAME / "progress.xml"
    legacy_md = root / _BEAGLE_DIR_NAME / "progress.md"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    content = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<beagle_progress version="2" updated="{timestamp}" '
        f'updated_by="beagle_progress_update">\n'
        f"  <step_just_done>{_xml_escape(step_just_done.strip())}</step_just_done>\n"
        f"  <next_step>{_xml_escape(next_step.strip())}</next_step>\n"
        f"  <remaining_steps>{('' if not remaining_block else chr(10) + remaining_block + chr(10) + '  ')}</remaining_steps>\n"
        f"  <critical_context>{_xml_escape(critical_context.strip()) or '(none)'}</critical_context>\n"
        f"</beagle_progress>\n"
    )

    import contextlib

    try:
        safe_write(target, content)
        # Remove the legacy MD progress file once XML write succeeds, so the
        # watchdog and bootstrap helpers don't see two contradictory sources.
        if legacy_md.exists():
            with contextlib.suppress(OSError):
                legacy_md.unlink()
    except (OSError, ValueError) as e:
        logger.error(f"[{correlation_id}] beagle_progress_update failed: {e}")
        return json.dumps({"status": "error", "message": str(e)[:300]})

    record_metric("beagle_progress_update", 0.0, success=True)
    logger.info(f"[{correlation_id}] beagle_progress_update wrote {len(content)} bytes to {target}")
    return json.dumps(
        {
            "status": "ok",
            "path": str(target),
            "format": "xml",
            "bytes_written": len(content),
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# Context Management — report_context_usage, check_and_fold_context
# ══════════════════════════════════════════════════════════════════════════════


async def report_context_usage(
    percentage: float,
    used_tokens: int = 0,
    max_tokens: int = 0,
) -> str:
    """Lightweight context heartbeat — report utilization between fold cycles.

    v13.15.5: This is the LIGHTWEIGHT between-folds check. For the primary
    context management call, use check_and_fold_context instead — it
    saves checkpoints and builds TurboQuant folds.

    Call this every 1-2 turns between folds for a quick status heartbeat.
    If this returns recommendation="approaching_pre_compact" or above,
    call check_and_fold_context on the NEXT turn.

    Args:
        percentage: Fraction of context window used (0.0-1.0, e.g. 0.59).
        used_tokens: Approximate tokens used (optional, derived if 0).
        max_tokens: Maximum context window tokens (optional, uses config default).

    Returns:
        JSON with status, percentage, and recommendation.

    """
    from beagle.context.context_compaction_hook import get_monitor
    from beagle.context.context_reporter import write_report

    await _limiter.check()
    correlation_id = set_correlation_id()
    logger.info(f"[{correlation_id}] report_context_usage invoked: {percentage:.2%}")

    # Clamp percentage
    percentage = max(0.0, min(1.0, float(percentage)))

    # Write the report file
    try:
        write_report(percentage, used_tokens, max_tokens, source="mcp_tool")
    except (OSError, ValueError) as exc:
        logger.error(f"[{correlation_id}] report_context_usage write failed: {exc}")
        return json.dumps({"status": "error", "message": str(exc)[:300]})

    # Check compaction thresholds
    try:
        monitor = get_monitor()
        status = monitor.get_status()
        should_compact = status.should_compact
        level = status.warning_level
    except (ImportError, RuntimeError, OSError):
        should_compact = False
        level = "unknown"

    recommendation = "continue"
    if should_compact:
        recommendation = "compact_now"
    elif level == "warning":
        recommendation = "approaching_threshold"

    record_metric("report_context_usage", 0.0, success=True)
    return json.dumps(
        {
            "status": "ok",
            "percentage": round(percentage, 4),
            "level": level,
            "should_compact": should_compact,
            "recommendation": recommendation,
        }
    )


async def check_and_fold_context(
    percentage: float,
    used_tokens: int = 0,
    max_tokens: int = 0,
    query: str = "",
    project_dir: str = "",
) -> str:
    """Check context usage and trigger TurboQuant folding if past threshold.

    Combines context reporting with proactive compaction in a single call.
    If context is at or above the compaction threshold (default 70%),
    this will:
      1. Save a compaction checkpoint
      2. Build the TurboQuant skeleton and fold compressed context
      3. Return the rehydration prompt to inject after compaction

    If below threshold, returns current status and recommendation.

    Args:
        percentage: Fraction of context window used (0.0-1.0, e.g. 0.72).
        used_tokens: Approximate tokens used (optional, derived if 0).
        max_tokens: Maximum context window tokens (optional).
        query: Current task description (for rehydration prompt).
        project_dir: Project root directory (for context file discovery).

    Returns:
        JSON with status, compaction info, and rehydration prompt if folded.

    """
    from beagle.context.context_compaction_hook import (
        get_monitor,
    )
    from beagle.context.context_reporter import write_report
    from beagle.context.post_compaction_rehydration import (
        build_lightweight_rehydration,
        on_post_compaction,
    )

    # Live-config: keep the tom Top-of-Mind in sync with the canonical style-guide
    # TOMLs. render_canonical() is mtime-staleness-guarded (no-ops when fresh), so
    # this is cheap to call every turn. Best-effort — never let it break the tool.
    try:
        from beagle.style_guides.render import GooseTopOfMindRenderer

        GooseTopOfMindRenderer().render_canonical()
    except (OSError, ImportError, RuntimeError, ValueError) as _e:
        logger.warning(
            "live-config render_canonical failed in check_and_fold_context: %s",
            _e,
            exc_info=True,
        )
        record_metric("render_canonical_failure", 0.0, success=False)

    await _limiter.check()
    correlation_id = set_correlation_id()
    logger.info(f"[{correlation_id}] check_and_fold_context invoked: {percentage:.2%}")

    # Clamp percentage
    percentage = max(0.0, min(1.0, float(percentage)))

    # Write the report (non-fatal — we still check thresholds)
    import contextlib

    with contextlib.suppress(OSError, ValueError):
        write_report(percentage, used_tokens, max_tokens, source="mcp_tool")

    # Get config thresholds (v13.15.5: pre_compact is the fold trigger, not compact)
    try:
        config = get_config()
        pre_compact_threshold = getattr(config.context_threshold, "pre_compact", 0.58)
        compact_threshold = config.context_threshold.compact
        critical_threshold = config.context_threshold.critical
    except (AttributeError, RuntimeError):
        pre_compact_threshold = 0.58
        compact_threshold = 0.70
        critical_threshold = 0.85

    # Build rehydration info
    result: dict[str, Any] = {
        "status": "ok",
        "percentage": round(percentage, 4),
        "pre_compact_threshold": pre_compact_threshold,
        "compact_threshold": compact_threshold,
        "critical_threshold": critical_threshold,
    }

    if percentage < pre_compact_threshold:
        # Below pre-compact — no folding needed, Beagle has plenty of headroom
        result["action"] = "continue"
        result["recommendation"] = (
            f"Context at {percentage:.0%} — below {pre_compact_threshold:.0%} pre-compact "
            f"threshold. Plenty of headroom. Continue normally."
        )
        record_metric("check_and_fold_context", 0.0, success=True)
        return json.dumps(result)

    # At or above pre_compact threshold — execute Beagle-controlled fold
    # v13.15.6: This fires BEFORE goose's internal compaction, giving Beagle
    # the chance to save state and deliver an actionable rehydration prompt.
    try:
        monitor = get_monitor()
        monitor.record_compaction(
            context_size=int(percentage * (max_tokens or 128000)),
            node_name="mcp_check_and_fold",
        )
    except (ImportError, RuntimeError, OSError) as exc:
        logger.warning(
            "Cannot record the compaction event with the context monitor (%s); "
            "the fold proceeds but this compaction is missing from the monitor's history.",
            exc,
        )

    # Build rehydration prompt (comprehensive version for the sidecar)
    # on_post_compaction already imported at top of function
    rehydration_full = on_post_compaction(
        workflow_id="cli_session",
        query=query,
        completed_nodes=[],
    )
    # Also build the lightweight version for inline return
    rehydration = build_lightweight_rehydration(
        query=query,
        workflow_id="cli_session",
        completed_nodes=[],
    )

    # Write rehydration to sidecar so it survives goose-level compaction
    try:
        sidecar_path = Path.home() / ".beagle" / "post_compaction_rehydration.txt"
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(rehydration_full)
        logger.info(f"[{correlation_id}] Rehydration sidecar written: {sidecar_path}")
    except OSError as exc:
        logger.warning(f"[{correlation_id}] Rehydration sidecar write failed: {exc}")

    # Create a TurboQuant context fold via ContextIntegration
    fold_id = ""
    try:
        from beagle.context.context_integration import (
            get_context_integration,
        )

        integration = get_context_integration()
        # Build data payload for the fold from available context state
        import datetime as _dt

        data_payload = (
            f"# Context Fold — {_dt.datetime.now(_dt.UTC).isoformat()}\n"
            f"Query: {query or '(none)'}\n"
            f"Context usage: {percentage:.1%} ({used_tokens}/{max_tokens})\n"
            f"Level: {'critical' if percentage >= critical_threshold else 'compact'}\n"
            f"Rehydration prompt: {rehydration}\n"
        )
        # Actually create the fold — this was the missing call
        await integration.enhanced_context_fold(data_payload, "turboquant")
        fold_stats = integration.get_stats()
        if fold_stats and "turbo_fold_id" in fold_stats.get("integration", {}):
            fold_id = fold_stats["integration"]["turbo_fold_id"] or ""
        logger.info(f"[{correlation_id}] TurboQuant fold created: {fold_id}")
    except (ImportError, RuntimeError, OSError, ValueError, AttributeError) as exc:
        logger.warning(f"[{correlation_id}] ContextIntegration fold failed: {exc}")

    result["action"] = "compact_now"
    result["level"] = "critical" if percentage >= critical_threshold else "compact"
    result["recommendation"] = (
        f"Context at {percentage:.0%} — "
        f"{'CRITICAL' if percentage >= critical_threshold else 'at'} "
        f"threshold. Compaction is required. Use the rehydration prompt below."
    )
    if fold_id:
        result["fold_id"] = fold_id
    result["rehydration_prompt"] = rehydration

    record_metric("check_and_fold_context", 0.0, success=True)
    return json.dumps(result)


async def enforce_post_final_answer_fold(
    workflow_id: str = "cli_session",
    query: str = "",
    completed_nodes: list[str] | None = None,
    project_dir: str = "",
    percentage: float = 0.0,
) -> str:
    """Runtime-side enforcement of the post_final_answer_fold TOML contract.

    v13.21.13: The ``<post_final_answer_fold required="true"/>` rule has been
    declared in the Top-of-Mind since v13.15.5, but enforcement was delegated
    to the model (it had to remember to call ``check_and_fold_context`` after
    every ``</final_answer>``). That pattern is unreliable across providers —
    a deepseek session in Projects/Skylon_Ecosystem/skylon recorded the fold
    as its next step in progress.xml and then never executed it. The result
    was a session that completed without writing a rehydration sidecar,
    forcing the next session to re-bootstrap from scratch.

    This tool is the deterministic, runtime-side enforcement hook. Unlike
    :func:`check_and_fold_context`, it ALWAYS fires the fold and ALWAYS
    writes the rehydration sidecar, regardless of the supplied ``percentage``.
    A session ending at 30% context still gets a sidecar so the next session
    can rehydrate cleanly.

    The orchestrator (autonomous_orchestrator._run_finalize) calls this
    UNCONDITIONALLY at workflow completion. The MCP surface also exposes it
    so external harnesses (CLI shutdown hooks, goose's response envelope
    post-processor, test runners) can invoke the same path.

    Args:
        workflow_id: Current workflow / session identifier.
        query: Current task description (used in the sidecar).
        completed_nodes: Nodes completed so far (used in the sidecar).
        project_dir: Project root for context-file discovery (optional).
        percentage: Reported context-usage fraction (stored as metadata only;
            does NOT gate the fold).

    Returns:
        JSON with: status, action (always "compact_now"), workflow_id,
        sidecar_path, sidecar_chars, fold_id, percentage_reported.

    """
    await _limiter.check()
    correlation_id = set_correlation_id()
    logger.info(
        f"[{correlation_id}] enforce_post_final_answer_fold invoked: "
        f"workflow={workflow_id} percentage={percentage:.2%}"
    )

    from beagle.context.post_compaction_rehydration import (
        enforce_post_final_answer_fold as _enforce,
    )

    project_root: Path | None = None
    if project_dir:
        project_root = Path(project_dir)

    fold_result = _enforce(
        workflow_id=workflow_id,
        query=query,
        completed_nodes=list(completed_nodes or []),
        project_dir=project_root,
        percentage=percentage,
    )

    fold_result["correlation_id"] = correlation_id
    record_metric("enforce_post_final_answer_fold", 0.0, success=True)
    return json.dumps(fold_result)


# ══════════════════════════════════════════════════════════════════════════════
# Resources — beagle://config, beagle://workflows, beagle://routing-rules
# ══════════════════════════════════════════════════════════════════════════════


async def get_beagle_config() -> str:
    """Current Beagle configuration."""
    config = get_config()
    return json.dumps(
        {
            "version": "12.0.0",
            "goose_binary": config.goose.binary_path,
            "default_model": config.goose.default_model,
            "provider": config.goose.provider,
            "default_budget_usd": config.budget.default_usd,
            "hard_limit_usd": config.budget.hard_limit_usd,
            "cache_enabled": config.cache.enabled,
        },
        indent=2,
    )


async def get_workflows_resource() -> str:
    """Available workflow definitions."""
    return json.dumps(list_workflows(), indent=2)


async def get_routing_rules() -> str:
    """Workflow routing keywords and patterns."""
    return json.dumps(list_routable_workflows(), indent=2)
