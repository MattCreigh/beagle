"""Vendored ``pi`` agentic frontend.

The ``pi`` frontend is a TypeScript/Node TUI coding agent, not importable
Python. It runs under ``node`` via ``launcher.py``, which locates the shipped
bundle and execs it.

- ``vendor/pi-prebuild/``      published ``@earendil-works/pi-coding-agent``
  prebuilt bundle, bundled into the Beagle wheel (default interactive frontend)
- ``vendor/pi/``               pristine upstream source checkout (provenance/re-sync)
- ``vendor/UPSTREAM.txt``      the exact upstream ref that was vendored
- ``vendor/license-inventory.json``  generated third-party license manifest
- ``tools/``                   Beagle-side tooling for the vendored tree

``beagle`` with no subcommand launches the ``pi`` frontend. Requires Node.js
>= 20. See ``README.md`` in this directory.
"""
