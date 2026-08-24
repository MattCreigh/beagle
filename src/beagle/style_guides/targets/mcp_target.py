"""The ``mcp_resource`` render target: return directives to a client.

This target delivers the rendered directive to an MCP client as a returned
payload instead of writing a file. It is the mechanism by which OpenClaw
(or later a `pi` front end) can pull directives over MCP. Pure and offline.
"""

from __future__ import annotations

from dataclasses import dataclass

from beagle.style_guides.targets.base import EmitOptions


@dataclass
class MCPResourceTarget:
    """Return rendered content as a payload, writing no file.

    Attributes:
        name: The CLI-facing target name, ``mcp_resource``.

    """

    name: str = "mcp_resource"

    def emit(self, content: str, options: EmitOptions) -> str:
        """Return the content as a payload marker.

        The actual content is handed back to the caller through the
        return value; this target writes nothing to disk.

        Args:
            content: The rendered directive text.
            options: Emission options.

        Returns:
            A JSON-ish status string carrying the payload length and the
            requested scope, so an MCP client can surface it.

        """
        scope = str(options.scope or "global")
        layers = ",".join(options.layers)
        return (
            f'{{"target": "mcp_resource", "scope": "{scope}", '
            f'"layers": "{layers}", "payload_chars": {len(content)}}}'
        )


mcp_resource_target = MCPResourceTarget()
