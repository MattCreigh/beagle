"""Data models for beagle_dockeriser — typed generation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WheelSpec:
    """Metadata about the built wheel file."""

    path: Path
    name: str
    size_bytes: int
    version: str

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


@dataclass(frozen=True)
class DockerfileSpec:
    """Complete specification for Dockerfile generation.

    Every field has a sane default from constants.py.
    """

    base_image: str
    container_user: str
    container_uid: int
    container_gid: int
    container_home: str
    app_dir: str
    data_dir: str
    wheel_filename: str = ""
    env_vars: dict[str, str] = field(default_factory=dict)
    expose_ports: list[int] = field(default_factory=list)
    stop_signal: str = "SIGTERM"
    entrypoint: list[str] = field(default_factory=list)
    healthcheck_interval: int = 30
    healthcheck_timeout: int = 10
    healthcheck_retries: int = 3
    project_name: str = "beagle"
    version: str = ""


@dataclass(frozen=True)
class VolumeSpec:
    """A single volume mount specification."""

    host_path: str
    container_path: str
    read_only: bool = False

    def to_compose_line(self) -> str:
        rw = ":ro" if self.read_only else ""
        return f"      - {self.host_path}:{self.container_path}{rw}"


@dataclass(frozen=True)
class ComposeSpec:
    """Complete specification for docker-compose.yaml generation."""

    image_name: str = "beagle-factory"
    image_tag: str = "latest"
    container_name: str = "beagle-factory"
    restart_policy: str = "unless-stopped"
    volumes: list[VolumeSpec] = field(default_factory=list)
    ports: list[str] = field(default_factory=list)
    env_file: str = ".env"


@dataclass
class PipelineState:
    """Mutable state passed between pipeline phases."""

    project_root: Path
    phase1_passed: bool = False
    phase2_passed: bool = False
    phase3_passed: bool = False
    phase4_passed: bool = False
    phase5_passed: bool = False

    wheel_spec: WheelSpec | None = None
    dockerfile_path: Path | None = None
    dockerfile_spec: DockerfileSpec | None = None
    compose_path: Path | None = None
    compose_spec: ComposeSpec | None = None

    image_id: str = ""
    image_size_mb: float = 0.0
    build_duration_seconds: float = 0.0

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    phase_details: dict[str, str] = field(default_factory=dict)
