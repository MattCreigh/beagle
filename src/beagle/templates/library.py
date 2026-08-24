"""Versioned, composable normative template library (D2).

A :class:`NormativeTemplate` is a named, versioned specification fragment
with three parts: constraints, success criteria, and verification patterns.
A workflow references a template by name instead of inlining the
specification, so the library is the single source of truth for the
normative requirements. Each template validates against a schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TemplateSchema:
    """Schema describing a valid normative template.

    Attributes:
        name: The template name (must be non-empty).
        version: The template version (must be non-empty).
        constraints: List of constraint strings.
        success_criteria: List of success-criterion strings.
        verification_patterns: List of verification-pattern strings.

    """

    name: str
    version: str
    constraints: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    verification_patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NormativeTemplate:
    """A versioned normative specification fragment.

    Attributes:
        name: Stable identifier (e.g. ``research``, ``security``).
        version: Semantic version of the template.
        constraints: Hard constraints the workflow must honour.
        success_criteria: Criteria that gate completion.
        verification_patterns: Patterns for proving a criterion is met.

    """

    name: str
    version: str
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    verification_patterns: tuple[str, ...] = ()

    def to_schema(self) -> TemplateSchema:
        """Return the schema view of this template.

        Returns:
            A :class:`TemplateSchema` with the same fields.

        """
        return TemplateSchema(
            name=self.name,
            version=self.version,
            constraints=list(self.constraints),
            success_criteria=list(self.success_criteria),
            verification_patterns=list(self.verification_patterns),
        )


def validate_template(template: NormativeTemplate) -> list[str]:
    """Validate a template against the schema.

    Args:
        template: The template to validate.

    Returns:
        A list of validation errors (empty when valid).

    """
    errors: list[str] = []
    if not template.name:
        errors.append("template name must be non-empty")
    if not template.version:
        errors.append("template version must be non-empty")
    if not template.constraints:
        errors.append("template must declare at least one constraint")
    if not template.success_criteria:
        errors.append("template must declare at least one success criterion")
    return errors


class TemplateLibrary:
    """A versioned, composable library of normative templates.

    Attributes:
        templates: Mapping of template name to :class:`NormativeTemplate`.

    """

    def __init__(self, templates: dict[str, NormativeTemplate] | None = None) -> None:
        """Initialise the library.

        Args:
            templates: Initial templates keyed by name.

        """
        self.templates: dict[str, NormativeTemplate] = dict(templates or {})

    def add(self, template: NormativeTemplate) -> None:
        """Add a validated template.

        Args:
            template: The template to add.

        Raises:
            ValueError: When the template fails validation.

        """
        errors = validate_template(template)
        if errors:
            raise ValueError(f"invalid template {template.name!r}: {errors}")
        self.templates[template.name] = template

    def get(self, name: str) -> NormativeTemplate:
        """Resolve a template by name.

        Args:
            name: The template name.

        Returns:
            The template.

        Raises:
            KeyError: When the name is not in the library.

        """
        if name not in self.templates:
            raise KeyError(f"unknown template {name!r}; available: {sorted(self.templates)}")
        return self.templates[name]

    def names(self) -> list[str]:
        """List template names.

        Returns:
            The sorted list of template names.

        """
        return sorted(self.templates)

    def compose(self, names: list[str]) -> NormativeTemplate:
        """Compose several templates into one.

        Later templates' constraints/criteria/patterns are appended after
        earlier ones; the composed name is the joined names.

        Args:
            names: Template names to compose, in order.

        Returns:
            A composed :class:`NormativeTemplate`.

        Raises:
            KeyError: When any name is unknown.

        """
        constraints: list[str] = []
        criteria: list[str] = []
        patterns: list[str] = []
        for name in names:
            t = self.get(name)
            constraints.extend(t.constraints)
            criteria.extend(t.success_criteria)
            patterns.extend(t.verification_patterns)
        return NormativeTemplate(
            name="+".join(names),
            version="composed",
            constraints=tuple(constraints),
            success_criteria=tuple(criteria),
            verification_patterns=tuple(patterns),
        )


def load_library(directory: Path | None = None) -> TemplateLibrary:
    """Load a template library from a directory of TOML files.

    Each ``*.toml`` file in the directory is a template with keys ``name``,
    ``version``, ``constraints``, ``success_criteria``, and
    ``verification_patterns``.

    Args:
        directory: Directory of template TOML files. When ``None``, uses
            the bundled ``<pkg>/templates/definitions``.

    Returns:
        A populated :class:`TemplateLibrary`.

    Raises:
        ValueError: When a template file fails validation.

    """
    import tomllib

    if directory is None:
        directory = Path(__file__).resolve().parent / "definitions"
    library = TemplateLibrary()
    if not directory.is_dir():
        return library
    for path in sorted(directory.glob("*.toml")):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        template = NormativeTemplate(
            name=str(data.get("name", path.stem)),
            version=str(data.get("version", "1.0.0")),
            constraints=tuple(data.get("constraints", [])),
            success_criteria=tuple(data.get("success_criteria", [])),
            verification_patterns=tuple(data.get("verification_patterns", [])),
        )
        library.add(template)
    return library
