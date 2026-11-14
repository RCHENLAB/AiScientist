"""Preset parameter tables agree with the actual pinned native tools/list schemas.

This platform contract runs without Scanpy. The scientific resolver/default regression
lives with the algorithms in the independent scrna-analysis package.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from aiscientist.agents.mcp_bridge import DEFAULT_CONFIG, load_servers
from aiscientist.agents.preset_pipelines import _pipelines_dir, _parse_skill

_TOOL_HEADING_RE = re.compile(r"^\*\*`([a-z_]+)`\*\*\s*$")
_ROW_RE = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$")
NATIVE = {s.planner_name(t["name"]): t["inputSchema"]["properties"]
          for s in load_servers(DEFAULT_CONFIG) for t in s.tools}
# Transport/admission and optional override fields are documented by the tool schema,
# not as scientific defaults in a preset's parameter table.
CONTROL_FIELDS = {"analysis_id", "dataset", "dataset_paths", "rerun", "input", "samples"}

def _documented_params(body: str) -> dict[str, dict[str, str]]:
    """{tool: {param: default-as-json-text}} parsed out of a SKILL.md ``## Parameters`` section."""
    section = body.split("## Parameters", 1)
    if len(section) < 2:
        return {}
    out: dict[str, dict[str, str]] = {}
    tool = None
    for line in section[1].splitlines():
        if line.startswith("## "):
            break
        heading = _TOOL_HEADING_RE.match(line.strip())
        if heading:
            tool = heading.group(1)
            out.setdefault(tool, {})
            continue
        row = _ROW_RE.match(line.strip())
        if row and tool and row.group(1) != "parameter":
            out[tool][row.group(1)] = row.group(2)
    return out

def _skills_with_parameters() -> list[tuple[str, str]]:
    found = []
    for skill_md in sorted(_pipelines_dir().glob("*/SKILL.md")):
        meta, body = _parse_skill(skill_md.read_text(encoding="utf-8"))
        if "## Parameters" in body:
            found.append((meta.get("name") or skill_md.parent.name, body))
    return found

def test_at_least_one_protocol_documents_its_parameters():
    """Guards the guard: a parsing change that silently found nothing would make every check
    below vacuously pass."""
    assert _skills_with_parameters(), "no preset SKILL.md has a '## Parameters' section"

def _same_json_default(shown, expected):
    actual = json.loads(shown)
    return type(actual) is type(expected) and actual == expected


def test_documented_defaults_do_not_coerce_json_types():
    assert _same_json_default("false", False)
    assert _same_json_default("0", 0)
    assert _same_json_default("1.0", 1.0)
    assert not _same_json_default("false", 0)
    assert not _same_json_default("true", 1)
    assert not _same_json_default("0", False)
    assert not _same_json_default("1", 1.0)


def test_every_documented_default_matches_the_released_schema():
    for name, body in _skills_with_parameters():
        documented = _documented_params(body)
        assert documented, f"{name}: parameter section parsed no rows"
        for tool, params in documented.items():
            assert tool in NATIVE, f"{name} documents unregistered tool {tool}"
            for parameter, shown in params.items():
                prop = NATIVE[tool][parameter]
                assert "default" in prop, (name, tool, parameter)
                assert _same_json_default(shown, prop["default"]), (name, tool, parameter, shown)
                assert prop.get("description"), (tool, parameter)


def test_a_documented_tool_documents_all_published_scientific_defaults():
    for name, body in _skills_with_parameters():
        for tool, params in _documented_params(body).items():
            required = {k for k, value in NATIVE[tool].items() if "default" in value} - CONTROL_FIELDS
            assert not required - params.keys(), (name, tool, sorted(required - params.keys()))


def test_the_protocol_and_published_replication_floor_agree():
    assert NATIVE["mcp_run_pseudobulk_de"]["min_samples_per_condition"]["default"] == 2
    body = (Path(_pipelines_dir()) / "differential_expression" / "SKILL.md").read_text()
    assert ">=2 samples per arm" in body or "≥2 samples per arm" in body


def test_published_enrichment_defaults_use_the_significant_set_without_a_top_n_cap():
    props = NATIVE["mcp_run_enrichment"]
    assert props["top_n_genes"]["default"] == 0
    assert props["padj"]["default"] == .05
    assert props["lfc"]["default"] == NATIVE["mcp_run_de"]["lfc"]["default"] == .25
