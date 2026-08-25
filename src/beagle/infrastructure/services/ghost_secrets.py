"""GhostSecretsVault service — status surface over the vault's unix socket.

Thin, fail-closed adapter: this service NEVER reads secret material. It
answers reachability questions (socket presence) so other beagle components
can decide whether secret-dependent flows can start.

Dependency: the ``ghostSecretsVault`` wheel (uv-installed into the active
environment). All lookups are import-guarded so beagle boots fine when the
vault is not installed.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

DEFAULT_SOCKET = "/run/ghost-vault/docker.sock"


class GhostSecretsService:
    """Status prober for the ghostSecretsVault daemon."""

    @staticmethod
    def socket_path() -> str:
        """Vault docker-proxy socket path from vault config, or the default."""
        try:
            from ghost_vault.config import load_config  # type: ignore[import-not-found]

            return str(load_config().proxy.socket_path)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — status must never raise
            return DEFAULT_SOCKET

    @staticmethod
    def is_up() -> bool:
        """True when the vault socket exists (daemon listening)."""
        return Path(GhostSecretsService.socket_path()).exists()

    @staticmethod
    def version() -> str:
        """Installed ghostSecretsVault wheel version, or 'not installed'."""
        try:
            return version("ghostSecretsVault")
        except PackageNotFoundError:
            return "not installed"

    def status(self) -> dict[str, Any]:
        """Structured status for dashboards/health checks."""
        return {
            "service": "ghostSecretsVault",
            "version": self.version(),
            "socket": self.socket_path(),
            "up": self.is_up(),
        }
