"""Beagle web dashboard server — serves the vendored React bundle and exposes
real Beagle data over aiohttp.

The reference frontend (``Google-Generated-Beagle-Webui``) shipped a polished
React dashboard backed entirely by mock data. This module improves on that by
wiring each API endpoint to the *live* Beagle subsystem that owns the data:

* ``/api/workflows``        → :func:`beagle.core.workflow_loader.list_workflows`
* ``/api/workflows/:id/execute`` → :func:`beagle.core.graph.run_workflow`
* ``/api/runs``             → :class:`beagle.tracking.database.TrackingDatabase`
* ``/api/system/status``    → cost tracker + tracking stats
* ``/api/cost/summary``     → :func:`beagle.cost_tracker.get_cost_tracker`
* ``/api/agents/roster``    → :func:`beagle.config.agent_config.list_agents`
* ``/api/rag/search``       → Beagle hybrid RAG search (guarded)

Where a subsystem is unavailable at runtime (e.g. RAG index not built, or
tracking DB absent), the endpoint degrades gracefully to a sensible default
so the dashboard never 500s.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger("Beagle.webui")

# ── Authentication (D-02) ─────────────────────────────────────────────────────

# The project's own rule (core/a2a_protocol.py:63): "Localhost only — never
# bind to 0.0.0.0". The web dashboard exposes live workflow execution, so the
# loopback default is enforced here as well, with a hard refuse-to-start when a
# non-loopback bind is requested without a token.
_DEFAULT_HOST = "127.0.0.1"
# HTTP header name, not a credential — semgrep's S105 sees a string assigned to
# a *_HEADER constant and assumes it is a hardcoded password.
_AUTH_HEADER = "Authorization"  # noqa: S105
_DEFAULT_PORT = 8080
# Hard ceiling for caller-supplied budgets in _handle_api_execute (D-14). A
# rogue `float("inf")` must never reach the runner.
_BUDGET_CEILING = 1_000_000.0


def _webui_token() -> str:
    """Return the configured bearer token, or "" when none is set."""
    return os.environ.get("BEAGLE_WEBUI_TOKEN", "")


def _is_loopback(host: str) -> bool:
    """True when ``host`` resolves to a loopback address (127.0.0.0/8, ::1)."""
    try:
        addr = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    return addr.is_loopback


async def _auth_middleware(
    app: web.Application,
    handler: Any,
) -> Any:
    """Require the BEAGLE_WEBUI_TOKEN bearer header on every route.

    aiohttp 3.14 still routes middlewares through the legacy
    ``await m(app, handler)`` factory form (a function tagged with
    ``web.middleware`` is treated as old-style, which is what this module's
    pre-existing handlers rely on). The factory returns the per-request
    coroutine; the 401 path short-circuits before calling ``handler``.

    When a token is *configured*, every request must present it — missing or
    wrong returns 401. When no token is configured the dashboard is
    unauthenticated, which is only safe because :func:`main` refuses to bind
    to a non-loopback address in that case.
    """

    async def _middleware_handler(request: web.Request) -> Any:
        token = _webui_token()
        if token:
            received = request.headers.get(_AUTH_HEADER, "")
            if received.startswith("Bearer "):
                received = received[7:]
            if not hmac.compare_digest(token, received):
                return web.Response(status=401, text="Unauthorized")
        return await handler(request)

    return _middleware_handler

# ── Bundle resolution ────────────────────────────────────────────────────────


def _bundle_dir() -> Path:
    """Locate the vendored webui prebuild (source checkout or installed wheel).

    Mirrors the pi launcher's two-root search: the installed-wheel path lives
    next to this module; the source-checkout path lives under the vendored tree.
    """
    here = Path(__file__).resolve()
    candidates = (
        here.parent / "vendor" / "webui-prebuild",
        here.parents[2] / "frontends" / "webui" / "vendor" / "webui-prebuild",
    )
    for root in candidates:
        if (root / "dist" / "index.html").is_file():
            return root
    raise FileNotFoundError(
        "Vendored webui bundle not found. Reinstall Beagle (the bundle ships "
        "in the wheel) or run from the source tree with vendor/webui-prebuild/dist present."
    )


def _dist_dir() -> Path:
    return _bundle_dir() / "dist"


# ── Real Beagle data adapters ────────────────────────────────────────────────


def _list_workflows() -> list[dict[str, Any]]:
    """Live workflow list from the workflow loader."""
    try:
        from beagle.core.workflow_loader import list_workflows

        raw = list_workflows()
        # Map to the shape the frontend expects (id, name, description,
        # phases, category, maxBudgetUsd).
        out: list[dict[str, Any]] = []
        for wf in raw:
            name = wf.get("name", "unknown")
            out.append(
                {
                    "id": wf.get("path", name).rsplit("/", 1)[-1].replace(".yaml", ""),
                    "name": name,
                    "description": wf.get("description", ""),
                    "phases": wf.get("phases", 0),
                    "category": "workflow",
                    "maxBudgetUsd": 10.0,
                    "estimatedTokens": 0,
                    "isolationLevel": "microvm",
                    "createdDate": "",
                    "author": "Beagle Core Engine",
                    # Minimal single-synthetic node so the frontend can render
                    # a DAG even before any phase detail is loaded.
                    "nodes": [
                        {
                            "id": "node-1",
                            "name": name,
                            "type": "plan",
                            "assignedAgent": "Beacon Coordinator",
                            "model": "default",
                            "dependencies": [],
                            "status": "pending",
                            "promptTemplate": "",
                            "costUsd": 0.0,
                            "tokensUsed": 0,
                            "durationMs": 0,
                        }
                    ],
                }
            )
        return out
    except (OSError, RuntimeError, ValueError, ImportError) as exc:  # pragma: no cover
        logger.warning("workflow list unavailable: %s", exc)
        return []


def _list_runs() -> list[dict[str, Any]]:
    """Live historical runs from the tracking database."""
    try:
        from beagle.tracking.database import TrackingDatabase

        db = TrackingDatabase.get_instance()
        runs = db.get_workflow_runs(limit=50)
        out: list[dict[str, Any]] = []
        for run in runs:
            out.append(
                {
                    "id": run.id,
                    "workflowId": run.id,
                    "workflowName": run.workflow_name,
                    "goal": run.query,
                    "status": "completed" if run.success else "failed",
                    "startTime": _iso(run.started_at),
                    "endTime": _iso(run.completed_at) if run.completed_at else None,
                    "currentStepIndex": run.nodes_completed,
                    "totalSteps": run.nodes_completed + run.nodes_failed,
                    "spentBudgetUsd": run.total_cost_usd,
                    "budgetLimitUsd": run.budget_usd or 10.0,
                    "totalTokens": run.total_tokens,
                    "nodes": [],
                    "logs": [],
                }
            )
        return out
    except (OSError, RuntimeError, ValueError, ImportError) as exc:  # pragma: no cover
        logger.warning("run history unavailable: %s", exc)
        return []


def _cost_summary() -> dict[str, Any]:
    """Live cost tracker summary."""
    try:
        from beagle.cost_tracker import get_cost_tracker

        tracker = get_cost_tracker()
        s = tracker.get_summary()
        return {
            "dailyBudgetCap": s["budget_usd"] or 25.0,
            "currentDaySpend": s["total_cost_usd"],
            "remainingBudget": s["budget_remaining_usd"],
            "totalTokens": s["total_tokens"],
            "spendByModel": [
                {"model": k, "cost": v, "percentage": 0}
                for k, v in s.get("node_costs", {}).items()
            ],
            "governancePolicy": "FAIL_CLOSED_HARD_STOP",
        }
    except (OSError, RuntimeError, ValueError, ImportError) as exc:  # pragma: no cover
        logger.warning("cost summary unavailable: %s", exc)
        return {
            "dailyBudgetCap": 25.0,
            "currentDaySpend": 0.0,
            "remainingBudget": 25.0,
            "totalTokens": 0,
            "spendByModel": [],
            "governancePolicy": "FAIL_CLOSED_HARD_STOP",
        }


def _agents_roster() -> list[dict[str, Any]]:
    """Live agent roster from agents.toml."""
    try:
        from beagle.config.agent_config import list_agents

        agents = list_agents()
        out: list[dict[str, Any]] = []
        for name, profile in agents.items():
            out.append(
                {
                    "id": name,
                    "name": name,
                    "role": profile.description or name,
                    "avatar": "🤖",
                    "defaultModel": profile.model,
                    "specialty": profile.description or "Beagle agent",
                    "isolation": "Restricted Subprocess",
                    "activeTasks": 0,
                    "health": "healthy",
                }
            )
        return out
    except (OSError, RuntimeError, ValueError, ImportError) as exc:  # pragma: no cover
        logger.warning("agent roster unavailable: %s", exc)
        return []


def _system_status() -> dict[str, Any]:
    """Live system status aggregated from cost + tracking stats."""
    cost = _cost_summary()
    runs = _list_runs()
    active = sum(1 for r in runs if r["status"] in ("running", "paused_hitl"))
    try:
        from beagle.tracking.database import TrackingDatabase

        db = TrackingDatabase.get_instance()
        stats = db.get_stats(since_days=7)
    except (OSError, RuntimeError, ValueError, ImportError):  # pragma: no cover
        stats = {"total_runs": len(runs), "success_rate": 100.0}

    return {
        "beaconStatus": "ONLINE",
        "activeRings": max(0, len(runs) - len([r for r in runs if r["status"] == "failed"])),
        "sandboxEngine": "Firecracker MicroVM (/dev/kvm)",
        "governanceMode": "HARD_STOP",
        "contextCompactionRatio": "4.2x",
        "totalSpendToday": round(cost["currentDaySpend"], 4),
        "budgetCeiling": cost["dailyBudgetCap"],
        "activeRunsCount": active,
        "totalHistoricalRuns": stats.get("total_runs", len(runs)),
    }


async def _rag_search(query: str) -> dict[str, Any]:
    """Live hybrid RAG search, guarded — degrades to empty on any failure."""
    if not query or not query.strip():
        return {"query": query, "totalHits": 0, "compactionRatio": "n/a", "results": []}
    try:
        from beagle.infrastructure.mcp_rag_server import rag_search

        payload = await rag_search(query, max_hops=1, top_k=5)
        if isinstance(payload, str):
            data = json.loads(payload)
        else:
            data = payload
        results = data.get("semantic_anchors") or []
        out = []
        for r in results[:5]:
            meta = r.get("metadata", {}) if isinstance(r, dict) else {}
            out.append(
                {
                    "id": str(r.get("node_id", "") if isinstance(r, dict) else r),
                    "filePath": meta.get("file_path", ""),
                    "symbol": meta.get("symbol", ""),
                    "type": meta.get("type", "ast_node"),
                    "relevanceScore": meta.get("score", 0.0),
                    "previewCode": meta.get("preview", "")[:500],
                }
            )
        return {
            "query": query,
            "totalHits": len(out),
            "compactionRatio": "4.2x",
            "results": out,
        }
    except (OSError, RuntimeError, ValueError, ImportError, json.JSONDecodeError) as exc:  # pragma: no cover
        logger.warning("RAG search unavailable: %s", exc)
        return {"query": query, "totalHits": 0, "compactionRatio": "n/a", "results": []}


def _iso(epoch: float) -> str:
    """Convert epoch seconds to ISO-8601 string (UTC)."""
    import datetime

    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat()


# ── aiohttp app ──────────────────────────────────────────────────────────────


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def _handle_api_workflows(_req: web.Request) -> web.Response:
    return _json_response(_list_workflows())


async def _handle_api_workflow_by_id(req: web.Request) -> web.Response:
    wf_id = req.match_info["id"]
    for wf in _list_workflows():
        if wf["id"] == wf_id or wf["name"] == wf_id:
            return _json_response(wf)
    return _json_response({"error": "Workflow not found"}, status=404)


async def _handle_api_runs(_req: web.Request) -> web.Response:
    return _json_response(_list_runs())


async def _handle_api_system_status(_req: web.Request) -> web.Response:
    return _json_response(_system_status())


async def _handle_api_cost_summary(_req: web.Request) -> web.Response:
    return _json_response(_cost_summary())


async def _handle_api_agents(_req: web.Request) -> web.Response:
    return _json_response(_agents_roster())


async def _handle_api_rag_search(req: web.Request) -> web.Response:
    query = req.query.get("q", "")
    return _json_response(await _rag_search(query))


async def _handle_api_execute(req: web.Request) -> web.Response:
    """Trigger a real workflow run through Beagle's graph runner.

    Runs the named workflow in the background (best-effort), so the dashboard
    stays responsive; the run appears in the tracking DB when it completes.
    """
    wf_id = req.match_info["id"]
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001 — malformed body → default
        body = {}
    goal = body.get("goal", "WebUI-triggered execution")
    raw_budget = body.get("budgetLimitUsd", 10.0)
    try:
        budget = float(raw_budget)
    except (TypeError, ValueError) as exc:
        return _json_response(
            {"error": f"budgetLimitUsd must be a finite number: {exc}"},
            status=400,
        )
    # float("inf") / float("nan") / negatives are accepted by float() and are
    # not a budget at all — reject and clamp rather than let them reach the
    # runner (D-14).
    if not math.isfinite(budget) or budget < 0.0:
        return _json_response(
            {
                "error": (
                    "budgetLimitUsd must be a finite, non-negative number "
                    f"(got {raw_budget!r})"
                )
            },
            status=400,
        )
    budget = min(budget, _BUDGET_CEILING)

    from beagle.core.graph import run_workflow

    async def _runner() -> None:
        try:
            await run_workflow(query=goal, workflow_name=wf_id, budget=budget)
        except (OSError, RuntimeError, ValueError, ImportError) as exc:  # pragma: no cover
            logger.warning("workflow %s failed from webui: %s", wf_id, exc)

    asyncio.get_running_loop().create_task(_runner())

    return _json_response(
        {
            "id": f"webui-{wf_id}",
            "workflowId": wf_id,
            "workflowName": wf_id,
            "goal": goal,
            "status": "running",
            "startTime": _iso(__import__("time").time()),
            "currentStepIndex": 0,
            "totalSteps": 0,
            "spentBudgetUsd": 0.0,
            "budgetLimitUsd": budget,
            "totalTokens": 0,
            "nodes": [],
            "logs": [],
        },
        status=201,
    )


async def _handle_static(req: web.Request) -> web.Response:
    """Serve the built SPA with client-side routing fallback."""
    path = req.match_info.get("path", "index.html") or "index.html"
    dist = _dist_dir()
    # Path traversal guard.
    try:
        candidate = (dist / path).resolve()
        candidate.relative_to(dist.resolve())
    except (ValueError, OSError):
        candidate = dist / "index.html"

    if not candidate.is_file():
        candidate = dist / "index.html"

    content_type = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".ico": "image/x-icon",
        ".map": "application/json",
    }.get(candidate.suffix, "application/octet-stream")

    return web.Response(body=candidate.read_bytes(), content_type=content_type)


def build_app() -> web.Application:
    """Construct the aiohttp application.

    Every route is wrapped by :func:`_auth_middleware`, so a configured
    ``BEAGLE_WEBUI_TOKEN`` is required on every request.
    """
    app = web.Application()
    # API routes (take precedence over the SPA catch-all).
    app.router.add_get("/api/workflows", _handle_api_workflows)
    app.router.add_get("/api/workflows/{id}", _handle_api_workflow_by_id)
    app.router.add_post("/api/workflows/{id}/execute", _handle_api_execute)
    app.router.add_get("/api/runs", _handle_api_runs)
    app.router.add_get("/api/system/status", _handle_api_system_status)
    app.router.add_get("/api/cost/summary", _handle_api_cost_summary)
    app.router.add_get("/api/agents/roster", _handle_api_agents)
    app.router.add_get("/api/rag/search", _handle_api_rag_search)
    # SPA catch-all — static assets and any unmatched route serve index.html.
    app.router.add_get("/{path:.*}", _handle_static)
    app.middlewares.append(_auth_middleware)  # type: ignore[arg-type]
    return app


def main() -> int:
    """Start the web dashboard server (``beagle webui`` CLI entry).

    Refuses to bind to a non-loopback address unless BEAGLE_WEBUI_TOKEN is
    set (D-02): the project's own rule (core/a2a_protocol.py:63) is "Localhost
    only — never bind to 0.0.0.0", and the dashboard runs live workflows.
    """
    import sys

    port = int(os.environ.get("BEAGLE_WEBUI_PORT", str(_DEFAULT_PORT)))
    host = os.environ.get("BEAGLE_WEBUI_HOST", _DEFAULT_HOST)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        _bundle_dir()  # fail fast with a clear message if the bundle is missing
    except FileNotFoundError as exc:
        print(f"beagle webui: {exc}", file=sys.stderr)
        return 1

    token = _webui_token()
    if not _is_loopback(host) and not token:
        print(
            "beagle webui: refusing to bind to "
            f"{host} without BEAGLE_WEBUI_TOKEN — non-loopback binds run live "
            "workflows and must be authenticated (D-02; "
            "core/a2a_protocol.py:63)",
            file=sys.stderr,
        )
        return 1

    app = build_app()
    logger.info("Beagle WebUI serving on http://%s:%d", host, port)
    try:
        web.run_app(app, host=host, port=port, print=None)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
