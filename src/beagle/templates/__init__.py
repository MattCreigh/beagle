"""Normative template library (D2).

A normative template captures the reusable specification fragments a
workflow needs: constraints, success criteria, and verification patterns.
This package makes them a versioned, composable library: a workflow names a
template instead of inlining a specification, and the library validates each
template against a schema.
"""

from beagle.templates.library import (
    NormativeTemplate,
    TemplateLibrary,
    TemplateSchema,
    load_library,
    validate_template,
)

__all__ = [
    "NormativeTemplate",
    "TemplateLibrary",
    "TemplateSchema",
    "load_library",
    "validate_template",
]
