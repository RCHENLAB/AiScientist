"""Measure the planning stage against the DDX41 run, with the real served model.

Run `Ziyaoma/f5111e1a2382` produced a plan with eight defects and n=1 cannot say whether that was
the model, the protocol or the scaffolding around them. This turns the question into a measurement.
Two experiments, both driven by the SAME model the lab runs (Qwen3.6-35B-A3B-AWQ on HPC3), against
the SAME dataset profile:

  E1  DAG STABILITY — structure ONE fixed agenda into a dependency DAG N times and compare the
      edge sets. `_structure_agenda_dag` is an LLM call whose output decides execution order, and
      "is the DAG itself unstable" is answerable only by running it more than once. Note what it
      cannot do: the pass is contractually forbidden from changing step TEXT, so it can reorder a
      bad plan but never author one.

  E2  PLAN QUALITY, BEFORE vs AFTER — draft the plan N times under two conditions and score each
      on properties this dataset makes checkable without a judge model:
        BEFORE  the dataset profile as it was (no `orig.ident`, no REPLICATION line, the ⚑ hint
                offering only `groupby`), the old PI prompt (tool names forbidden), no plan review.
        AFTER   everything as deployed at 9ed983f.
      The dataset has 11 `majorclass` labels and ONE library, so the right plan reuses the labels,
      stratifies, and does NOT plan a per-donor pseudobulk. Each of those is a regex away.

Usage:
    AISCIENTIST_PROBE_BASE=http://127.0.0.1:PORT/v1 \\
    PYTHONPATH=src .venv/bin/python scripts/probe_plan_quality.py --trials 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MODEL = os.environ.get("AISCIENTIST_PROBE_MODEL", "QuantTrio/Qwen3.6-35B-A3B-AWQ")
BASE = os.environ.get("AISCIENTIST_PROBE_BASE", "http://127.0.0.1:8000/v1")

QUESTION = 'Call the run_de tool with groupby="sampleid" on this dataset and report exactly what it returns.'

# The agenda that actually ran, verbatim from artifacts/process/agenda.json. E1 structures THIS.
RUN_AGENDA = [
    "Inspect the uploaded h5ad file to verify its format, confirm the presence of the sampleid "
    "column, and validate that it contains the two expected experimental groups (DDX41 and WT).",
    "Calculate per-cell quality metrics, filter out low-quality cells, and normalize the "
    "expression matrix so downstream comparisons reflect biological variation rather than "
    "technical artifacts.",
    "Run the Wilcoxon marker analysis with groupby set to sampleid, computing ranked gene lists "
    "and differential expression statistics between the DDX41 and WT groups across all "
    "individual cells.",
    "Parse the generated DE tables to extract and report the exact tabular output, including the "
    "total number of genes called significant at default thresholds, the top differentially "
    "expressed genes with their log fold-changes and adjusted p-values, and the precise "
    "statistical parameters applied.",
    "Aggregate raw counts per donor to perform a statistically valid pseudobulk differential "
    "expression test between DDX41 and WT, reporting the genuinely significant genes, effect "
    "sizes, and adjusted p-values that properly account for biological replication instead of "
    "treating cells as independent replicates.",
]

# The profile the PI was given on 2026-08-15, rendered from the run's own dataset_results.json.
# `orig.ident` is absent from the categoricals and therefore lands in the "high-cardinality" line —
# which is what let a per-donor step look reasonable.
PROFILE_BEFORE = """Dataset profile: 15307 cells x 33696 genes, h5ad_single_cell.
Metadata (obs) columns with categories:
  - DF.classifications: 1 categories [Singlet]
  - celltype: 87 categories
  - majorclass: 11 categories [AC, BC, Cone, Endothelial, HC, MG, Microglia, Pericyte, RGC, RPE, Rod]
  - sampleid: 2 categories [DDX41, WT]
Other obs columns (numeric / high-cardinality): nCount_RNA, nFeature_RNA, nuclear_fraction, orig.ident, pANN, percent.mt, scANVI_prediction_max_probability
⚑ This dataset is ALREADY annotated with cell types: celltype (87 labels), majorclass (11 labels). Ground marker (run_de) and enrichment analysis on one of THESE columns — pass it as `groupby` (e.g. groupby="celltype") — do NOT run DE/enrichment on de-novo leiden cluster numbers, which are not interpretable on their own. Clustering + UMAP is still fine for showing structure."""


def _dataset_result() -> dict[str, Any]:
    """The run's own dataset_result, plus the `orig.ident` entry the fixed profiler now emits."""
    return {
        "format": "h5ad", "cells": 15307, "genes": 33696, "dataset_kind": "h5ad_single_cell",
        "obs_keys": ["DF.classifications", "celltype", "majorclass", "nCount_RNA", "nFeature_RNA",
                     "nuclear_fraction", "orig.ident", "pANN", "percent.mt",
                     "scANVI_prediction_max_probability", "sampleid"],
        "obs_categoricals": {
            "DF.classifications": {"n": 1, "values": ["Singlet"]},
            "celltype": {"n": 87, "values": []},
            "majorclass": {"n": 11, "values": ["AC", "BC", "Cone", "Endothelial", "HC", "MG",
                                               "Microglia", "Pericyte", "RGC", "RPE", "Rod"]},
            "sampleid": {"n": 2, "values": ["DDX41", "WT"]},
            "orig.ident": {"n": 1, "values": ["0"]},
        },
    }


# --- the model ----------------------------------------------------------------


def make_complete(temperature: float = 0.7):
    def complete(messages: list[dict[str, Any]]) -> str:
        body = json.dumps({"model": MODEL, "messages": messages,
                           "temperature": temperature, "max_tokens": 3000}).encode()
        req = urllib.request.Request(f"{BASE}/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    out = json.loads(r.read())
                return out["choices"][0]["message"]["content"] or ""
            except (urllib.error.URLError, TimeoutError, KeyError) as exc:
                if attempt == 2:
                    print(f"    ! model call failed: {exc}", file=sys.stderr)
                    return ""
        return ""
    return complete


# --- scoring (deterministic; no judge model) ----------------------------------

_RE = {
    # The dataset carries 11 major classes. A plan that never names them re-derives what it has.
    "reuses_labels": re.compile(r"\b(majorclass|celltype|cell[- ]type label|existing (cell[- ])?"
                                r"(type )?(annotation|label))", re.I),
    # There is ONE library. A per-donor aggregation cannot execute on this data.
    "plans_pseudobulk": re.compile(r"\b(pseudobulk|pseudo-bulk|per[- ]donor|per donor|aggregate[^.]"
                                   r"{0,40}(donor|sample|animal))", re.I),
    # Composition is the analysis this dataset most needs and the plan never had.
    "has_composition": re.compile(r"\b(composition|cell[- ]type proportion|proportion[^.]{0,30}"
                                  r"(shift|differ|change)|abundance)", re.I),
    # Stratifying is what separates a real contrast from the pooled test that ran.
    "stratifies": re.compile(r"\b(stratif|within each cell[- ]type|per cell[- ]type|separately "
                             r"(with)?in each)", re.I),
    # A researcher must be able to see which tool runs.
    "names_tools": re.compile(r"\brun_(scanpy_qc|clustering|de|pseudobulk_de|composition|enrichment"
                              r"|gsea_prerank|marker_annotation|code)\b"),
    # Two steps where one declares the other invalid — the defect that shipped.
    "self_contradicts": re.compile(r"instead of[^.]{0,60}(independent replicate|treating cells)"
                                   r"|rather than treating cells", re.I),
}


def score(agenda: list[str], disclosure: str = "") -> dict[str, Any]:
    from aiscientist.agents.research_lab import _is_readback_step
    text = " \n".join(agenda)
    return {
        "n_steps": len(agenda),
        "reuses_labels": bool(_RE["reuses_labels"].search(text)),
        "plans_pseudobulk": bool(_RE["plans_pseudobulk"].search(text)),
        "has_composition": bool(_RE["has_composition"].search(text)),
        "stratifies": bool(_RE["stratifies"].search(text)),
        "names_tools": bool(_RE["names_tools"].search(text)),
        "self_contradicts": bool(_RE["self_contradicts"].search(text)),
        "has_readback": any(_is_readback_step(s) for s in agenda),
        "discloses": bool(disclosure.strip()),
    }


# --- E1: is the DAG stable? ---------------------------------------------------


def experiment_dag(trials: int, complete) -> None:
    from aiscientist.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    print(f"\n=== E1  DAG structuring stability — {trials} structurings of ONE fixed agenda ===")
    lab = ResearchLab(HarnessContext(decisions={"dataset_result": _dataset_result()}),
                      LabConfig(planner="dag"), complete_fn=complete,
                      scientist=ResearchHarness(catalog=default_catalog(), chat_fn=lambda *_: ""))
    shapes: Counter[str] = Counter()
    for i in range(trials):
        plan = lab._structure_agenda_dag(QUESTION, list(RUN_AGENDA), lambda _e: None)
        edges = sorted(f"{d}->{n.id}" for n in plan.nodes for d in (n.depends_on or []))
        shape = ",".join(edges) or "(all roots)"
        shapes[shape] += 1
        print(f"  trial {i + 1:2d}: {len(edges)} edge(s)  {shape[:96]}")
    top, top_n = shapes.most_common(1)[0]
    print(f"\n  distinct graphs : {len(shapes)} / {trials}")
    print(f"  modal graph     : {top_n}/{trials} ({100 * top_n // trials}%)")
    print(f"  is a pure chain : {top == ','.join(f's{i}->s{i + 1}' for i in range(1, len(RUN_AGENDA)))}")


# --- E2: did the fixes change the plan? ---------------------------------------


def _plan_before(complete, trials: int) -> list[dict[str, Any]]:
    """The 2026-08-15 planning call: old prompt, old profile, no review."""
    import subprocess
    from aiscientist.agents.research_lab import _parse_plan

    old_src = subprocess.run(["git", "show", "980b9c2:src/aiscientist/agents/research_lab.py"],
                             cwd=ROOT, capture_output=True, text=True).stdout
    ns: dict[str, Any] = {}
    m = re.search(r"^_PI_SYSTEM = \((.*?)^\)$", old_src, re.S | re.M)
    exec("_PI_SYSTEM = (" + m.group(1) + ")", ns)          # noqa: S102 - the historical prompt
    guidance = (ROOT / "preset_pipelines" / "differential_expression" / "SKILL.md").read_text()
    guidance = guidance.split("\n---", 1)[1].strip()
    out = []
    for i in range(trials):
        raw = complete([
            {"role": "system", "content": ns["_PI_SYSTEM"]},
            {"role": "user", "content": f"Research question:\n{QUESTION}\n\n"
                                        f"Follow this research-path guidance when planning:\n{guidance}\n\n"
                                        f"{PROFILE_BEFORE}\n\nReturn the ordered agenda now."},
        ])
        parsed = _parse_plan(raw, 20, False)
        agenda = parsed[1] if parsed and parsed[0] == "agenda" else []
        s = score(agenda)
        out.append(s)
        print(f"  before trial {i + 1:2d}: {json.dumps(s)}")
    return out


def _plan_after(complete, trials: int) -> list[dict[str, Any]]:
    """The deployed planner: fixed profile, new prompt, plan review on."""
    from aiscientist.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    guidance = (ROOT / "preset_pipelines" / "differential_expression" / "SKILL.md").read_text()
    out = []
    for i in range(trials):
        lab = ResearchLab(HarnessContext(decisions={"dataset_result": _dataset_result()}),
                          LabConfig(preset_prompt=guidance.split("\n---", 1)[1].strip()),
                          complete_fn=complete,
                          scientist=ResearchHarness(catalog=default_catalog(),
                                                    chat_fn=lambda *_: ""))
        kind, payload = lab._pi_plan(QUESTION, lambda _e: None)
        agenda = list(payload) if kind == "agenda" else []
        reviewed = lab._plan_review(QUESTION, agenda, lambda _e: None)
        s = score(reviewed, lab.self_sourced)
        s["review_changed"] = reviewed != agenda
        out.append(s)
        print(f"  after  trial {i + 1:2d}: {json.dumps(s)}")
    return out


def _summarise(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    keys = [k for k in rows[0] if isinstance(rows[0][k], bool)] if rows else []
    return {"arm": name, "trials": len(rows),
            **{k: f"{sum(bool(r.get(k)) for r in rows)}/{n}" for k in keys},
            "mean_steps": round(sum(r["n_steps"] for r in rows) / n, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--only", choices=["dag", "plan"], default=None)
    args = ap.parse_args()
    complete = make_complete()
    print(f"model : {MODEL}\nbase  : {BASE}\ntrials: {args.trials}")

    if args.only != "plan":
        experiment_dag(args.trials, complete)
    if args.only != "dag":
        print(f"\n=== E2  plan quality, BEFORE vs AFTER — {args.trials} plans each ===")
        print("\n[BEFORE — 2026-08-15 prompt + profile, no plan review]")
        before = _plan_before(complete, args.trials)
        print("\n[AFTER — deployed 9ed983f]")
        after = _plan_after(complete, args.trials)
        print("\n--- summary (want: reuses_labels/composition/stratifies/names_tools HIGH; "
              "plans_pseudobulk/has_readback/self_contradicts LOW) ---")
        for row in (_summarise("before", before), _summarise("after", after)):
            print(json.dumps(row))


if __name__ == "__main__":
    main()
