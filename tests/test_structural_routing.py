"""Tests for structural/analogical routing (D3)."""

from __future__ import annotations

import asyncio
import time

from beagle.core.skill_library import SkillLibrary, SkillMetadata, SkillRouter


def _skill(name: str, desc: str, tags: list[str], triggers: list[str]) -> SkillMetadata:
    return SkillMetadata(
        name=name,
        description=desc,
        tags=tags,
        trigger_conditions=triggers,
        use_count=10,
        success_count=8,
        failure_count=2,
        updated_at=time.time(),
        created_at=time.time(),
    )


def _router_with_skills() -> SkillRouter:
    lib = SkillLibrary()
    lib._index["audit"] = _skill(
        "audit", "audit the codebase", ["audit", "security"], ["audit the codebase"]
    )
    lib._index["research"] = _skill(
        "research", "research a topic", ["research"], ["research a topic"]
    )
    return SkillRouter(lib)


def test_structural_match_boosts_overlap() -> None:
    """A shared structural token boosts the score beyond lexical alone."""
    router = _router_with_skills()
    meta = router.library._index["audit"]
    boost = router._structural_match("audit the codebase", meta)
    assert boost > 0.0


def test_structural_match_no_overlap_is_zero() -> None:
    """No shared structural token yields no boost."""
    router = _router_with_skills()
    meta = router.library._index["audit"]
    boost = router._structural_match("quantum physics", meta)
    assert boost == 0.0


def test_route_returns_skill_and_confidence() -> None:
    """route returns the best skill, a confidence, and a reason."""
    router = _router_with_skills()
    name, conf, reason = asyncio.run(router.route({"query": "audit the codebase"}))
    assert name == "audit"
    assert 0.0 <= conf <= 1.0
    assert "audit" in reason


def test_route_falls_back_to_generic_on_low_confidence() -> None:
    """A query with no matching skill falls back to the generic path."""
    router = _router_with_skills()
    name, conf, reason = asyncio.run(router.route({"query": "zzz no match here"}))
    assert name == "generic"
    assert conf < 0.6
    assert "below" in reason or "no skill" in reason


def test_route_respects_threshold() -> None:
    """A high threshold forces the generic fallback even with a match."""
    router = _router_with_skills()
    name, _conf, reason = asyncio.run(
        router.route({"query": "audit the codebase"}, confidence_threshold=0.99)
    )
    assert name == "generic"
    assert "below" in reason
    assert _conf < 0.99
