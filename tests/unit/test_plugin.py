"""The Claude Code plugin this repository publishes, and its marketplace.

Claude Code's own validator, which CI runs, checks the manifests' shape. These
check what it cannot know: that the plugin is the one this package documents,
at the same version, and that the skill's pieces are all reachable.
"""

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import pg_debug

REPOSITORY = Path(__file__).resolve().parents[2]
MARKETPLACE = REPOSITORY / ".claude-plugin" / "marketplace.json"
PLUGIN = REPOSITORY / "plugins" / "pg-debug"
SKILL = PLUGIN / "skills" / "pg-debug"

#: Claude Code truncates a skill's description beyond this in its listing.
DESCRIPTION_LIMIT = 1536

#: Claude Code's guidance for SKILL.md; detail belongs in references/.
SKILL_LINE_LIMIT = 500


def _manifest() -> dict[str, Any]:
    return json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


def _skill_frontmatter() -> dict[str, str]:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md has no frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def test_the_marketplace_lists_the_plugin_in_this_repository():
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))

    [entry] = marketplace["plugins"]
    assert entry["name"] == _manifest()["name"] == "pg-debug"
    assert (REPOSITORY / entry["source"]).resolve() == PLUGIN
    # One place for the version: Claude Code reads the manifest's first, and
    # its documentation says not to set both.
    assert "version" not in entry


def test_the_plugin_is_at_the_package_version():
    """Setting a version pins installed copies until it changes, so it moves
    with each release; a release that forgets it fails here."""
    assert _manifest()["version"] == pg_debug.__version__


def test_the_plugin_carries_the_package_license():
    pyproject = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))
    assert _manifest()["license"] == pyproject["project"]["license"]


def test_the_skill_is_named_for_its_directory():
    assert _skill_frontmatter()["name"] == SKILL.name


def test_the_skill_description_fits_the_listing():
    description = _skill_frontmatter()["description"]
    assert 0 < len(description) <= DESCRIPTION_LIMIT


def test_skill_md_stays_short_enough_to_load_whole():
    lines = (SKILL / "SKILL.md").read_text(encoding="utf-8").count("\n")
    assert lines <= SKILL_LINE_LIMIT, f"SKILL.md has {lines} lines; move detail into references/"


def test_every_reference_is_linked_from_the_skill_and_every_link_resolves():
    """A reference nothing links to is never read; a link to nothing misleads."""
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\((references/[^)]+\.md)\)", text))
    present = {f"references/{path.name}" for path in (SKILL / "references").glob("*.md")}

    assert linked == present


def test_the_project_skill_is_the_plugin_skill():
    """``.claude/skills/pg-debug`` links to the plugin's skill so a session in
    this repository has it without installing anything. A Windows checkout
    without symlink support writes the link as a file holding its target."""
    link = REPOSITORY / ".claude" / "skills" / "pg-debug"
    target = os.readlink(link) if link.is_symlink() else link.read_text(encoding="utf-8")

    assert Path(target).as_posix() == "../../plugins/pg-debug/skills/pg-debug"
