"""Vendored ``pi`` agentic frontend.

This package is a namespace marker only. The actual frontend is a vendored
checkout of the ``earendil-works/pi`` TUI coding agent under ``vendor/pi/`` —
a TypeScript/Node project, not importable Python.

- ``vendor/pi/``               pristine upstream tree (only a re-sync touches it)
- ``vendor/UPSTREAM.txt``      the exact upstream ref that was vendored
- ``vendor/license-inventory.json``  generated third-party license manifest
- ``tools/``                   Beagle-side tooling for the vendored tree

The vendored tree is **repo-only**: it is not bundled into the Beagle wheel.
See ``README.md`` in this directory for the rationale and the re-sync procedure.
"""
