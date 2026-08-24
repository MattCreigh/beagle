"""Tests for block Jinja env rendering."""

from __future__ import annotations

from beagle.blocks.jinja_env import render_template


def test_render_simple():
    result = render_template("Hello {{ name }}!", {"name": "World"})
    assert result == "Hello World!"


def test_render_missing_var_is_empty():
    result = render_template("Hello {{ name }}!", {})
    assert result == "Hello !"


def test_render_with_filter():
    result = render_template("{{ items|to_json }}", {"items": [1, 2, 3]})
    assert "[1, 2, 3]" in result
