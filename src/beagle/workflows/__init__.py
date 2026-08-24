"""Workflows package (empty since S5).

The workflow YAML definitions were detached to the canonical config root
(resolved by ``find_workflows_dir()``) during S5. This package
remains as a registered namespace so ``pyproject.toml`` packaging and any
legacy ``import beagle.workflows`` reference keep resolving. It holds no
data — the canonical workflow source is the config root.
"""
