#!/usr/bin/env python3
"""Cut this repository into AiScientist, AiScientist-tools and AiScientist-skills, history kept.

Safe to rehearse: everything happens in fresh clones under ``--out``; this checkout and every remote
are left alone, and nothing is pushed.

    python scripts/refactor/extract_repos.py --out ../split-rehearsal [--branch NAME]

Produces three repositories under ``--out``:

* ``AiScientist-skills``: ``skills/`` and ``pipelines/`` (renamed from ``preset_pipelines/``), with
  their history, plus a README commit.
* ``AiScientist-tools``: ``src/aiscientist/tools/``, the tests that import only ``aiscientist.tools``,
  ``tests/fixtures/`` and ``scripts/tool_docs.py``, with their history, plus a commit adding
  ``pyproject.toml`` (distribution ``aiscientist-tools``, import path still ``aiscientist.tools``) and
  a README.
* ``AiScientist``: everything else. One commit removes what moved and ``src/aiscientist/__init__.py``,
  so ``aiscientist`` becomes a namespace package that the platform and AiScientist-tools both provide.

It then prints the commands that test each repository the way the others will consume it.
Needs ``git filter-repo`` on PATH.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SKILLS_README = """# AiScientist-skills

What the AiScientist agent reads and adapts: plain files, no Python package.

```
skills/<name>/      SKILL.md (name, one-line description, When to use / Details / Run) + reference.py
pipelines/<name>/   SKILL.md (name, description, tools, data_type) + PROTOCOL.md (+ references/)
```

* A **skill** is an atomic CodeAct template. The Scientist sees only its name and description, reads
  SKILL.md with `read_skill_reference`, then the template, adapts it and runs it with `run_code`.
* A **pipeline** is an end-to-end protocol the PI picks when planning; its `tools:` must name tools
  that exist in AiScientist-tools.

The platform (RCHENLAB/AiScientist) reads this repository from a checkout of a pinned tag:
`AISCIENTIST_SKILLS_DIR=<checkout>/skills`, `AISCIENTIST_PIPELINES_DIR=<checkout>/pipelines`. Its contract
tests check that every tool a skill or pipeline names exists.

History: extracted from KrimsonSun/BioAgentPrototype with git filter-repo (`preset_pipelines/` was
renamed to `pipelines/`).
"""

TOOLS_README = """# AiScientist-tools

The model-callable tools of AiScientist, one folder per tool. The index of every tool, with what it
does and where it runs, is [src/aiscientist/tools/README.md](src/aiscientist/tools/README.md); each tool's
own documentation is its `TOOL.md`.

* Distribution `aiscientist-tools`; import path `aiscientist.tools` (a portion of the `aiscientist`
  namespace package, shared with the platform repository, so the job images' `python -m
  aiscientist.tools...` entry points keep their names).
* The contract with the platform: `sdk.py` (the tool record and context), `catalog.py` (discovery
  from `TOOL.md`) and `api.py` (everything else the platform may use). The tools import nothing
  from the platform; `tests/test_tools_boundary.py` enforces it.
* After changing a tool: `python scripts/tool_docs.py` (regenerates the generated sections of every
  `TOOL.md` and the index), then `python -m pytest`.

History: extracted from KrimsonSun/BioAgentPrototype with git filter-repo.
"""

TOOLS_PYPROJECT = """[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "aiscientist-tools"
version = "0.1.0"
description = "The model-callable tools of AiScientist, one folder per tool."
requires-python = ">=3.10"
dependencies = []

[project.optional-dependencies]
# The heavy science stack lives in the HPC3 images; install it locally to run the tool tests.
analysis = ["scanpy", "anndata", "gseapy", "pydeseq2", "h5py"]
test = ["pytest"]

[tool.setuptools.packages.find]
where = ["src"]
include = ["aiscientist.tools*"]
namespaces = true

[tool.setuptools.package-data]
"*" = ["*.md", "*.txt", "*.tsv", "*.tsv.gz", "*.json"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
"""


def run(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"$ {' '.join(args)}\n{r.stdout}{r.stderr}")
    return r.stdout


def tools_only_tests(repo: Path) -> list[str]:
    """Tests whose every ``aiscientist`` reference (imports and dotted string targets) is under
    ``aiscientist.tools``: they test the tools alone, so they move with them."""
    out = []
    for p in sorted((repo / "tests").glob("test_*.py")):
        text = p.read_text(encoding="utf-8")
        mods: set[str] = set()
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] == "aiscientist":
                mods.add(n.module)
                if n.module == "aiscientist":
                    mods |= {f"aiscientist.{a.name}" for a in n.names}
            elif isinstance(n, ast.Import):
                mods |= {a.name for a in n.names if a.name.split(".")[0] == "aiscientist"}
        mods |= set(re.findall(r'"(aiscientist\.[a-z_.]+)', text))
        mods.discard("aiscientist")
        if mods and all(m.startswith("aiscientist.tools") for m in mods):
            out.append(f"tests/{p.name}")
    return out


def fresh_clone(dest: Path, branch: str) -> None:
    if dest.exists():
        raise SystemExit(f"{dest} exists; pick an empty --out")
    run("git", "clone", "-q", "--no-local", "--single-branch", "--branch", branch, str(ROOT), str(dest))


def commit_all(repo: Path, message: str) -> None:
    run("git", "add", "-A", cwd=repo)
    run("git", "-c", "user.name=split-rehearsal", "-c", "user.email=split@localhost",
        "commit", "-q", "-m", message, cwd=repo)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="an empty directory to create the three repositories in")
    ap.add_argument("--branch", default=None, help="the branch to split (default: the current one)")
    a = ap.parse_args(argv)
    if shutil.which("git-filter-repo") is None:
        raise SystemExit("git filter-repo is not installed (pip install git-filter-repo)")
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    branch = a.branch or run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=ROOT).strip()
    tests = tools_only_tests(ROOT)

    # 1. AiScientist-skills
    skills = out / "AiScientist-skills"
    fresh_clone(skills, branch)
    run("git", "filter-repo", "--force", "--path", "skills/", "--path", "preset_pipelines/",
        "--path-rename", "preset_pipelines/:pipelines/", cwd=skills)
    (skills / "README.md").write_text(SKILLS_README, encoding="utf-8")
    commit_all(skills, "docs: what this repository is and how the platform reads it")

    # 2. AiScientist-tools
    tools = out / "AiScientist-tools"
    fresh_clone(tools, branch)
    paths = ["src/aiscientist/tools/", "tests/fixtures/", "scripts/tool_docs.py", *tests]
    run("git", "filter-repo", "--force", *[x for p in paths for x in ("--path", p)], cwd=tools)
    (tools / "pyproject.toml").write_text(TOOLS_PYPROJECT, encoding="utf-8")
    (tools / "README.md").write_text(TOOLS_README, encoding="utf-8")
    commit_all(tools, "build: package the tools as aiscientist-tools (import path aiscientist.tools)")

    # 3. AiScientist (the platform keeps its history; one commit removes what moved)
    platform = out / "AiScientist"
    fresh_clone(platform, branch)
    moved = ["skills", "preset_pipelines", "src/aiscientist/tools", "scripts/tool_docs.py",
             "src/aiscientist/__init__.py", *tests]
    run("git", "rm", "-r", "-q", *moved, cwd=platform)
    commit_all(platform, "split: the tools and the skills move to their own repositories\n\n"
               "aiscientist becomes a namespace package: src/aiscientist/__init__.py is removed so that\n"
               "AiScientist-tools can provide aiscientist.tools.")

    py = sys.executable
    print(f"""Three repositories in {out}:
  AiScientist-skills  AiScientist-tools ({len(tests)} test files moved)  AiScientist

Test each the way the others consume it:

  cd {tools} && PYTHONPATH=src {py} -m pytest -q
  cd {platform} && PYTHONPATH=src:{tools}/src \\
      AISCIENTIST_SKILLS_DIR={skills}/skills AISCIENTIST_PIPELINES_DIR={skills}/pipelines \\
      {py} -m pytest -q
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
