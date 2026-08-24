"""Steering system for Beagle mid-workflow guidance.

Supports multiple input sources with priority ordering:
1. API (webhook/callback)
2. TUI channel
3. File (.beagle/steer.md)
4. Environment variables

Usage:
    from beagle.steering import SteeringManager, inject_steering

    # Check for steering between nodes
    directive = steering_manager.check()

    # Inject into prompt
    new_prompt = inject_steering(original_prompt, directive)
"""

from .injection import inject_steering
from .manager import SteeringManager
from .sources import (
    APISource,
    EnvSteeringSource,
    FileSteeringSource,
    SteeringSource,
    SteeringSourceManager,
    TUIChannelSource,
)
from .types import SteeringDirective

__all__ = [
    "APISource",
    "EnvSteeringSource",
    "FileSteeringSource",
    # Core
    "SteeringDirective",
    "SteeringManager",
    # Sources
    "SteeringSource",
    "SteeringSourceManager",
    "TUIChannelSource",
    "inject_steering",
]
