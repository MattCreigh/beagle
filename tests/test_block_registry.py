"""Tests for BlockRegistry discovery and lookup."""

from __future__ import annotations

import pytest

from beagle.blocks.errors import BlockNotFoundError
from beagle.blocks.registry import BlockRegistry


@pytest.fixture
def registry():
    reg = BlockRegistry()
    reg._python.clear()
    reg._xml.clear()
    return reg


def test_register_python(registry):
    def fake_block(ctx, **kwargs):
        return 42

    fake_block.__block_name__ = "fake"

    registry.register_python("fake", fake_block)
    assert registry.has_block("fake")
    assert registry.list_python() == ["fake"]


def test_get_python_not_found(registry):
    with pytest.raises(BlockNotFoundError):
        registry.get_python("missing")


def test_register_xml(registry, tmp_path):
    path = tmp_path / "test.xml"
    path.write_text("<block name='test'></block>")
    registry.register_xml("test", path)
    assert registry.has_block("test")


def test_list_xml_empty(registry):
    assert registry.list_xml() == []


def test_discover_xml(registry, tmp_path):
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    (stdlib / "plan.xml").write_text("<block name='plan'></block>")
    (stdlib / "verify.xml").write_text("<block name='verify'></block>")
    count = registry.discover_xml(stdlib)
    assert count == 2
    assert "plan" in registry.list_xml()
    assert "verify" in registry.list_xml()


def test_instance_singleton():
    r1 = BlockRegistry.instance()
    r2 = BlockRegistry.instance()
    assert r1 is r2
