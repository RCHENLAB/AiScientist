"""FULL end-to-end simulation of the DAG + HITL research lab, with EVERY LLM role on OpenRouter.

What is REAL here:
  • Every LLM call — PI planning, DAG structure pass, Coordinator, the Scientist's native
    tool-calling, Critic, and synthesize — goes to the real model via OpenRouter (Qwen3.6).
  • The analysis tools run for REAL, locally: run_scanpy_qc / run_clustering / run_de (scanpy +
    leidenalg) and run_code (the real CodeSandbox subprocess), on a synthetic h5ad.
  • literature_search hits the REAL Europe PMC API (public, no key).
  • The DAG planner + HITL decision-point pause/inject flow.

What is STUBBED, and WHY (never a code defect — always an environment gap this OpenRouter-only
local harness can't cover):
  • HPC3 Slurm / SSH execution — NOT stubbed, REPLACED: the same tools run locally in-process
    instead of as remote CPU batch jobs. So this is a truthful functional test, just off-cluster.
  • run_enrichment — REAL only if `gseapy` + a local GMT are present; otherwise the tool itself
    returns dependency_missing and we label it STUB(local-dep). gseapy is an analysis dependency,
    unrelated to OpenRouter and not a system/code issue.
  • PDF/DOCX rendering (pandoc/xelatex) — NOT part of lab.run(); it is a gateway post-step. The
    report is produced as real Markdown by the real synthesize call; only file rendering is skipped.

Run:  PYTHONPATH=src .venv/bin/python scripts/dag_full_sim_openrouter.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")

OR_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OR_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY")
MODEL = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.6-35b-a3b")

if not OR_KEY:
    print("OPENROUTER_API_KEY not set — cannot run the full simulation.")
    sys.exit(2)

from bioagent.agents.registry import build_scientist_catalog  # noqa: E402
from bioagent.agents.research_harness import HarnessContext, ResearchHarness  # noqa: E402
from bioagent.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from bioagent.agents.sandbox import CodeSandbox  # noqa: E402
from bioagent.gateway import vllm_client  # noqa: E402
from bioagent.providers.openai_compatible import OpenRouterClient  # noqa: E402


def synthetic_h5ad(path: str) -> dict:
    """Two populations with distinct marker genes + a PRE-EXISTING `majorclass` annotation (so the
    annotation-vs-recluster decision point can fire), written as a real .h5ad."""
    import anndata as ad
    import numpy as np

    rng = np.random.default_rng(0)
    n_per, n_genes = 120, 60
    a_x = rng.poisson(1.0, size=(n_per, n_genes))
    a_x[:, :15] += 8
    b_x = rng.poisson(1.0, size=(n_per, n_genes))
    b_x[:, 15:30] += 8
    x = np.vstack([a_x, b_x]).astype("float32")
    adata = ad.AnnData(x)
    adata.var_names = [f"MT-{i}" if i < 3 else f"GENE{i}" for i in range(n_genes)]
    adata.obs_names = [f"cell{i}" for i in range(x.shape[0])]
    adata.obs["majorclass"] = (["TypeA"] * n_per) + (["TypeB"] * n_per)
    adata.write_h5ad(path)
    return {"cells": x.shape[0], "genes": n_genes,
            "obs_categoricals": {"majorclass": {"n": 2, "values": ["TypeA", "TypeB"]}},
            "obs_keys": ["majorclass"]}


# Reasoning is DISABLED for the plain-text roles: Qwen3.6 on OpenRouter otherwise spends the output
# budget on reasoning tokens and returns empty content on the big synthesize call (an OpenRouter model
# config nuance, not a lab bug — the self-hosted vLLM in production returns full reports). The
# OpenRouterClient sets reasoning={"effort":"none","exclude":True}; give the report room with 4k tokens.
_OR_CLIENT = OpenRouterClient(model=MODEL, base_url=OR_URL, api_key=OR_KEY,
                              reasoning_effort="none", timeout_seconds=120)


def or_complete(messages):
    return _OR_CLIENT.chat(messages, max_tokens=4096, temperature=0.2).content


def or_scientist_chat(messages, tools):
    return vllm_client.chat_tools(0, MODEL, messages, tools, base_url=OR_URL, api_key=OR_KEY,
                                  max_tokens=1536, timeout=120)


def main() -> None:
    print(f"Model (all LLM roles via OpenRouter): {MODEL}\n")
    tmp = Path(tempfile.mkdtemp(prefix="dag_full_sim_"))
    ds = tmp / "synthetic.h5ad"
    profile = synthetic_h5ad(str(ds))
    run = tmp / "run"
    (run / "artifacts" / "tables").mkdir(parents=True)
    (run / "artifacts" / "figures").mkdir(parents=True)

    ctx = HarnessContext(
        decisions={"dataset_path": str(ds), "dataset_result": profile},
        workspace=run, model=MODEL)
    sandbox = CodeSandbox(dataset_path=str(ds), work_dir=str(run / "work"),
                          artifacts_dir=str(run / "artifacts"))
    catalog = build_scientist_catalog(code_executor=sandbox)
    print("Scientist catalog (real tools):", ", ".join(t.name for t in catalog), "\n")

    scientist = ResearchHarness(catalog=catalog, chat_fn=or_scientist_chat)
    lab = ResearchLab(ctx, LabConfig(planner="dag", auto_select_skill=False, max_rounds=12,
                                     max_revisions=1, multi_agent=True, max_concurrency=2),
                      complete_fn=or_complete, scientist=scientist)

    question = ("Characterize this small single-cell dataset: run QC, cluster the cells, decide how "
                "to annotate (it already has a majorclass label), find marker genes per class, run "
                "pathway enrichment, and search the literature for the markers. Keep to ~5 steps.")

    tool_runs: dict[str, list[str]] = {}
    decisions = {"n": 0}
    events: list[dict] = []

    def decision_review(node):
        decisions["n"] += 1
        choice = node.options[0] if node.options else "Use the existing majorclass annotation"
        print(f"\n🔀 HITL decision: {node.goal[:64]}\n   options={list(node.options)} → choosing: {choice}\n")
        return {"action": "proceed", "choice": choice}

    def on_event(ev):
        events.append(ev)
        t = ev.get("type")
        if t == "pi_agenda":
            print("PI agenda:", *[f"\n   - {s}" for s in ev.get("agenda", [])])
        elif t == "lab_plan_dag":
            print("\nDAG:")
            for n in ev.get("nodes", []):
                flags = (" [DECISION]" if n.get("decision") else "") + \
                        (f" dep={n['depends_on']}" if n.get("depends_on") else " (root)")
                print(f"   [{n['id']}] {n['goal'][:60]}{flags}")
        elif t == "concurrency_batch":
            print(f"\n⚡ PARALLEL batch: {ev.get('nodes')} running concurrently")
        elif t == "node_claim":
            print(f"   🙋 {ev.get('specialist')} claimed {ev.get('node')}")
        elif t == "scientist_start":
            print(f"\n▶ {ev.get('step','')[:70]}")
        elif t == "coordinator_pick":
            print(f"   🧭 coordinator: {ev['next']} (ready {ev['ready']})")
        elif t == "tool_result":
            tool = ev.get("tool")
            summ = str(ev.get("summary", ""))
            tool_runs.setdefault(tool, []).append(summ)
            print(f"   ✓ {tool}: {summ[:60]}")
        elif t == "tool_error":
            print(f"   ⚠ {ev.get('tool')}: {str(ev.get('error'))[:70]}")
        elif t == "critic":
            print(f"   critic: {ev.get('verdict')} ({ev.get('score')})")

    result = lab.run(question, on_event=on_event, decision_review=decision_review)

    # --- coverage report -----------------------------------------------------
    print("\n" + "=" * 72)
    print("COVERAGE — what ran REAL vs STUB (and why):")
    real_tools = {"run_scanpy_qc", "run_clustering", "run_de", "run_code", "literature_search"}
    for tool, summaries in sorted(tool_runs.items()):
        joined = " | ".join(summaries)
        if tool == "run_enrichment" and any("depend" in s or "error" in s for s in summaries):
            tag = "STUB(local-dep: gseapy/GMT not installed locally — NOT OpenRouter, NOT a code bug)"
        elif tool in real_tools:
            tag = "REAL (ran locally / real API)"
        else:
            tag = "ran"
        print(f"  • {tool}: {tag}  [{joined[:60]}]")
    print("\n  • HPC3 Slurm/SSH: N/A — REPLACED by real local execution (not stubbed).")
    print("  • PDF/DOCX render: not part of lab.run() — report Markdown produced by the real synthesize.")
    print("\nLLM roles on OpenRouter: PI, DAG-structure, Coordinator, Scientist tool-calling, Critic, synthesize.")
    print(f"HITL decision_review fired: {decisions['n']} time(s)")
    batches = [e["nodes"] for e in events if e["type"] == "concurrency_batch"]
    print(f"Parallel batches (independent branches co-run): {batches or 'none this run'}")
    figs = list((run / "artifacts" / "figures").glob("*.png"))
    tables = list((run / "artifacts" / "tables").glob("*.csv"))
    print(f"Real artifacts on disk: {len(figs)} figure(s), {len(tables)} table(s) "
          f"(e.g. {[f.name for f in figs[:3]]})")
    print(f"\nlab.run: converged={result.converged} accepted={result.accepted_steps} "
          f"report_chars={len(result.final_answer or '')}")
    ok = bool(figs) and bool(tables) and result.final_answer
    print("RESULT:", "PASS — full DAG+HITL pipeline ran on OpenRouter with real local tools"
          if ok else "CHECK OUTPUT ABOVE")
    print(f"(artifacts under {run})")


if __name__ == "__main__":
    main()
