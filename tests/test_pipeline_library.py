"""The preset pipelines are well-formed.

They are plain files the platform reads by path, so nothing else checks their shape: a pipeline
without a description never reaches the PI's choice, and a pipeline that lists a tool which no
longer exists plans a step nothing can run. (The skills' shape is checked by the registry,
lijinbio/aiscientist-skills, before it publishes a release.)"""
from __future__ import annotations

from pathlib import Path

from aiscientist.agents.preset_pipelines import _pipelines_dir
from aiscientist.agents.skills import _parse_front_matter
from aiscientist.tools import catalog

# Read from where the platform reads them ($AISCIENTIST_PIPELINES_DIR or preset_pipelines/).
PIPELINES_DIR = _pipelines_dir()
PLATFORM_TOOLS = {"finish", "run_qc", "run_de_markers", "run_code", "describe_environment",
                  "search_skills", "read_skill_reference", "read_tool_source",
                  "list_dir", "stat_path", "find_files", "read_text", "disk_usage", "run_shell",
                  "fetch_url", "install_package"}


def _folders(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


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
