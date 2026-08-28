"""``beagle.frontends.webui`` — Beagle web dashboard frontend.

This package ships a vendored, prebuilt React dashboard (``vendor/webui-prebuild/``)
that is served by a real Beagle-backed ``aiohttp`` server. Unlike a static mock
dashboard, the API endpoints here read live data from Beagle's own subsystems:

* ``/api/workflows`` — workflows discovered via :func:`beagle.core.workflow_loader.list_workflows`
* ``/api/runs`` — historical runs from :class:`beagle.tracking.database.TrackingDatabase`
* ``/api/rag/search`` — hybrid RAG search via Beagle's RAG pipeline
* ``/api/cost/summary`` — live cost / budget from the cost tracker
* ``/api/agents/roster`` — agent profiles from ``agents.toml``
* ``/api/workflows/:id/execute`` — actually runs a workflow through
  :func:`beagle.core.graph.run_workflow`

Use the ``beagle webui`` CLI command to start the server. See ``README.md``.
"""
