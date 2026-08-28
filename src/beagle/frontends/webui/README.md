# Vendored `webui` frontend

Beagle's web dashboard is a vendored fork of the
[`Google-Generated-Beagle-Webui`](https://github.com/MattCreigh/Google-Generated-Beagle-Webui)
React + Vite + Tailwind dashboard, **improved** by wiring every API endpoint
to the *live* Beagle engine instead of mock data.

## Layout

```text
src/beagle/frontends/webui/
├── __init__.py                 # namespace marker (no runtime Python)
├── server.py                   # real Beagle-backed aiohttp server (serves bundle + live API)
├── README.md                   # this file
├── tools/
│   └── build_prebuild.sh       # rebuild the frontend bundle (npm)
└── vendor/
    ├── UPSTREAM.txt            # exact upstream ref that was vendored
    ├── webui/                  # vendored React source (verbatim upstream)
    └── webui-prebuild/         # built `dist/` (shipped in wheel as package-data)
```

## Improvements over upstream

The upstream `Google-Generated-Beagle-Webui` serves a static React dashboard
backed entirely by **mock/simulated** data — workflows, runs, agents, RAG
search results and cost figures are hardcoded in the Express server.

This vendored fork replaces the Express mock backend with a real
Beagle-backed `aiohttp` server (`server.py`) that serves the *same* React
bundle but answers every API endpoint with live data:

| Endpoint | Data source |
|---|---|
| `GET /api/workflows` | `beagle.core.workflow_loader.list_workflows()` |
| `GET /api/workflows/{id}` | live workflow spec |
| `POST /api/workflows/{id}/execute` | `beagle.core.graph.run_workflow()` (real run) |
| `GET /api/runs` | `beagle.tracking.database.TrackingDatabase` (real run history) |
| `GET /api/system/status` | cost tracker + tracking stats |
| `GET /api/cost/summary` | `beagle.cost_tracker.get_cost_tracker()` (live spend) |
| `GET /api/agents/roster` | `beagle.config.agent_config.list_agents()` (from `agents.toml`) |
| `GET /api/rag/search` | Beagle hybrid RAG search (vector + graph) |

When a subsystem is unavailable at runtime (e.g. RAG index not yet built, or
the tracking DB absent) the endpoint degrades gracefully to a sensible
default so the dashboard never 500s.

## Bundled into the wheel

`vendor/webui-prebuild/dist/` (the built React bundle: `index.html` +
`assets/*`) ships inside the Beagle wheel as package-data, so
`beagle webui` works out of the box. `server.py` locates the bundle whether
Beagle runs from a source checkout or an installed wheel.

## Using it

```bash
beagle webui                     # serve on http://0.0.0.0:8080
beagle webui --port 9000         # custom port
BEAGLE_WEBUI_HOST=127.0.0.1 beagle webui
```

## Rebuilding the frontend bundle

The vendored source lives in `vendor/webui/`. To rebuild the shipped bundle:

```bash
cd src/beagle/frontends/webui/vendor/webui
npm install
npm run build        # esbuild bundle for the frontend only
cp -r dist/* ../webui-prebuild/dist/
```

The `server.ts` Express backend is **not** shipped — its functionality is
replaced by `server.py`. Only `vite build` (the frontend) is used; the
`esbuild server.ts` step in upstream's `package.json` is skipped.

Requires Node.js >= 20 on `PATH` at build time. At runtime, the served bundle
is static files — `beagle webui` needs only the Python wheel, no node.
