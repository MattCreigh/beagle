"""TOML-based style guide engine for Beagle prompt injection."""

from .injector import ContextInjector
from .loader import StyleGuideLoader

__all__ = ["ContextInjector", "StyleGuideLoader"]
