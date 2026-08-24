"""Tests for the hardware profile detection (v13.21.5)."""

from __future__ import annotations

import sys

import pytest

from beagle.infrastructure.hardware_checks import (
    HardwareProfile,
    detect_hardware_profile,
    get_hardware_profile,
)


def _profile_for(monkeypatch, machine: str, platform_name: str) -> HardwareProfile:
    """Helper: build a profile with mocked platform.machine and sys.platform."""
    import platform as _platform

    monkeypatch.setattr(_platform, "machine", lambda: machine)
    monkeypatch.setattr(sys, "platform", platform_name)
    return detect_hardware_profile()


class TestDetectHardwareProfile:
    def test_returns_hardware_profile_instance(self):
        p = detect_hardware_profile()
        assert isinstance(p, HardwareProfile)

    def test_profile_attributes_consistent(self):
        p = detect_hardware_profile()
        if p.is_apple_silicon:
            assert p.arch == "arm64"
            assert p.is_macos is True
        if p.is_linux:
            assert p.is_macos is False

    def test_torch_index_url_is_set(self):
        p = detect_hardware_profile()
        assert p.torch_index_url.startswith("https://")

    def test_label_nonempty(self):
        p = detect_hardware_profile()
        assert isinstance(p.label, str)
        assert len(p.label) > 0
        assert p.machine in p.label

    def test_apple_silicon_detected(self, monkeypatch):
        p = _profile_for(monkeypatch, machine="arm64", platform_name="darwin")
        assert p.is_apple_silicon is True
        assert p.arch == "arm64"
        assert p.machine == "arm64"
        assert "Apple Silicon" in p.label

    def test_x86_64_linux_detected(self, monkeypatch):
        p = _profile_for(monkeypatch, machine="x86_64", platform_name="linux")
        assert p.is_apple_silicon is False
        assert p.arch == "x86_64"
        assert p.is_linux is True
        assert p.is_macos is False

    def test_arm64_linux_detected(self, monkeypatch):
        p = _profile_for(monkeypatch, machine="aarch64", platform_name="linux")
        assert p.arch == "arm64"
        assert p.is_apple_silicon is False  # only macOS arm64 is Apple Silicon
        assert p.is_linux is True

    def test_unknown_machine(self, monkeypatch):
        p = _profile_for(monkeypatch, machine="", platform_name="linux")
        assert p.arch == "unknown"
        assert p.machine == "unknown"


class TestGetHardwareProfile:
    def test_returns_singleton(self):
        """The cached profile is the same instance on every call."""
        p1 = get_hardware_profile()
        p2 = get_hardware_profile()
        assert p1 is p2

    def test_profile_is_frozen(self):
        p = get_hardware_profile()
        with pytest.raises((AttributeError, Exception)):
            p.arch = "x86"  # type: ignore[misc]
