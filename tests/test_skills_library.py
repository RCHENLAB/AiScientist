"""The skills and preset pipelines are well-formed (they become AiScientist-skills).

They are plain files the platform reads by path, so nothing else checks their shape: a SKILL.md
without a description never reaches the Scientist's manifest, a CodeAct skill without its Run
section leaves the model guessing how to execute it, and a pipeline that lists a tool which no
longer exists plans a step nothing can run."""
from __future__ import annotations

import re
from pathlib import Path

from aiscientist.agents.preset_pipelines import _pipelines_dir
from aiscientist.agents.skills import _load_from, _parse_front_matter, _skill_md_files, _skills_dir
from aiscientist.tools import catalog

# Read from where the platform reads them: the repo-root folders today, the AiScientist-skills
# checkout ($AISCIENTIST_SKILLS_DIR / $AISCIENTIST_PIPELINES_DIR) after the split.
SKILLS_DIR = _skills_dir()
PIPELINES_DIR = _pipelines_dir()
PLATFORM_TOOLS = {"finish", "run_qc", "run_de_markers", "run_code", "describe_environment",
                  "search_skills", "read_skill_reference", "read_tool_source",
                  "list_dir", "stat_path", "find_files", "read_text", "disk_usage", "run_shell",
                  "fetch_url", "install_package"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _folders(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


def _skill_folders(root: Path) -> list[Path]:
    """Every folder holding a SKILL.md, at any depth (``skills/<name>/`` or ``skills/<category>/<name>/``)."""
    return [p.parent for p in _skill_md_files(root)]


def test_every_skill_has_a_manifest_line_and_a_name_that_matches_its_folder():
    for folder in _skill_folders(SKILLS_DIR):
        meta, body = _parse_front_matter((folder / "SKILL.md").read_text(encoding="utf-8"))
        assert _NAME.match(folder.name), f"{folder.name}: use lowercase, digits, _ or -"
        assert meta.get("name", folder.name) == folder.name, f"{folder.name}: name differs from the folder"
        assert str(meta.get("description", "")).strip(), f"{folder.name}: no description (the manifest line)"
        assert body.strip(), f"{folder.name}: empty SKILL.md body"


def test_codeact_skills_say_when_to_use_them_and_how_to_run_them():
    for folder in _skill_folders(SKILLS_DIR):
        if not (folder / "reference.py").is_file():
            continue                      # SKILL.md-only: guidance, possibly with the code inline
        body = (folder / "SKILL.md").read_text(encoding="utf-8")
        for section in ("## When to use", "## Run"):
            assert section in body, f"{folder.name}: SKILL.md lacks '{section}'"


def test_the_loader_sees_every_skill_folder():
    loaded = _load_from(SKILLS_DIR)
    assert {f.name for f in _skill_folders(SKILLS_DIR)} <= set(loaded)


def test_no_two_skill_folders_share_a_name():
    # The loader keeps the first of two same-named folders; in the repo that is always a mistake.
    names = [f.name for f in _skill_folders(SKILLS_DIR)]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)


def test_every_pipeline_has_its_protocol_and_names_only_real_tools():
    known = set(catalog.names()) | PLATFORM_TOOLS
    for folder in _folders(PIPELINES_DIR):
        assert (folder / "SKILL.md").is_file() and (folder / "PROTOCOL.md").is_file(), folder.name
        meta, _body = _parse_front_matter((folder / "SKILL.md").read_text(encoding="utf-8"))
        assert meta.get("name", folder.name) == folder.name, f"{folder.name}: name differs from the folder"
        assert meta.get("description", "").strip(), f"{folder.name}: no description"
        tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
        unknown = [t for t in tools if t not in known]
        assert not unknown, f"{folder.name} lists tools that do not exist: {unknown}"
