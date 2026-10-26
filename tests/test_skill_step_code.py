"""The code a skill step ran, as the Critic reads it, and the skill counted once per step.

Prod run 74873590ca7b (2026-10-23) is the case. The Critic got each run_code script of a skill step
cut at a fixed 2,000 characters; the scoring script was 3,514, its `sc.tl.score_genes(` call began at
character 2,167, and the Critic could only ask whether the skill's method had been used. And
skill_versions.sqlite3 got 7 successes for the run: every run_code of the three steps that had the
skill, two of them only listing files.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from aiscientist.agents import skills_registry as registry
from aiscientist.agents.loop_utils import ContextThreadPoolExecutor
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.skill_code import calls_to, excerpt, fit_scripts, skill_functions
from aiscientist.agents.skills import Skill

SCORING = "scanpy.tl.score_genes"
F = (SCORING,)


def _skill(code: str = "", doc: str = "", **kw) -> Skill:
    return Skill(name="s", files={"scripts/reference.py": code} if code else {}, doc=doc, **kw)


# --- a skill's own functions --------------------------------------------------------------------------

SKILL_CODE = '''
import json
import os
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scanpy as sc

adata = sc.read_h5ad(os.environ["AISCIENTIST_WORK"] + "/adata_qc.h5ad")
values = np.asarray(adata.X[:100].todense()).ravel()


def coverage(genes, pool):
    return len(set(genes) & set(pool)) / len(genes)


sc.tl.score_genes(adata, gene_list=["Isg15"], ctrl_size=50, random_state=0)
pd.DataFrame({"n": [1]}).to_csv("t.csv")
fig, ax = plt.subplots()
print(json.dumps({"coverage": coverage(["Isg15"], adata.var_names)}))
adata.write("adata_scored.h5ad")
'''


def test_a_skill_s_functions_are_its_method_not_its_plumbing():
    """Reading and writing files, numpy, pandas, matplotlib and the standard library say nothing
    about the method; the scanpy call and the skill's own helper do."""
    assert skill_functions(_skill(SKILL_CODE)) == ("coverage", SCORING)


def test_tests_are_left_out_and_a_python_block_of_skill_md_counts():
    skill = Skill(name="s", files={"tests/test_x.py": "import scanpy as sc\nsc.pp.pca(a)\n"},
                  doc="Run:\n\n```python\nimport scanpy as sc\nsc.pp.neighbors(adata, n_pcs=30)\n```\n")
    assert skill_functions(skill) == ("scanpy.pp.neighbors",)


def test_the_programs_a_skill_runs_are_its_functions_but_not_the_shell_that_runs_them():
    code = ('import shutil, subprocess\nif not shutil.which("bcftools"):\n    raise SystemExit(1)\n'
            'subprocess.run(["bcftools", "view", "-H", vcf], check=True)\n'
            'subprocess.run(["bash", "-c", pipeline])\n'
            'subprocess.run(f"tabix -p vcf {out}", shell=True)\n')
    assert skill_functions(_skill(code)) == ("bcftools (program)", "tabix (program)")


def test_a_skill_that_calls_only_the_general_libraries_is_known_by_those_calls():
    code = "import pandas as pd\nd = pd.read_csv(path)\nbins = pd.qcut(d['nCount_RNA'], 5)\n"
    assert skill_functions(_skill(code)) == ("pandas.qcut",)


def test_a_skill_of_shell_commands_has_no_functions_to_find():
    assert skill_functions(Skill(name="s", files={"scripts/run.sh": "cellqc run --config c.yaml\n"})) == ()


# --- which of them a step's code calls -------------------------------------------------------------------

@pytest.mark.parametrize("code", [
    "import scanpy as sc\nsc.tl.score_genes(adata, GENES)",
    "import scanpy as scp\nscp.tl.score_genes(adata, GENES)",
    "from scanpy.tl import score_genes\nscore_genes(adata, GENES)",
    "import scanpy as sc\nif True:\n    sc.tl.score_genes(adata, GENES)",
    "sc.tl.score_genes(adata, GENES",            # does not parse: found by its name and a bracket
])
def test_a_call_is_found_whatever_the_code_calls_its_library(code):
    assert calls_to(code, F) == [SCORING]


@pytest.mark.parametrize("code", [
    "import scanpy as sc\nadata = sc.read_h5ad(p)\nprint(adata.obs['score_ISG'].median())",
    "score_genes = 'a name, not a call'",
    "import pandas as pd\npd.read_csv('t.csv').groupby('majorclass')['score_ISG'].median()",
])
def test_code_that_only_reads_what_the_skill_made_calls_none_of_it(code):
    assert calls_to(code, F) == []


def test_a_copied_helper_and_a_program_in_a_string_are_found():
    assert calls_to("def coverage(g, p):\n    return 1\nc = coverage(G, P)", ("coverage",)) == ["coverage"]
    bcftools = ("bcftools (program)",)
    assert calls_to('import subprocess\nsubprocess.run("bcftools norm -m -any in.vcf", shell=True)',
                    bcftools) == list(bcftools)
    assert calls_to("bcftools norm -m -any in.vcf.gz | bgzip > out.vcf.gz", bcftools) == list(bcftools)
    assert calls_to("import subprocess\nsubprocess.run(['bcftoolsx', 'x'])", bcftools) == []


# --- an excerpt keeps the skill's lines ----------------------------------------------------------------

def _scoring_script() -> str:
    head = ["import os", "import numpy as np", "import scanpy as sc",
            "adata = sc.read_h5ad(os.path.join(os.environ['AISCIENTIST_WORK'], 'adata_qc.h5ad'))"]
    checks = [f"detected_{i} = float((adata.raw[:, GENES[{i % 8}]].X > 0).mean())" for i in range(30)]
    call = ["assert coverage >= 0.8, 'NOT SCORED'", "sc.tl.score_genes(",
            "    adata, gene_list=GENES, score_name='score_ISG',",
            "    ctrl_size=50, random_state=0, use_raw=True,", ")"]
    tail = [f"q_{i} = adata.obs.groupby('majorclass')['score_ISG'].quantile({i / 10})" for i in range(1, 15)]
    return "\n".join(head + checks + call + tail + ["adata.write('adata_scored.h5ad')"])


def _gaps(text: str) -> "list[int]":
    return [int(n) for n in re.findall(r"^# \[\.\.\. (\d+) lines left out \.\.\.\]$", text, re.M)]


def test_a_long_script_keeps_the_skill_call_and_the_lines_around_it():
    script = _scoring_script()
    assert script.index("sc.tl.score_genes(") > 1_500, "a fixed prefix of the limit would miss it"
    out = excerpt(script, F, 1_000)
    lines, kept = script.splitlines(), out.splitlines()
    assert len(out) <= 1_000
    call = "\n".join(lines[lines.index("sc.tl.score_genes("):][:4])
    assert call in out, "the whole multi-line call"
    assert "assert coverage >= 0.8, 'NOT SCORED'" in kept, "the guard just before it"
    assert kept[0] == lines[0] and kept[-1] == lines[-1], "the first and last lines"
    assert _gaps(out) and sum(_gaps(out)) + len(kept) - len(_gaps(out)) == len(lines)
    shown = [ln for ln in kept if not _gaps(ln)]
    assert shown == [ln for ln in lines if ln in set(shown)], "lines stay in their order"


def test_a_script_that_fits_is_whole_and_one_without_skill_calls_keeps_its_ends():
    script = _scoring_script()
    assert excerpt(script, F, len(script)) == script
    plain = "\n".join(f"x_{i} = {i}" for i in range(200))
    out = excerpt(plain, F, 300)
    assert len(out) <= 300 and out.startswith("x_0 = 0\n") and out.endswith("x_199 = 199")


# --- the step's scripts within the budget ----------------------------------------------------------------

def _steps(*codes, ok=True):
    return [{"tool": "run_code", "args": {"code": c}, "ok": ok} for c in codes]


def test_every_script_is_whole_while_the_step_fits_the_budget():
    script = _scoring_script()
    steps = (_steps("print(1)") + [{"tool": "inspect_dataset", "args": {}, "ok": True}]
             + _steps(script, ok=False)
             + [{"tool": "run_in_environment", "args": {"skill": "x", "command": "cellqc run"}, "ok": True},
                {"tool": "finish", "args": {"answer": "done"}, "ok": True}])
    shown, not_shown = fit_scripts(steps, F)
    assert not_shown == 0
    assert [(s["tool_call"], s["tool"], s["ok"], s["shown"]) for s in shown] == [
        (1, "run_code", True, "full"), (3, "run_code", False, "full"), (4, "run_in_environment", True, "full")]
    assert shown[1]["code"] == script and shown[1]["calls_skill_functions"] == [SCORING]


def test_past_the_budget_short_scripts_stay_whole_and_long_ones_share_the_rest():
    script = _scoring_script()
    shown, _ = fit_scripts(_steps("print(1)", script, script + "\nprint('again')"), F, budget=3_000)
    assert [s["shown"] for s in shown] == ["full", "excerpt", "excerpt"]
    assert shown[0]["code"] == "print(1)"
    assert all(len(s["code"]) <= 1_500 and "sc.tl.score_genes(" in s["code"] for s in shown[1:])
    assert sum(len(s["code"]) for s in shown) <= 3_000


def test_a_step_with_many_scripts_shows_the_ones_that_call_the_skill_first():
    look = [f"print(os.listdir('{i}'))" for i in range(13)]
    shown, not_shown = fit_scripts(_steps(*look, _scoring_script()), F, max_scripts=12)
    assert not_shown == 2
    assert [s["tool_call"] for s in shown] == [*range(1, 12), 14], "in the order they ran"
    assert shown[-1]["calls_skill_functions"] == [SCORING]


# --- one count per step ------------------------------------------------------------------------------------

SKILL = Skill(name="score-gene-signatures-with-scanpy", version="1.0.0", registry_release="v0.1.0",
              requirements=registry.requirements({}),
              files={"scripts/reference.py": "import scanpy as sc\nsc.tl.score_genes(adata, G, ctrl_size=50)\n"})
SCORE = "import scanpy as sc\nsc.tl.score_genes(adata, GENES, ctrl_size=50)"
LOOK = "import os\nprint(os.listdir(os.environ['AISCIENTIST_WORK']))"


class Sandbox:
    supports_skill_policy = True


@pytest.fixture
def counts(tmp_path, monkeypatch):
    path = tmp_path / "counts.sqlite3"
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_STATS", str(path))

    def read():
        if not path.exists():
            return []
        with sqlite3.connect(path) as db:
            return db.execute("SELECT name, success, error FROM skill_versions ORDER BY name").fetchall()
    return read


def _call(ctx, code, status="ok", *, skill=SKILL, tool="run_code"):
    return registry.execute(skill, ctx, Sandbox(), lambda: {"status": status}, tool, code=code)


def test_a_step_counts_once_by_the_calls_that_ran_the_skill(counts):
    ctx = HarnessContext(decisions={})
    with registry.step_scope(SKILL, step="score"):
        _call(ctx, LOOK)
        _call(ctx, SCORE)
        _call(ctx, LOOK)
    assert counts() == [(SKILL.name, 1, 0)]


def test_a_step_whose_scoring_failed_is_one_error_whatever_else_worked(counts):
    ctx = HarnessContext(decisions={})
    with registry.step_scope(SKILL, step="score"):
        _call(ctx, LOOK)
        _call(ctx, SCORE, "error")
    assert counts() == [(SKILL.name, 0, 1)]


def test_a_step_that_only_read_what_the_skill_made_is_not_counted(counts):
    ctx = HarnessContext(decisions={})
    with registry.step_scope(SKILL, step="compare"):
        _call(ctx, LOOK)
        _call(ctx, "import pandas as pd\npd.read_csv('scores.csv').groupby('majorclass').median()")
    assert counts() == []


def test_the_attempts_of_one_step_are_one_use(counts):
    ctx = HarnessContext(decisions={})
    with registry.step_scope(SKILL, step="score"):
        _call(ctx, SCORE, "error")
    assert counts() == [(SKILL.name, 0, 1)]
    with registry.step_scope(SKILL, step="score"):
        _call(ctx, SCORE)
    assert counts() == [(SKILL.name, 1, 0)], "the revision that worked turns the step's error into a success"
    with registry.step_scope(SKILL, step="score"):
        _call(ctx, SCORE, "error")
    assert counts() == [(SKILL.name, 1, 0)], "a step that ran the skill stays a success"
    with registry.step_scope(SKILL, step="figures"):
        _call(ctx, SCORE)
    assert counts() == [(SKILL.name, 2, 0)], "another step is another use"
    with registry.step_scope(SKILL, step="score"):
        _call(HarnessContext(decisions={}), SCORE)
    assert counts() == [(SKILL.name, 3, 0)], "so is the same step of another run"
    for _ in range(2):
        with registry.step_scope(SKILL):
            _call(ctx, SCORE)
    assert counts() == [(SKILL.name, 5, 0)], "a scope that names no step is a step of its own"


def test_a_stopped_call_counts_nothing_and_a_crashed_step_still_counts(counts):
    ctx = HarnessContext(decisions={})
    with registry.step_scope(SKILL, step="stopped"):
        _call(ctx, SCORE, "cancelled")
    assert counts() == []
    with pytest.raises(RuntimeError):
        with registry.step_scope(SKILL, step="crashed"):
            _call(ctx, SCORE)
            raise RuntimeError("the step died after scoring")
    assert counts() == [(SKILL.name, 1, 0)]


def test_a_command_in_the_skill_s_environment_and_a_skill_of_shell_commands_count_every_call(counts):
    shell = Skill(name="qc-snrna-with-cellqc-standalone", version="1.0.0", registry_release="v0.1.0",
                  requirements=registry.requirements({}), files={"scripts/run.sh": "cellqc run\n"})
    ctx = HarnessContext(decisions={})
    with registry.step_scope(shell, step="qc"):
        _call(ctx, "import subprocess\nsubprocess.run(['cellqc', 'run'])", skill=shell)
    with registry.step_scope(SKILL, step="env"):
        _call(ctx, "cellqc run --config c.yaml", tool="run_in_environment")
    assert counts() == [(shell.name, 1, 0), (SKILL.name, 1, 0)]


def test_parallel_steps_are_counted_apart(counts):
    ctx = HarnessContext(decisions={})

    def step(name):
        with registry.step_scope(SKILL, step=name):
            _call(ctx, SCORE)

    with ContextThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(step, ["score A", "score B"]))
    assert counts() == [(SKILL.name, 2, 0)]


def test_outside_a_step_each_call_is_counted_as_it_ends(counts):
    ctx = HarnessContext(decisions={})
    _call(ctx, LOOK)
    _call(ctx, SCORE, "error")
    assert counts() == [(SKILL.name, 1, 1)]
