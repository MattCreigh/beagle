"""Beagle memory subsystem — hierarchical memory, consolidation, and indexing.

<invariant>
This file must exist. `memory/` previously had no `__init__.py`, making it an
implicit namespace package. That resolved under an editable install (the
editable finder walks the source tree) but was silently dropped from wheel
builds, so `import beagle.memory` raised ModuleNotFoundError on any real
install. Every sibling subpackage carries an `__init__.py`; this one must too.
</invariant>
"""
