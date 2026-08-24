"""Tests for Beagle Prompt Caching (Phase 8.1)."""

from beagle.context.prompt_cache import PromptCache


def test_static_content_caching():
    """Test that set_static caches content and build_prompt returns it."""
    cache = PromptCache()
    cache.set_static("node1", "recipe1", "directive1")

    prompt, meta = cache.build_prompt("node1", "intent1")

    assert "recipe1" in prompt
    assert "directive1" in prompt
    assert "intent1" in prompt
    assert meta.cache_hit is True


def test_cache_hit_flag():
    """Test that cache_hit is False on first call and True on second."""
    cache = PromptCache()

    # First build (no set_static yet)
    _prompt1, meta1 = cache.build_prompt("node1", "intent1")
    assert meta1.cache_hit is False

    # Set static
    cache.set_static("node1", "recipe1", "directive1")

    # Second build
    _prompt2, meta2 = cache.build_prompt("node1", "intent1")
    assert meta2.cache_hit is True


def test_dynamic_content_changes():
    """Test that same static but different dynamic results in different prompts."""
    cache = PromptCache()
    cache.set_static("node1", "recipe1", "directive1")

    prompt1, meta1 = cache.build_prompt("node1", "intent1", steering="s1")
    prompt2, meta2 = cache.build_prompt("node1", "intent1", steering="s2")

    assert prompt1 != prompt2
    assert "s1" in prompt1
    assert "s2" in prompt2
    assert meta1.static_tokens == meta2.static_tokens


def test_static_content_invalidation():
    """Test that changing recipe updates the cache entry."""
    cache = PromptCache()
    cache.set_static("node1", "recipe1", "directive1")
    prompt1, _meta1 = cache.build_prompt("node1", "intent1")

    # Change recipe
    cache.set_static("node1", "recipe2", "directive1")
    prompt2, _meta2 = cache.build_prompt("node1", "intent1")

    assert "recipe1" in prompt1
    assert "recipe2" in prompt2
    assert prompt1 != prompt2


def test_token_counting_accuracy():
    """Verify that static_tokens + dynamic_tokens = total_tokens."""
    cache = PromptCache()
    cache.set_static("node1", "recipe1", "directive1")
    _prompt, meta = cache.build_prompt("node1", "intent1", steering="s1", constraints="c1")

    assert meta.static_tokens + meta.dynamic_tokens == meta.total_tokens
    assert meta.static_tokens > 0
    assert meta.dynamic_tokens > 0


def test_boundary_markers_present():
    """Test that boundary markers are present in the output."""
    cache = PromptCache()
    cache.set_static("node1", "recipe1", "directive1")
    prompt, _ = cache.build_prompt("node1", "intent1")

    assert "STATIC BOUNDARY START" in prompt
    assert "STATIC BOUNDARY END" in prompt
    assert "DYNAMIC BOUNDARY START" in prompt
    assert "DYNAMIC BOUNDARY END" in prompt


def test_empty_steering_constraints():
    """Test that empty steering/constraints produce clean output without empty tags."""
    cache = PromptCache()
    cache.set_static("node1", "r", "d")
    prompt, _ = cache.build_prompt("node1", "i", steering="", constraints="")

    assert "<steering>" not in prompt
    assert "<constraints>" not in prompt
    assert "<intent>i</intent>" in prompt


def test_memory_pointers_injection():
    """Test that memory pointers are correctly injected."""
    cache = PromptCache()
    cache.set_static("node1", "r", "d")
    prompt, _ = cache.build_prompt("node1", "i", memory_pointers="pointers123")

    assert "<memory>" in prompt
    assert "pointers123" in prompt
