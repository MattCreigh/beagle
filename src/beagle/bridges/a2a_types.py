"""A2A shared dataclass types — a leaf module (stdlib only, no intra-package imports).

SP-7 (beagle-spotless-phase2): ``AgentCard`` was defined in
``bridges/a2a_server.py`` and imported by ``bridges/a2a_card_builder.py``. Because
``a2a_server`` lazily imports ``build_agent_cards`` from ``a2a_card_builder`` (its
discover endpoint) and ``a2a_card_builder`` hard-imported ``AgentCard`` from
``a2a_server``, the two formed a cycle. Extracting the shared dataclass here lets
both depend on this leaf without a cycle.

``a2a_server`` re-exports ``AgentCard`` for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentCard:
    """A2A Agent Card — describes an agent's capabilities.

    Follows the A2A specification for agent discovery.
    """

    name: str
    description: str = ""
    version: str = "1.0.0"
    capabilities: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    endpoint_url: str = ""
