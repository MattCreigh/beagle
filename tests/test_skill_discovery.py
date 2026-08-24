"""Tests for directory-scan skill discovery (QA-1 / BGL-042).

The skill index is built by scanning the skills directory, not by reading a
sidecar ``.skill_index.json``. XML is the preferred format; Markdown is
indexed only when no XML twin exists.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from beagle.core.skill_library import SkillLibrary


def _packaged_skills_dir() -> Path:
    """Return the packaged skills directory shipped with the wheel."""
    import beagle.core.skill_library as sl

    return Path(sl.__file__).resolve().parent.parent / "skills"


def test_bundled_skills_are_indexed() -> None:
    """The packaged skills dir yields an index of 8 skills."""
    lib = SkillLibrary(_packaged_skills_dir())
    assert len(lib._index) == 8


def test_router_can_find_a_bundled_skill() -> None:
    """search_skills('search') returns the web-search skill."""
    lib = SkillLibrary(_packaged_skills_dir())
    hits = asyncio.run(lib.search_skills("search"))
    names = [m.name for m in hits]
    assert "web-search" in names


def test_unparseable_skill_is_skipped_not_fatal() -> None:
    """A malformed skill file is skipped, not fatal."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_dir = Path(tmpdir)
        (skills_dir / "good.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<skill name="good"><description>a good skill</description>'
            "<prompt_template>do it</prompt_template></skill>\n",
            encoding="utf-8",
        )
        (skills_dir / "bad.xml").write_text("not xml", encoding="utf-8")
        lib = SkillLibrary(skills_dir)
        assert len(lib._index) == 1
        assert "good" in lib._index


def test_xml_preferred_over_markdown() -> None:
    """When both <stem>.xml and <stem>.md exist, XML wins."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_dir = Path(tmpdir)
        (skills_dir / "dup.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<skill name="dup"><description>from xml</description>'
            "<prompt_template>xml</prompt_template></skill>\n",
            encoding="utf-8",
        )
        (skills_dir / "dup.md").write_text(
            "## Description\nfrom markdown\n## Prompt Template\nmd\n",
            encoding="utf-8",
        )
        lib = SkillLibrary(skills_dir)
        assert len(lib._index) == 1
        assert lib._index["dup"].description == "from xml"


def test_markdown_skill_indexed_when_no_xml_twin() -> None:
    """A Markdown skill with no XML twin is indexed for compatibility."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        skills_dir = Path(tmpdir)
        (skills_dir / "only-md.md").write_text(
            "## Description\nmarkdown only\n## Prompt Template\nmd\n",
            encoding="utf-8",
        )
        lib = SkillLibrary(skills_dir)
        assert len(lib._index) == 1
        assert "only-md" in lib._index
