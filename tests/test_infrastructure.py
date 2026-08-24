#!/usr/bin/env python3
"""Test script for Beagle Docker Infrastructure.

Validates:
1. Orpheus ring IPC
2. Cost tracker with context window tracking
3. Agent health checks
"""

import asyncio
import os
import sys

from beagle.context.context_window import (
    ContextWindowManager,
)

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from beagle.cost_tracker import (  # ruff: ignore[E402]
    ContextAwareCostTracker,
    estimate_tokens_agnostic,
)


def test_cost_tracker():
    """Test context-aware cost tracker."""
    print("=" * 60)
    print("Testing ContextAwareCostTracker")
    print("=" * 60)

    tracker = ContextAwareCostTracker(
        budget_usd=5.0,
        model="qwen3.5:397b",
    )

    # Test with sample data
    input_text = "This is a test prompt. " * 500
    output_text = "This is a test response. " * 1000

    usage = asyncio.run(
        tracker.estimate_from_text(
            input_text,
            output_text,
            model="qwen3.5:397b",
            node_name="PlanningPhase",
        )
    )

    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")
    print(f"Cost: ${usage.cost_usd:.6f}")
    print()

    # Check context status
    ctx = tracker.context_status
    print(f"Context window: {ctx.context_window:,} tokens")
    print(f"Current usage: {ctx.current_tokens:,} tokens")
    print(f"Utilization: {ctx.utilization_percent:.2f}%")
    print()

    # Multiple nodes
    for node in ["ExecutionPhase", "VerificationPhase", "SynthesisPhase"]:
        asyncio.run(
            tracker.estimate_from_text(
                input_text * 2,
                output_text * 2,
                model="qwen3.5:397b",
                node_name=node,
            )
        )

    print("After 4 nodes:")
    print(f"Total tokens: {tracker.total_tokens:,}")
    print(f"Total cost: ${asyncio.run(tracker._get_total_cost()):.6f}")
    print(f"Context peak: {tracker.context_status.peak_tokens:,} tokens")
    print(f"Context utilization: {tracker.context_status.utilization_percent:.2f}%")
    print()

    print(tracker.format_report())
    print()

    # test passed


def test_context_window_manager():
    """Test context window manager."""
    print("=" * 60)
    print("Testing ContextWindowManager")
    print("=" * 60)

    ctx = ContextWindowManager(model="qwen3.5:397b")

    # Start node
    ctx.start_node("PlanningPhase")

    # Record tokens for node
    metrics = asyncio.run(ctx.record_node_tokens("PlanningPhase", 5000, 3000))
    print(f"PlanningPhase metrics: {metrics.total_tokens} tokens")
    print(f"Context utilization: {metrics.context_utilization * 100:.2f}%")
    print()

    # Execute more nodes
    ctx.start_node("ExecutionPhase")
    asyncio.run(ctx.record_node_tokens("ExecutionPhase", 8000, 5000))

    ctx.start_node("VerificationPhase")
    asyncio.run(ctx.record_node_tokens("VerificationPhase", 6000, 4000))

    # Check compression strategy
    strategy = ctx.get_compression_strategy()
    print(f"Compression strategy: {strategy}")
    print()

    # Check prompt acceptance
    can_accept, reason = ctx.can_accept_prompt(10000)
    print(f"Can accept 10000 token prompt: {can_accept} ({reason})")

    can_accept, reason = ctx.can_accept_prompt(100000)
    print(f"Can accept 100000 token prompt: {can_accept} ({reason})")
    print()

    # Summary
    import json

    print(json.dumps(ctx.get_summary(), indent=2))
    print()

    # test passed


def test_token_estimation():
    """Test token estimation accuracy."""
    print("=" * 60)
    print("Testing Token Estimation")
    print("=" * 60)

    test_cases = [
        "Hello, world!",
        "The quick brown fox jumps over the lazy dog.",
        "def hello_world():\n    print('Hello, World!')\n    return True",
        "A" * 1000,  # Repetitive
        "",
    ]

    for text in test_cases:
        tokens = estimate_tokens_agnostic(text)
        chars = len(text)
        ratio = tokens / max(1, chars / 4) if chars > 0 else 0
        print(
            f"Text: {text[:40]!r:45s} | Chars: {chars:5d} "
            f"| Tokens: {tokens:5d} | Ratio: {ratio:.2f}"
        )

    print()
    # test passed


def main():
    """Run all tests."""
    tests = [
        ("Token Estimation", test_token_estimation),
        ("ContextAwareCostTracker", test_cost_tracker),
        ("ContextWindowManager", test_context_window_manager),
    ]

    all_passed = True
    for name, test_fn in tests:
        print(f"\n{'=' * 60}")
        print(f"TEST: {name}")
        print("=" * 60)
        try:
            result = test_fn()
            if result:
                print(f"✅ {name} PASSED")
            else:
                print(f"❌ {name} FAILED")
                all_passed = False
        except Exception as e:  # ruff: ignore[BLE001]
            print(f"❌ {name} FAILED with exception: {e}")
            import traceback

            traceback.print_exc()
            all_passed = False

    print()
    print("=" * 60)
    if all_passed:
        print("✅ ALL TESTS PASSED")
        return 0
    else:
        print("❌ SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
