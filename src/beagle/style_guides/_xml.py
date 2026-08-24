"""Canonical XML text-escaping for the style-guide prompt-substrate emitters.

Single source of truth shared by every emitter of Top-of-Mind / style-guide
XML — ``render.py`` (the Top-of-Mind renderer), ``tom_hydrator.py`` (the
RAG/chat hydrator), and ``injector.py`` (the per-file-edit style-guide
injector). Keeping one implementation prevents the failure mode where one
emitter escapes and another does not: a guide TOML value containing ``<``,
``>``, or ``&`` would then produce malformed XML from the un-escaped path,
breaking the document the ``tom`` extension feeds to the model every turn.
"""

from __future__ import annotations


def xml_escape(text: str) -> str:
    """Escape ``&``, ``<``, ``>`` for safe XML *text content*.

    ``&`` is escaped first so the ampersands introduced when escaping
    ``<``/``>`` are not themselves double-escaped. This is text-content
    escaping (not attribute-value escaping); it intentionally does not
    touch quotes, matching the long-standing behaviour the renderer and
    hydrator already relied on.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
