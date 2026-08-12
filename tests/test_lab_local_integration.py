"""Local END-TO-END integration: the real ResearchLab loop driving the REAL scanpy
analysis tools on a synthetic dataset — no HPC3, no live LLM (the PI/Critic/Scientist
turns are scripted), but the tools, CodeAct sandbox, and bundle are all real.

This is the fast local debug loop: it exercises the whole chain (PI agenda → multi-step
Scientist running real run_scanpy_qc/run_clustering/run_de + a CodeAct snippet that
reads the dataset → Critic → categorized bundle) without a GPU. Skipped where the
analysis stack isn't installed (light CI); runs locally + on the eye-server.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("scanpy")
pytest.importorskip("leidenalg")

from bioagent.agents.registry import build_scientist_catalog  # noqa: E402
from bioagent.agents.research_harness import HarnessContext, ResearchHarness  # noqa: E402
from bioagent.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from bioagent.agents.sandbox import CodeSandbox  # noqa: E402


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
    """Per step, call the matching real tool once, then finish on the next turn."""
    import re

    def chat(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):   # a tool result is in → finish
            return _tool_call("finish", {"answer": "step done"})
        brief = next((m["content"] for m in messages if m["role"] == "user"), "")
        m = re.search(r"Step to execute now:\s*(.+)", brief)   # the CURRENT step only
        step = (m.group(1) if m else brief).lower()
        if "qc" in step:
            return _tool_call("run_scanpy_qc", {"min_genes": 5, "max_pct_mt": 100, "n_top_genes": 30})
        if "cluster" in step:
            return _tool_call("run_clustering", {"resolution": 1.0, "n_neighbors": 10, "n_pcs": 10})
        if "expression" in step or "marker" in step:
            return _tool_call("run_de", {"n_genes": 20})
        if "code" in step:
            return _tool_call("run_code", {"code":
                "import os, anndata as ad\n"
                "a = ad.read_h5ad(os.environ['BIOAGENT_DATASET'])\n"
                "open(os.path.join(os.environ['BIOAGENT_ARTIFACTS'], 'tables', 'codeact_shape.txt'), 'w')"
                ".write(f'{a.n_obs}x{a.n_vars}')\n"
                "print('codeact read', a.shape)\n"})
        return _tool_call("finish", {"answer": "nothing to do"})
    return chat


def test_full_lab_loop_runs_real_tools_locally(tmp_path):
    ds = tmp_path / "synthetic.h5ad"
    _synthetic_h5ad(ds)
    run = tmp_path / "run"
    art = run / "artifacts"
    (art / "tables").mkdir(parents=True)

    ctx = HarnessContext(decisions={"dataset_path": str(ds)}, workspace=run, model="test")
    sandbox = CodeSandbox(dataset_path=str(ds), work_dir=str(run / "work"), artifacts_dir=str(art))
    scientist = ResearchHarness(catalog=build_scientist_catalog(code_executor=sandbox), chat_fn=_smart_scientist())
    agenda = ["Run scanpy QC", "Cluster the cells", "Differential expression markers", "Custom code check"]
    lab = ResearchLab(ctx, LabConfig(max_rounds=8), complete_fn=_scripted_pi_critic(agenda), scientist=scientist)

    result = lab.run("Characterize the synthetic dataset")

    assert result.accepted_steps == len(agenda) and result.converged
    # real scanpy artifacts landed
    figs = list((art / "figures").glob("*.png"))
    tables = list((art / "tables").glob("*.csv"))
    assert any("umap" in f.name for f in figs)            # clustering produced a UMAP
    assert any(t.name.startswith("de_") for t in tables)  # DE wrote ranked tables
    # the CodeAct snippet read the dataset via BIOAGENT_DATASET and wrote an artifact
    assert (art / "tables" / "codeact_shape.txt").read_text() == "240x60"

    # Regression (read-out bug): run_de asked for 20 genes/group, so the RETURN must expose the
    # true count (20) via explicit fields — NOT let a reviewer infer "10" from the capped
    # ``top_genes_by_group`` preview. Cross-check the reported counts against the on-disk CSVs.
    de_result = next(
        (s["result"] for r in result.rounds for s in r.scientist_result.get("steps", [])
         if s.get("tool") == "run_de" and isinstance(s.get("result"), dict)),
        None,
    )
    assert de_result is not None and de_result.get("status") == "ok"
    assert de_result["n_genes_per_group"] == 20
    assert de_result["de_rows_total"] > 10
    assert de_result["de_rows_by_group"] and all(v > 10 for v in de_result["de_rows_by_group"].values())
    assert all(len(v) <= 10 for v in de_result["top_genes_by_group"].values())  # preview stays capped
    for grp, n in de_result["de_rows_by_group"].items():
        csv = art / "tables" / f"de_leiden_{grp}.csv"
        assert len(csv.read_text().splitlines()) - 1 == n   # reported count == real CSV rows
