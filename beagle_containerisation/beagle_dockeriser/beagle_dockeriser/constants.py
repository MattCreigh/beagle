"""Constants for beagle_dockeriser — single source of truth."""

from __future__ import annotations

from pathlib import Path
from typing import Final


def _resolve_project_version() -> str:
    """Read the package version from its declared single source of truth.

    The generated Dockerfile header states ``SSOT:
    beagle/constants.PACKAGE_VERSION``, but this module used to hardcode its
    own ``PROJECT_VERSION``, so the claim was false and the two drifted three
    ways at once: beagle/constants.py said 13.22.3, pyproject.toml said 1.0.0,
    and this said 13.15.2. Reading the real value makes the header accurate.

    Returns:
        ``beagle.constants.PACKAGE_VERSION``.

    Raises:
        RuntimeError: If beagle is not importable. Emitting a Dockerfile that
            pins a guessed wheel filename would produce an image that fails at
            ``uv pip install`` with a confusing "file not found", so failing
            here is the cheaper error.
    """
    try:
        from beagle.constants import PACKAGE_VERSION
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise RuntimeError(
            "beagle_dockeriser cannot resolve the project version: the beagle "
            "package is not importable. Install it (uv pip install -e .) before "
            "generating container artifacts."
        ) from exc
    return PACKAGE_VERSION


# ── Project Identity ──────────────────────────────────────
PROJECT_NAME: Final = "beagle"
PROJECT_VERSION: Final = _resolve_project_version()
DOCKER_IMAGE_NAME: Final = "beagle-factory"
DOCKER_IMAGE_TAG: Final = f"v{PROJECT_VERSION}"
FULL_IMAGE_REF: Final = f"{DOCKER_IMAGE_NAME}:{DOCKER_IMAGE_TAG}"

# ── Package Name (importable) ────────────────────────────
PACKAGE_NAME: Final = "beagle"

# ── Python Version ────────────────────────────────────────
PYTHON_VERSION: Final = "3.12"
PYTHON_DOCKER_IMAGE: Final = f"python:{PYTHON_VERSION}-slim"

# ── Build Tools ───────────────────────────────────────────
UV_BINARY: Final = "/home/linuxbrew/.linuxbrew/Cellar/uv/0.11.2/bin/uv"
WHEEL_DIR: Final = "dist"

# ── Ports ─────────────────────────────────────────────────
A2A_PORT: Final = 8420
A2A_BIND_ADDRESS: Final = "127.0.0.1"

# ── Container User ────────────────────────────────────────
CONTAINER_USER: Final = "beagle_user"
CONTAINER_UID: Final = 1000
CONTAINER_GID: Final = 1000
CONTAINER_HOME: Final = "/home/beagle_user"

# ── Filesystem Paths (Container) ─────────────────────────
CONTAINER_APP_DIR: Final = "/app"
CONTAINER_DATA_DIR: Final = "/app/data"
CONTAINER_RAG_DIR: Final = "/app/data/rag"
CONTAINER_CHECKPOINTS_DIR: Final = "/home/beagle_user/.cache/goose/beagle/checkpoints"
CONTAINER_SECRETS_FILE: Final = "/home/beagle_user/.config/goose/secrets.yaml"
CONTAINER_STATE_DIR: Final = "/app/state"
CONTAINER_OUTPUT_DIR: Final = "/app/output"

# ── Filesystem Paths (Host) ──────────────────────────────
HOST_DATA_DIR: Final = "./data"
HOST_RAG_DIR: Final = "./data/rag"
HOST_CHECKPOINTS_DIR: Final = "./data/checkpoints"
HOST_SECRETS_FILE: Final = "~/.config/goose/secrets.yaml"

# ── Vestigial Directories to Scan ────────────────────────
VESTIGIAL_DIRS: Final[tuple[str, ...]] = (
    "build",
    "dist",
    "*.egg-info",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    ".mypy_cache",
)
VESTIGIAL_EXCLUDE_PREFIXES: Final[tuple[str, ...]] = (str(Path("beagle") / ""),)

# ── Environment Variables (Dockerfile) ───────────────────
DOCKERFILE_ENV: Final[dict[str, str]] = {
    "BEAGLE_EXECUTION_ENV": "docker",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "WORKSPACE_ROOT": "/app",
    "BEAGLE_DATA_ROOT": "/app/data",
    "BEAGLE_KNOWLEDGE_DIR": "/app/data/rag",
    "BEAGLE_KUZU_PATH": "/app/data/rag",
}

# ── Validation Commands ──────────────────────────────────
RUFF_CHECK_TARGETS: Final[tuple[str, ...]] = ("beagle/", "tests/")
VULTURE_MIN_CONFIDENCE: Final = 80
PYTEST_TESTPATH: Final = "tests/"
PYTEST_XDIST_FLAG: Final = "-n auto"

# ── Entrypoint ────────────────────────────────────────────
ENTRYPOINT_CMD: Final[list[str]] = ["beagle"]

# ── Stopsignal ────────────────────────────────────────────
STOP_SIGNAL: Final = "SIGTERM"

# ── Health Check ─────────────────────────────────────────
HEALTHCHECK_INTERVAL: Final = 30
HEALTHCHECK_TIMEOUT: Final = 10
HEALTHCHECK_RETRIES: Final = 3

# ── Subprocess Timeouts ──────────────────────────────────
VALIDATOR_TIMEOUT: Final = 600
BUILD_TIMEOUT: Final = 120
DOCKER_BUILD_TIMEOUT: Final = 600
