"""Subprocess execution sub-package — decomposed from utils/subprocess_pool.py.

Only the modules that the monolith (utils/subprocess_pool.py) and the test
suite actually import are kept here. The decomposed execution/pool_stats/
security_translation modules were dead copies that had diverged from the
monolith; they were removed in the SP-8 duplicate-subsystem remediation.
"""

__all__: list[str] = []
