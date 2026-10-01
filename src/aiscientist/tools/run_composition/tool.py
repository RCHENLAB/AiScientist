"""The ``run_composition`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_advanced.py`` by ``scripts/refactor/split_tools.py``: the code is the
old module's text, verbatim, with only the imports rewritten. Helpers that several tools use live
in ``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

import math
from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _bh_fdr,
    _dirs,
    _import_scanpy,
    _missing,
    _obs_series,
    _rel,
    _run_rel,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


def run_composition(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Cell-type proportions per sample, and how they shift between conditions.

    "Which populations expand or shrink in disease" is one of the most common questions asked
    of this data and had no tool. Proportions are COMPOSITIONAL — they must sum to 1, so one
    population growing mechanically shrinks every other. The test therefore runs on
    centered-log-ratio values, and the caveat is returned with the result rather than left for
    the reader to remember.
    """
    try:
        sc = _import_scanpy()
        import numpy as np
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    # `adata_qc.h5ad` is a valid starting point, not a fallback. Clustering is required only when
    # the grouping COMES from it; a dataset that arrives with its own cell-type labels is analysed
    # straight off QC, which is precisely what the condition-comparison protocol tells the planner
    # to do ("REUSE it — do NOT re-cluster"). `run_de` had this same defect and was fixed; this
    # tool was missed, so composition — the analysis a labelled two-arm dataset needs FIRST, and
    # the one whose absence let a 3x shift in Cone abundance pass as differential expression —
    # refused to run on exactly the datasets it exists for, and demanded the re-clustering the
    # protocol forbids.
    ckpt, err = _step_input(
        ctx, args, tuple(work / n for n in ("adata_annotated.h5ad", "adata_de.h5ad",
                                            "adata_clustered.h5ad", "adata_qc.h5ad")),
        "no analysis checkpoint found — run run_scanpy_qc first (it writes adata_qc.h5ad). "
        "run_clustering is needed only when the cell-type grouping has to be derived; a dataset "
        "that already carries labels needs QC only.")
    if err:
        return {"status": "error", "step": "composition", "error": err["error"]}

    adata = sc.read_h5ad(ckpt)
    group_key = str(args.get("group_key", "")).strip() or (
        "cell_type" if "cell_type" in adata.obs else "leiden")
    sample_key = str(args.get("sample_key", "")).strip()
    condition_key = str(args.get("condition_key", "")).strip()
    for key in [k for k in (group_key, sample_key, condition_key) if k]:
        if key not in adata.obs:
            return {"status": "error", "step": "composition",
                    "error": f"'{key}' not in obs; available: {list(adata.obs.columns)}"}

    groups = _obs_series(adata, group_key).values
    labels = sorted(set(groups))
    if not sample_key:
        # No replicate structure: describe, do not test.
        counts = {g: int((groups == g).sum()) for g in labels}
        total = sum(counts.values()) or 1
        rows = [{"group": g, "n_cells": counts[g], "pct": round(100 * counts[g] / total, 2)}
                for g in labels]
        _write_table(tables / "composition.csv", rows, ["group", "n_cells", "pct"])
        return {"status": "ok", "step": "composition", "group_key": group_key,
                "tested": False, "composition": rows,
                "note": ("no sample_key given, so this is a description of one pooled object. "
                         "Comparing conditions needs per-sample proportions."),
                "tables": [_rel(art, tables / "composition.csv")],
                "read_from": _run_rel(ctx, ckpt), "raw_data_to_llm": False}

    samples = _obs_series(adata, sample_key).values
    rows: list[dict[str, Any]] = []
    prop: dict[str, dict[str, float]] = {}
    for s in sorted(set(samples)):
        sel = samples == s
        n = int(sel.sum())
        prop[s] = {}
        for g in labels:
            k = int(((groups == g) & sel).sum())
            prop[s][g] = k / n if n else 0.0
            rows.append({"sample": s, "group": g, "n_cells": k,
                         "pct": round(100 * prop[s][g], 3)})
    _write_table(tables / "composition_by_sample.csv", rows,
                 ["sample", "group", "n_cells", "pct"])

    result: dict[str, Any] = {
        "status": "ok", "step": "composition", "group_key": group_key,
        "sample_key": sample_key, "n_samples": len(prop), "tested": False,
        "tables": [_rel(art, tables / "composition_by_sample.csv")],
        "read_from": _run_rel(ctx, ckpt),
        "raw_data_to_llm": False,
    }
    if not condition_key:
        result["note"] = "per-sample proportions only; pass condition_key to contrast arms."
        return result

    conditions = _obs_series(adata, condition_key).values
    # Proportions PER ARM, computed straight off the condition column and always reported — the
    # question this tool exists to answer is "does this population shift between the arms", and
    # that number must survive the study being untestable. Deriving it from the sample->condition
    # map instead silently collapses when a study has one library: `orig.ident` held a single
    # value, every cell mapped to one arm, and the tool returned the pooled composition of the
    # whole object. The 3x depletion of Cone cells between DDX41 and WT — the shift that made the
    # original run's pooled DE uninterpretable — was simply absent from the output.
    arm_names = sorted(set(str(c) for c in conditions))
    arm_rows: list[dict[str, Any]] = []
    arm_pct: dict[str, dict[str, float]] = {}
    arm_cells: dict[str, dict[str, int]] = {g: {} for g in labels}
    for a in arm_names:
        sel_a = conditions == a
        n_a = int(sel_a.sum())
        arm_pct[a] = {}
        for g in labels:
            k = int(((groups == g) & sel_a).sum())
            arm_pct[a][g] = round(100 * k / n_a, 3) if n_a else 0.0
            arm_cells[g][a] = k
            arm_rows.append({"condition": a, "group": g, "n_cells": k, "pct": arm_pct[a][g]})
    _write_table(tables / "composition_by_condition.csv", arm_rows,
                 ["condition", "group", "n_cells", "pct"])
    result["condition_key"] = condition_key
    result["pct_by_condition"] = arm_pct
    # The counts behind those percentages — what a write-up quotes as "n cells", and what the
    # Critic's count check (agents/step_numbers.py) holds a quoted count to.
    result["cells_by_group_and_arm"] = arm_cells
    result["tables"].append(_rel(art, tables / "composition_by_condition.csv"))
    if len(arm_names) == 2:
        a, b = arm_names
        result["largest_shifts"] = sorted(
            ({"group": g, f"pct_{a}": arm_pct[a][g], f"pct_{b}": arm_pct[b][g],
              "fold": round((arm_pct[b][g] + 1e-9) / (arm_pct[a][g] + 1e-9), 2)} for g in labels),
            key=lambda r: abs(math.log((r["fold"] or 1e-9))), reverse=True)[:6]

    cond = {s: c for s, c in zip(samples, conditions)}
    arms: dict[str, list[str]] = {}
    for s in prop:
        arms.setdefault(cond[s], []).append(s)
    if len(arms) < 2 or any(len(v) < 2 for v in arms.values()):
        one_library = len(set(samples)) == 1
        result["note"] = (
            (f"NOT TESTED — '{sample_key}' takes one value across the whole object, so this study "
             f"has one library and no biological replication; no p-value for '{condition_key}' "
             "exists. " if one_library else
             f"not tested: each arm needs >=2 samples, has {{{', '.join(f'{k}: {len(v)}' for k, v in arms.items())}}}. ")
            + "The per-arm proportions in `pct_by_condition` ARE the descriptive finding and "
              "should be reported — a large shift there means a pooled differential-expression "
              "result cannot be read as expression change. A difference between single samples is "
              "not evidence of an effect, so state it as a description, not a test.")
        return result

    # CLR: proportions are constrained to sum to 1, so testing them raw makes every population
    # look coupled to every other. CLR removes the constraint before the test.
    def _clr(p: dict[str, float]) -> dict[str, float]:
        vals = {g: max(v, 1e-6) for g, v in p.items()}
        gmean = math.exp(sum(math.log(v) for v in vals.values()) / len(vals))
        return {g: math.log(v / gmean) for g, v in vals.items()}

    clr = {s: _clr(p) for s, p in prop.items()}
    from scipy import stats as sstats
    (a_name, a_s), (b_name, b_s) = sorted(arms.items())[:2]
    tests, pvals = [], []
    for g in labels:
        A = [clr[s][g] for s in a_s]
        B = [clr[s][g] for s in b_s]
        t, p = sstats.ttest_ind(B, A, equal_var=False)
        p = 1.0 if (p is None or (isinstance(p, float) and math.isnan(p))) else float(p)
        pvals.append(p)
        tests.append({"group": g,
                      f"mean_pct_{a_name}": round(100 * sum(prop[s][g] for s in a_s) / len(a_s), 3),
                      f"mean_pct_{b_name}": round(100 * sum(prop[s][g] for s in b_s) / len(b_s), 3),
                      "clr_diff": round(sum(B) / len(B) - sum(A) / len(A), 4), "pval": p})
    for row, q in zip(tests, _bh_fdr(pvals)):
        row["pval_adj"] = round(q, 5)
    tests.sort(key=lambda r: r["pval"])
    _write_table(tables / "composition_test.csv", tests,
                 ["group", f"mean_pct_{a_name}", f"mean_pct_{b_name}", "clr_diff",
                  "pval", "pval_adj"])
    result.update({
        "tested": True,
        "contrast": f"{b_name} vs {a_name}",
        "n_samples_per_arm": {a_name: len(a_s), b_name: len(b_s)},
        "method": "per-sample proportions -> centered log-ratio -> Welch t-test -> BH FDR",
        "results": tests,
        "n_significant": sum(1 for r in tests if r["pval_adj"] < 0.05),
        "caveat": ("Proportions are compositional: they sum to 1, so a genuine expansion of one "
                   "population forces every other proportion down. A significant DECREASE is "
                   "not by itself evidence that that population lost cells."),
        "tables": result["tables"] + [_rel(art, tables / "composition_test.csv")],
    })
    return result


def make_tool() -> HarnessTool:
    """The ``run_composition`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_composition",
        "Cell-type proportions per sample and how they shift between conditions — 'which "
        "populations expand or shrink'. With `sample_key` + `condition_key` it tests on "
        "centered-log-ratio values (proportions sum to 1, so testing them raw makes every "
        "population look coupled) and needs >=2 samples per arm, otherwise it reports "
        "proportions without a test. Returns the compositional caveat with the result.",
        {"type": "object", "properties": {
            "group_key": {"type": "string"}, "sample_key": {"type": "string"},
            "condition_key": {"type": "string"}, "input": _INPUT_SPEC}},
        run_composition,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
