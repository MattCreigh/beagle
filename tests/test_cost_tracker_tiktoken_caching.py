"""B-2 regression test: tiktoken import-failure caching.

The previous implementation of ``estimate_tokens_agnostic`` re-attempted
``import tiktoken`` on every call when the first import failed. This caused
a 50-200ms import-failure storm under load, AND the heuristic fallback
underestimates code tokens by 30-40%, causing context-window overflow.

Reference: audit/golden_master_v13.22.0.md B-2
"""

from __future__ import annotations

import builtins
import importlib
import sys
import time

import pytest

_MODULE = "beagle.cost_tracker"


@pytest.fixture(autouse=True)
def _reset_tokenizer_cache():
    """Clear both halves of the tokenizer cache around every test in this file.

    These tests deliberately drive the cache into states the product treats as
    terminal — "tiktoken import failed", and a fake encoder that returns one
    token per character. Neither may survive the test.

    Two earlier attempts at this failed, which is why it is done on the live
    module rather than by juggling module identity:

    1. Letting ``_fresh_cost_tracker`` swap ``sys.modules`` and restoring the
       entry afterwards. The parent package ``beagle`` keeps
       its own reference, so the "fresh" module was frequently the same object
       every other test imports — confirmed by comparing ``id(fn.__globals__)``.
    2. Clearing the cache on ``sys.modules.get(...)`` in teardown. When the
       teardown had just popped the key, the lookup returned None and cleared
       nothing, while the live object kept the fake encoder.

    The observable symptom was ``estimate_tokens_agnostic("hello world " * 50)``
    returning 600 — one token per character — in
    ``tests/test_memory_management.py``, which passed in isolation.

    Yields:
        None. Both globals are nulled before and after each test.
    """
    module = importlib.import_module(_MODULE)

    def _clear() -> None:
        module._TOKENIZER_STATE = None
        module._tokenizer_cache = None

    _clear()
    yield
    _clear()


def _fresh_cost_tracker():
    """Return cost_tracker with its tokenizer caches reset.

    Deliberately does NOT delete the sys.modules entry. Re-importing produces a
    second module object while the parent package still references the first, so
    the caches these tests then poke may or may not be the ones the rest of the
    suite reads. Resetting the two globals on the single live module is both
    simpler and actually isolating; ``_reset_tokenizer_cache`` does the same
    around every test here.

    Returns:
        The cost_tracker module with empty tokenizer caches.
    """
    module = importlib.import_module(_MODULE)
    module._TOKENIZER_STATE = None
    module._tokenizer_cache = None
    return module


def test_tiktoken_caches_import_failure(monkeypatch):
    """B-2: import failure must be cached, not retried per call."""
    ct = _fresh_cost_tracker()
    # Reset the cache state
    ct._TOKENIZER_STATE = None
    ct._tokenizer_cache = None

    import_attempt_count = {"n": 0}
    real_import = builtins.__import__

    def counting_import(name, *args, **kwargs):
        if name == "tiktoken" or name.startswith("tiktoken."):
            import_attempt_count["n"] += 1
            raise ImportError("simulated missing tiktoken")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", counting_import)

    # Make sure tiktoken is not already loaded
    monkeypatch.delitem(sys.modules, "tiktoken", raising=False)

    # Call estimate_tokens_agnostic 1000 times
    for _ in range(1000):
        ct.estimate_tokens_agnostic("test text", "default")

    # The import should have been attempted at most ONCE
    assert import_attempt_count["n"] <= 1, (
        f"tiktoken import was attempted {import_attempt_count['n']} times — "
        f"B-2 NOT FIXED: import-failure storm is still happening"
    )


def test_tiktoken_state_persists_across_calls(monkeypatch):
    """After an import failure, _TOKENIZER_STATE must be False, not None."""
    ct = _fresh_cost_tracker()
    ct._TOKENIZER_STATE = None
    ct._tokenizer_cache = None

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "tiktoken" or name.startswith("tiktoken."):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    monkeypatch.delitem(sys.modules, "tiktoken", raising=False)

    # First call: attempts import, fails, sets _TOKENIZER_STATE = False
    ct.estimate_tokens_agnostic("hello", "default")
    assert ct._TOKENIZER_STATE is False, (
        f"_TOKENIZER_STATE should be False after import failure, got {ct._TOKENIZER_STATE!r}"
    )
    assert ct._tokenizer_cache is None

    # Subsequent calls: must NOT re-attempt the import
    for _ in range(100):
        ct.estimate_tokens_agnostic("more text", "default")
    assert ct._TOKENIZER_STATE is False


def test_tiktoken_caches_successful_import(monkeypatch):
    """A successful import should also be cached (not retried per call)."""
    ct = _fresh_cost_tracker()
    ct._TOKENIZER_STATE = None
    ct._tokenizer_cache = None

    # Inject a fake tiktoken module
    class FakeEncoding:
        def encode(self, s):
            # Pretend each char is a token
            return list(s)

    class FakeTiktoken:
        @staticmethod
        def get_encoding(name):
            return FakeEncoding()

    fake_module = FakeTiktoken()
    monkeypatch.setitem(sys.modules, "tiktoken", fake_module)

    # First call: imports, caches
    n1 = ct.estimate_tokens_agnostic("hello", "default")
    assert ct._TOKENIZER_STATE is True
    assert ct._tokenizer_cache is not None
    cache_after_first = ct._tokenizer_cache

    # Second call: must use the cache
    n2 = ct.estimate_tokens_agnostic("world", "default")
    assert ct._tokenizer_cache is cache_after_first
    # Each char is a token, so "hello" = 5, "world" = 5
    assert n1 == 5
    assert n2 == 5


def test_heuristic_estimate_is_fast():
    """Without tiktoken, 10000 calls should complete quickly (no re-import)."""
    ct = _fresh_cost_tracker()
    ct._TOKENIZER_STATE = False
    ct._tokenizer_cache = None

    # Use a realistic-sized code snippet
    text = (
        """
    def hello_world(name: str) -> str:
        return f"Hello, {name}!"
    """.strip()
        * 10
    )

    start = time.perf_counter()
    for _ in range(10000):
        ct.estimate_tokens_agnostic(text, "default")
    elapsed = time.perf_counter() - start

    # 10k heuristic calls must complete in <1s. Before the fix, each call
    # would have re-attempted the import (50-200ms per call), making this
    # test take 500-2000s.
    assert elapsed < 1.0, (
        f"10000 heuristic calls took {elapsed:.3f}s — tiktoken re-import is not cached"
    )


def test_estimate_handles_empty_text():
    """Empty text must return 0, regardless of model."""
    ct = _fresh_cost_tracker()
    ct._TOKENIZER_STATE = False
    ct._tokenizer_cache = None
    assert ct.estimate_tokens_agnostic("", "default") == 0
    assert ct.estimate_tokens_agnostic("", "glm-5.1:cloud") == 0


def test_estimate_handles_surrogates(monkeypatch):
    """Pathological input that breaks tiktoken.encode must fall back gracefully.

    Installs the fake through ``monkeypatch.setitem`` like its sibling tests.
    It used to assign ``sys.modules["tiktoken"]`` directly with no cleanup, so
    the fake encoder — one token per character — stayed installed for the rest
    of the session. Every later test that estimated tokens silently got a
    character count: ``tests/test_memory_management.py`` saw 600 tokens for a
    600-character string and failed, while passing in isolation.
    """
    ct = _fresh_cost_tracker()

    class FakeEncoding:
        def encode(self, s):
            # Simulate the surrogate-pair failure mode
            if "\ud800" in s:
                raise ValueError("surrogate pair error")
            return list(s)

    class FakeTiktoken:
        @staticmethod
        def get_encoding(name):
            return FakeEncoding()

    monkeypatch.setitem(sys.modules, "tiktoken", FakeTiktoken())
    ct._TOKENIZER_STATE = None
    ct._tokenizer_cache = None

    # Should not raise — falls back to heuristic
    n = ct.estimate_tokens_agnostic("text with surrogate: \ud800", "default")
    assert n > 0
