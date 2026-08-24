"""v13.15.1 P1.A: every goose subprocess invocation must include the
CRITICAL OUTPUT CONTRACT directive in the system prompt. Without this, models
intermittently omit </final_answer> and downstream parsing fails.

v13.22.1 (B-7, audit): the previous target (``core.orchestrator.node_executor``)
was a 700+ line dead-code file that was never imported by the live system.
The test now targets the actual live prompt builder
(``utils.subprocess.output_handlers``) where the directive is enforced.
"""

from __future__ import annotations

from beagle.utils.subprocess import output_handlers


def test_critical_output_contract_in_source():
    """The directive must appear in the live prompt builder module.

    We don't unit-test the full prompt assembly because it depends on workflow
    state; instead we assert the directive literal is present in the source
    that builds prompts. This guards against accidental removal.
    """
    import inspect

    src = inspect.getsource(output_handlers)
    assert "CRITICAL OUTPUT CONTRACT" in src, (
        "Prompt closure directive missing — see ai/goose_armslength_v13.15.1*.md"
    )
    assert "</final_answer>" in src
