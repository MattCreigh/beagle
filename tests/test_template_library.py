"""Tests for the versioned normative template library (D2)."""

from __future__ import annotations

import pytest

from beagle.templates.library import (
    NormativeTemplate,
    TemplateLibrary,
    load_library,
    validate_template,
)


def _sample() -> NormativeTemplate:
    return NormativeTemplate(
        name="research",
        version="1.0.0",
        constraints=("traceable",),
        success_criteria=("answers question",),
        verification_patterns=("grep source",),
    )


def test_validate_valid() -> None:
    assert validate_template(_sample()) == []


def test_validate_missing_name() -> None:
    t = _sample()
    t = NormativeTemplate(name="", version="1.0.0", constraints=("c",), success_criteria=("s",))
    assert "template name must be non-empty" in validate_template(t)


def test_validate_missing_constraints() -> None:
    t = NormativeTemplate(name="x", version="1.0.0", success_criteria=("s",))
    assert "template must declare at least one constraint" in validate_template(t)


def test_library_add_and_get() -> None:
    lib = TemplateLibrary()
    lib.add(_sample())
    assert lib.get("research").name == "research"
    assert lib.names() == ["research"]


def test_library_add_invalid_raises() -> None:
    lib = TemplateLibrary()
    with pytest.raises(ValueError):
        lib.add(NormativeTemplate(name="", version="1.0.0"))


def test_library_get_unknown_raises() -> None:
    lib = TemplateLibrary()
    with pytest.raises(KeyError):
        lib.get("nope")


def test_compose() -> None:
    lib = TemplateLibrary()
    lib.add(_sample())
    lib.add(
        NormativeTemplate(
            name="security",
            version="1.0.0",
            constraints=("no weaken",),
            success_criteria=("validated",),
        )
    )
    composed = lib.compose(["research", "security"])
    assert composed.name == "research+security"
    assert composed.constraints == ("traceable", "no weaken")


def test_load_library_bundled() -> None:
    lib = load_library()
    assert "research" in lib.names()
    assert "security" in lib.names()
