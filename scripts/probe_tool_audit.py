"""Can the model FIND the defect once it can read the code?

`read_tool_source` removes the asymmetry that hid `run_de`'s 50-gene cap. That is necessary,
not sufficient: a tool the model does not act on is worthless. This probe measures the second
half against the real served model.

Each scenario hands the model what `read_tool_source` actually returns — body, declared
description, extracted defaults — plus the downstream step that is about to consume the output,
and asks the question a reviewer would ask. Two scenarios contain a real defect; one is a
CONTROL with no defect, because a model that flags everything is as useless as one that flags
nothing.

Scored on whether the model names the specific parameter, not on whether it sounds concerned.

    python scripts/probe_tool_audit.py --base-url http://127.0.0.1:8000/v1 \
        --model QuantTrio/Qwen3.6-35B-A3B-AWQ
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

SYSTEM = (
    "You are reviewing one step of a bioinformatics pipeline you are running. You can read the "
    "SOURCE of your own tools. A tool's description states its intent; only the source states "
    "its behaviour — the caps, thresholds and defaults that decide the numbers you will report. "
    "Judge each default against THIS dataset and THIS downstream step.\n\n"
    "Most defaults are conventional and correct, so 'no problem' is the common and expected "
    "answer. Flagging a sound step costs as much as missing a bad one, because an audit that "
    "objects to everything gets ignored. Report a problem ONLY when you can name (a) the "
    "specific parameter, (b) the concrete wrong OUTPUT it produces on this dataset, and (c) "
    "which downstream step consumes that output and is damaged by it. 'A different value might "
    "be better' is NOT a problem; 'this silently caps or truncates, and step X needs what was "
    "dropped' is. Go through EVERY entry in `defaults` and report EVERY one that qualifies — a "
    "step can have more than one defect, and the one you notice first is not necessarily the "
    "worst.\n\n"
    'Answer with a JSON object: {"problems": [{"parameter": "<name>", "why": "<one sentence '
    'naming the wrong output and the damaged step>", "fix": "<what you would do>"}]}. '
    "An empty list means the step is sound."
)

RUN_DE_SOURCE = '''
def run_de(args, ctx):
    """rank_genes_groups (Wilcoxon) per group. Writes one ranked DE table per group
    + a combined table. Returns the top genes per group."""
    groupby = str(args.get("groupby", "leiden"))
    method = str(args.get("method", "wilcoxon"))
    n_genes = int(args.get("n_genes", 50))

    adata = sc.read_h5ad(ckpt)
    sc.tl.rank_genes_groups(adata, groupby=groupby, method=method, use_raw=True)
    res = adata.uns["rank_genes_groups"]
    groups = list(res["names"].dtype.names)
    for grp in groups:
        rows = []
        for i in range(min(n_genes, len(res["names"][grp]))):
            rows.append({"group": grp, "gene": str(res["names"][grp][i]),
                         "log2fc": float(res["logfoldchanges"][grp][i]),
                         "pval_adj": float(res["pvals_adj"][grp][i]),
                         "score": float(res["scores"][grp][i])})
        _write_table(tables / f"de_{groupby}_{grp}.csv", rows, COLS)
        combined.extend(rows)
    _write_table(tables / f"de_{groupby}_all.csv", combined, COLS)
    return {"status": "ok", "n_groups": len(groups), "de_rows_by_group": rows_by_group}
'''

ENRICH_SOURCE = '''
def run_enrichment(args, ctx):
    """Over-representation analysis (ORA) on the top DE genes per group, offline against
    local .gmt gene-set files."""
    top_n_genes = int(args.get("top_n_genes", 100))
    background = int(args.get("background", 20000))   # human gene universe

    # reads tables/de_<groupby>_all.csv written by run_de
    for row in csv.DictReader(open(de_all)):
        if len(groups_genes[row["group"]]) < top_n_genes and float(row["pval_adj"]) < 0.05:
            groups_genes[row["group"]].append(row["gene"])

    enr = gp.enrich(gene_list=genes, gene_sets=gmt_paths, background=background)
    return {"status": "ok", "top_terms_by_group": ...}
'''

QC_SOURCE = '''
def run_scanpy_qc(args, ctx):
    """Per-cell QC metrics, cell/gene filtering, normalization, log1p, HVG selection."""
    min_genes = int(args.get("min_genes", 200))
    min_cells = int(args.get("min_cells", 3))
    max_pct_mt = float(args.get("max_pct_mt", 15.0))
    n_top_genes = int(args.get("n_top_genes", 2000))

    adata = _read_anndata(sc, path)
    adata.var["mt"] = adata.var_names.str.startswith(("MT-", "mt-"))
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], inplace=True, log1p=False)
    sc.pp.filter_cells(adata, min_genes=min_genes)
    sc.pp.filter_genes(adata, min_cells=min_cells)
    adata = adata[adata.obs["pct_counts_mt"] < max_pct_mt].copy()
    adata.layers["counts"] = adata.X.copy()
    sc.pp.normalize_total(adata, target_sum=1e4)
    sc.pp.log1p(adata)
    adata.raw = adata
    sc.pp.highly_variable_genes(adata, n_top_genes=min(n_top_genes, adata.n_vars))
    return {"status": "ok", "cells_after": int(adata.n_obs), "layers": ["counts"]}
'''

SCENARIOS = [
    {
        "name": "truncation_blocks_gsea",
        "expect_problem": True,
        "expect_param": {"n_genes"},
        "tool": "run_de",
        "source": RUN_DE_SOURCE,
        "defaults": [{"param": "groupby", "default": '"leiden"'},
                     {"param": "method", "default": '"wilcoxon"'},
                     {"param": "n_genes", "default": "50"}],
        "situation": (
            "You called run_de(groupby='leiden') on a dataset with 18,412 tested genes and 14 "
            "clusters. It returned status=ok, n_groups=14, de_rows_by_group={every cluster: 50}. "
            "Your NEXT planned step is a PRERANKED GSEA, which walks the complete ranked gene "
            "list per cluster and looks for gene sets that drift toward either end of that "
            "ranking. Is anything about the run_de step a problem for that next step?"
        ),
    },
    {
        "name": "background_overstates_the_universe",
        "expect_problem": True,
        "expect_param": {"background"},
        "tool": "run_enrichment",
        "source": ENRICH_SOURCE,
        "defaults": [{"param": "top_n_genes", "default": "100"},
                     {"param": "background", "default": "20000"}],
        "situation": (
            "You are running run_enrichment on a TARGETED panel dataset: after QC the object "
            "contains 1,150 genes in total — that is the complete set of genes that were "
            "measured and tested. The ORA p-values from this step will go into the report. Is "
            "anything about this step a problem?"
        ),
    },
    {
        "name": "control_sound_step",
        "expect_problem": False,
        "expect_param": set(),
        "tool": "run_scanpy_qc",
        "source": QC_SOURCE,
        "defaults": [{"param": "min_genes", "default": "200"},
                     {"param": "min_cells", "default": "3"},
                     {"param": "max_pct_mt", "default": "15.0"},
                     {"param": "n_top_genes", "default": "2000"}],
        "situation": (
            "You called run_scanpy_qc on a standard 10x scRNA-seq dataset of human PBMCs "
            "(8,214 cells, 20,113 genes, whole-transcriptome). It returned status=ok, "
            "cells_after=7,689. Your next step is Leiden clustering. Is anything about this "
            "step a problem?"
        ),
    },
]


def _chat(base_url: str, model: str, system: str, user: str, timeout: int = 300) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": 1200,
        # Qwen3 thinking eats a small budget before emitting content (see the free-text HPO
        # mapper post-mortem) — keep it off for a short structured answer.
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(f"{base_url.rstrip('/')}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["choices"][0]["message"]["content"] or ""


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--repeats", type=int, default=3, help="runs per scenario (temperature 0, "
                                                           "but tool-call sampling still varies)")
    a = ap.parse_args()

    total, passed = 0, 0
    for sc in SCENARIOS:
        for r in range(a.repeats):
            user = (
                f"{sc['situation']}\n\n"
                f"read_tool_source(tool={sc['tool']!r}) returned:\n\n"
                f"declared_description: {sc['tool']} — see docstring below\n"
                f"defaults (values nobody chose): {json.dumps(sc['defaults'])}\n\n"
                f"source:\n```python\n{sc['source'].strip()}\n```"
            )
            try:
                raw = _chat(a.base_url, a.model, SYSTEM, user)
            except Exception as exc:                       # noqa: BLE001
                print(f"[{sc['name']} #{r+1}] REQUEST FAILED: {type(exc).__name__}: {exc}")
                total += 1
                continue
            got = _parse(raw)
            total += 1
            problems = got.get("problems") or []
            params = {str(p.get("parameter", "")).strip().lower() for p in problems
                      if isinstance(p, dict)}
            if sc["expect_problem"]:
                # Naming the offending parameter ANYWHERE in the findings counts: a step can
                # have several defects, and we are measuring whether the real one is caught.
                ok = any(exp in " ".join(params) for exp in sc["expect_param"])
            else:
                ok = not problems
            passed += ok
            print(f"[{sc['name']} #{r+1}] {'PASS' if ok else 'FAIL'} "
                  f"n_problems={len(problems)} params={sorted(params)}")
            for pr in problems[:3]:
                if isinstance(pr, dict):
                    print(f"    - {pr.get('parameter')}: {str(pr.get('why',''))[:150]}")
            if not got:
                print(f"    (unparseable reply: {raw[:200]!r})")

    print(f"\n{passed}/{total} correct "
          f"({len([s for s in SCENARIOS if s['expect_problem']])} defect scenarios + "
          f"{len([s for s in SCENARIOS if not s['expect_problem']])} control, "
          f"x{a.repeats})")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
