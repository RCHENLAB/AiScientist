"""A skill is added by dropping its folder under ``skills/``: no code change, no restart, no required
code file, any folder depth, and the Agent-Skills frontmatter other catalogs use (a nested
``metadata:`` block, quoted values). These tests pin each of those."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiscientist.agents import skills as skills_mod
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.skills import (
    Skill,
    _load_from,
    _parse_front_matter,
    make_skill_reference_tool,
    refresh_skills,
    register_skill,
    skill_manifest,
)

# The shape of lijinbio/aiscientist-skills: category folder, kebab-case name, nested metadata with
# quoted values, and the commands inline in the body with no bundled file.
_AGENT_SKILL = """---
name: qc-snrna-with-cellqc-standalone
description: Run the CellQC pipeline over a cohort of 10x Cell Ranger libraries with plain bash and conda only.
license: MIT
compatibility: Requires bash, coreutils, tar, awk and conda or mamba; Slurm optional.
metadata:
  category: single-cell
  version: "1.0.0"
  status: core
  tested-with: "cellqc 0.3.6, Python 3.12.14"
  requires-gpu: "false"
---

# QC snRNA-seq with CellQC (standalone)

```bash
mamba create -y -n cellqc_v0.3.6 -c conda-forge -c bioconda cellqc=0.3.6 r-doubletfinder python=3.12
```
"""


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill_md(name: str, description: str = "does a thing") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\nUse it like this.\n"


def test_an_agent_skills_folder_loads_as_is():
    meta, _body = _parse_front_matter(_AGENT_SKILL)
    # The nested block is a dict, and its children did NOT leak out as top-level keys.
    assert meta["metadata"] == {"category": "single-cell", "version": "1.0.0", "status": "core",
                                "tested-with": "cellqc 0.3.6, Python 3.12.14", "requires-gpu": "false"}
    assert "version" not in meta and "category" not in meta
    assert meta["license"] == "MIT"


def test_a_nested_skill_without_a_code_file_is_loaded(tmp_path):
    _write(tmp_path, "single-cell/qc-snrna-with-cellqc-standalone/SKILL.md", _AGENT_SKILL)
    lib = _load_from(tmp_path)
    skill = lib["qc-snrna-with-cellqc-standalone"]
    assert skill.category == "single-cell"
    assert skill.summary.startswith("Run the CellQC pipeline")
    assert skill.files == {}                                     # SKILL.md alone is a skill
    assert "mamba create" in skill.doc                           # the commands live in the body
    assert skill.metadata["version"] == "1.0.0"


def test_the_category_falls_back_to_the_folder_it_sits_in(tmp_path):
    _write(tmp_path, "variant/normalize-a-vcf/SKILL.md", _skill_md("normalize-a-vcf"))
    _write(tmp_path, "top_level/SKILL.md", _skill_md("top_level"))
    lib = _load_from(tmp_path)
    assert lib["normalize-a-vcf"].category == "variant"
    assert lib["top_level"].category == ""


def test_bundled_files_include_subfolders_but_not_hidden_drafts_or_nested_skills(tmp_path):
    _write(tmp_path, "outer/SKILL.md", _skill_md("outer"))
    _write(tmp_path, "outer/reference.py", "print('ref')")
    _write(tmp_path, "outer/scripts/stage.sh", "echo stage")
    _write(tmp_path, "outer/.DS_Store", "junk")
    _write(tmp_path, "outer/_drafts/old.py", "old")
    _write(tmp_path, "outer/inner/SKILL.md", _skill_md("inner"))
    _write(tmp_path, "outer/inner/run.sh", "echo inner")
    _write(tmp_path, "_wip/hidden/SKILL.md", _skill_md("hidden"))
    _write(tmp_path, ".git/SKILL.md", _skill_md("git_litter"))
    lib = _load_from(tmp_path)
    assert set(lib["outer"].files) == {"reference.py", "scripts/stage.sh"}
    assert set(lib["inner"].files) == {"run.sh"}                  # its own skill, not outer's bundle
    assert "hidden" not in lib and "git_litter" not in lib


def test_two_folders_with_one_name_keep_the_first(tmp_path, capsys):
    _write(tmp_path, "a/dup/SKILL.md", _skill_md("dup", "first"))
    _write(tmp_path, "b/dup/SKILL.md", _skill_md("dup", "second"))
    lib = _load_from(tmp_path)
    assert lib["dup"].summary == "first"
    assert "also named 'dup'" in capsys.readouterr().out


@pytest.fixture
def isolated_library(tmp_path, monkeypatch):
    """The loader pointed at an empty temporary ``skills/``, with its own SKILLS dict and scan state,
    all restored afterwards so nothing leaks into other tests."""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("AISCIENTIST_SKILLS_DIR", str(root))
    monkeypatch.delenv("AISCIENTIST_INDUCED_SKILLS_DIR", raising=False)
    lib: dict = {}
    monkeypatch.setattr(skills_mod, "SKILLS", lib)
    monkeypatch.setattr(skills_mod, "ALL_SKILLS", {})
    monkeypatch.setattr(skills_mod, "_SIGNATURE", ())
    monkeypatch.setattr(skills_mod, "_REGISTERED", {})
    refresh_skills()
    return root, lib


def test_a_dropped_in_folder_is_seen_without_a_restart(isolated_library):
    root, lib = isolated_library
    assert lib == {}
    _write(root, "single-cell/qc-snrna-with-cellqc-standalone/SKILL.md", _AGENT_SKILL)
    assert refresh_skills() is True
    assert "qc-snrna-with-cellqc-standalone" in lib
    assert refresh_skills() is False                              # nothing changed: no re-scan

    _write(root, "single-cell/qc-snrna-with-cellqc-standalone/SKILL.md",
           _AGENT_SKILL.replace("Run the CellQC pipeline", "Run CellQC, edited,"))
    refresh_skills()
    assert lib["qc-snrna-with-cellqc-standalone"].summary.startswith("Run CellQC, edited,")

    (root / "single-cell/qc-snrna-with-cellqc-standalone/SKILL.md").unlink()
    refresh_skills()
    assert "qc-snrna-with-cellqc-standalone" not in lib


def test_an_induced_skill_survives_a_rescan_until_it_is_removed(isolated_library):
    root, lib = isolated_library
    assert register_skill(Skill("induced_one", summary="learned", files={"reference.py": "x"}))
    _write(root, "dropped/SKILL.md", _skill_md("dropped"))
    refresh_skills()
    assert {"induced_one", "dropped"} <= set(lib)
    del lib["induced_one"]
    _write(root, "another/SKILL.md", _skill_md("another"))
    refresh_skills()
    assert "induced_one" not in lib                               # a removal sticks


def test_the_manifest_lists_every_skill_grouped_by_category():
    lib = {f"s{i}": Skill(f"s{i}", summary=f"thing {i}", category="single-cell" if i % 3 == 0 else "")
           for i in range(60)}
    manifest = skill_manifest(lib)
    assert all(f"- s{i} — thing {i}" in manifest for i in range(60))
    lines = manifest.splitlines()
    assert lines.count("[single-cell]") == 1
    # The grouped ones sit under their header, after the uncategorized ones.
    header = lines.index("[single-cell]")
    assert all(lines.index(f"- s{i} — thing {i}") > header for i in range(0, 60, 3))


def test_reading_a_skill_without_files_points_at_its_inline_code():
    lib = {"inline": Skill("inline", summary="short one", doc="```bash\necho hi\n```",
                           metadata={"requires-gpu": "false"})}
    tool = make_skill_reference_tool(lambda: lib)
    out = tool.executor({"name": "inline"}, HarnessContext(decisions={}))
    assert out["files"] == [] and "echo hi" in out["doc"]
    assert "in `doc` above" in out["next"]
    assert out["metadata"] == {"requires-gpu": "false"}
