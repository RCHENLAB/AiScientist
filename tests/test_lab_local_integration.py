"""Local END-TO-END integration: the real ResearchLab loop driving the REAL CodeAct sandbox on a
synthetic dataset. No HPC3 and no live LLM (the PI, Critic and Scientist turns are scripted), but
the catalog, the sandbox and the artifacts are real.

It exercises the chain the platform owns: PI agenda -> a Scientist that runs ``run_code`` snippets
which read the dataset -> Critic -> files under ``artifacts/``. The scientific tools run in their
MCP servers' images (``lijinbio/aiscientist-mcp``) and are tested there, not here. Skipped where
anndata is not installed (light CI).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("anndata")

from aiscientist.agents.registry import build_scientist_catalog  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from aiscientist.agents.sandbox import CodeSandbox  # noqa: E402


def _synthetic_h5ad(path):
    """Two cell populations × distinct marker genes, so clustering finds >=2 groups
    and DE has something to rank — deterministic, no network."""
    import anndata as ad
    import numpy as np

    rng = np.random.default_rng(0)
    n_per, n_genes = 120, 60
    pop_a = rng.poisson(1.0, size=(n_per, n_genes))
    pop_a[:, :15] += 8                      # group A markers
    pop_b = rng.poisson(1.0, size=(n_per, n_genes))
    pop_b[:, 15:30] += 8                    # group B markers
    X = np.vstack([pop_a, pop_b]).astype("float32")
    var_names = [f"MT-{i}" if i < 3 else f"GENE{i}" for i in range(n_genes)]
    a = ad.AnnData(X)
    a.var_names = var_names
    a.obs_names = [f"cell{i}" for i in range(X.shape[0])]
    a.write_h5ad(path)


def _scripted_pi_critic(agenda):
    def complete(messages):
        sys = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in sys:
            return json.dumps(agenda)
        if "rigorous scientific Critic" in sys:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "grounded in tool output"})
        return "FINAL REPORT: synthetic PBMC-like data resolved into clusters with markers."
    return complete


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _smart_scientist():
    """Per step, run one snippet in the real sandbox, then finish on the next turn."""
    import re

    def snippet(name, expr):
        return _tool_call("run_code", {"code":
            "import os, anndata as ad\n"
            "a = ad.read_h5ad(os.environ['AISCIENTIST_DATASET'])\n"
            f"open(os.path.join(os.environ['AISCIENTIST_ARTIFACTS'], 'tables', '{name}'), 'w').write({expr})\n"
            "print('codeact read', a.shape)\n"})

    def chat(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):   # a tool result is in → finish
            return _tool_call("finish", {"answer": "step done"})
        brief = next((m["content"] for m in messages if m["role"] == "user"), "")
        m = re.search(r"Step to execute now:\s*(.+)", brief)   # the CURRENT step only
        step = (m.group(1) if m else brief).lower()
        if "shape" in step:
            return snippet("codeact_shape.txt", "f'{a.n_obs}x{a.n_vars}'")
        if "mitochondrial" in step:
            return snippet("codeact_mt.txt", "str(sum(v.startswith('MT-') for v in a.var_names))")
        return _tool_call("finish", {"answer": "nothing to do"})
    return chat


def test_full_lab_loop_runs_code_steps_locally(tmp_path):
    ds = tmp_path / "synthetic.h5ad"
    _synthetic_h5ad(ds)
    run = tmp_path / "run"
    art = run / "artifacts"
    (art / "tables").mkdir(parents=True)

    ctx = HarnessContext(decisions={"dataset_path": str(ds)}, workspace=run, model="test")
    sandbox = CodeSandbox(dataset_path=str(ds), work_dir=str(run / "work"), artifacts_dir=str(art))
    scientist = ResearchHarness(catalog=build_scientist_catalog(code_executor=sandbox), chat_fn=_smart_scientist())
    agenda = ["Write the dataset's shape with code", "Count the mitochondrial genes with code"]
    lab = ResearchLab(ctx, LabConfig(max_rounds=8), complete_fn=_scripted_pi_critic(agenda), scientist=scientist)

    result = lab.run("Characterize the synthetic dataset")

    assert result.accepted_steps == len(agenda) and result.converged
    # each snippet read the dataset through AISCIENTIST_DATASET and wrote its file under artifacts/
    assert (art / "tables" / "codeact_shape.txt").read_text() == "240x60"
    assert (art / "tables" / "codeact_mt.txt").read_text() == "3"
    # both steps ran in the sandbox, as run_code, and nothing else was called
    tools = [s.get("tool") for r in result.rounds for s in r.scientist_result.get("steps", [])]
    assert [t for t in tools if t != "finish"] == ["run_code", "run_code"]
