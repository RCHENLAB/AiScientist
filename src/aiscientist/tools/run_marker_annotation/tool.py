"""The ``run_marker_annotation`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_advanced.py`` by ``scripts/refactor/split_tools.py``: the code is the
old module's text, verbatim, with only the imports rewritten. Helpers that several tools use live
in ``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

import json
from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _dirs,
    _import_scanpy,
    _missing,
    _rel,
    _run_rel,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


def run_marker_annotation(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Assign a cell type per cluster: signature score first pass, RAW expression decides.

    Previously a ``run_code`` template, i.e. code the model rewrote every run — the single most
    consequential step in the pipeline, and the least reproducible. As a tool the procedure is
    fixed and the judgement stays with the panel, which is where it belongs.

    Per-cell-type z-scoring inflates weak and ambient signal into confident-looking maxima, so
    the z-argmax is a FIRST PASS. The label is assigned only when that lineage's own
    discriminators are the dominant raw signal by ``dominance_ratio``; where the raw check
    disagrees with the first pass BOTH are reported; and a cluster with no dominant coherent
    signal stays ``Unassigned`` instead of being pushed into the nearest label.
    """
    try:
        sc = _import_scanpy()
        import numpy as np
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt, err = _step_input(ctx, args, (work / "adata_de.h5ad", work / "adata_clustered.h5ad"),
                            "run_clustering (and ideally run_de) must run first")
    if err:
        return {"status": "error", "step": "annotation", "error": err["error"]}

    panel = args.get("panel")
    if not isinstance(panel, dict) or not panel:
        return {"status": "error", "step": "annotation",
                "error": ("`panel` is required: {cell type: [marker symbols]} for THIS tissue. "
                          "There is no safe default — a panel from another tissue produces "
                          "confident wrong labels. Read the `annotate_clusters_by_markers_v2` "
                          "skill for how to build one and its discriminator table.")}
    discriminators = args.get("discriminators") or {}
    cluster_key = str(args.get("cluster_key", "leiden"))
    min_mean = float(args.get("min_discriminator_mean", 0.20))
    ratio = float(args.get("dominance_ratio", 1.5))

    adata = sc.read_h5ad(ckpt)
    if cluster_key not in adata.obs:
        return {"status": "error", "step": "annotation",
                "error": f"cluster_key '{cluster_key}' not in obs; available: {list(adata.obs.columns)}"}

    use_raw = adata.raw is not None
    present = set(adata.raw.var_names if use_raw else adata.var_names)
    panel = {ct: [g for g in gs if g in present] for ct, gs in panel.items()}
    absent = {ct: sorted(set(gs) - present)
              for ct, gs in (args["panel"] or {}).items() if set(gs) - present}
    not_testable = sorted(ct for ct, gs in panel.items() if not gs)
    panel = {ct: gs for ct, gs in panel.items() if gs}
    if not panel:
        return {"status": "error", "step": "annotation",
                "error": "no panel gene is present in the object — check the symbol nomenclature "
                         "(human HGNC vs mouse MGI) before anything else."}
    # No discriminators given: fall back to the panel itself, and SAY so — the disambiguation
    # is weaker without lineage-specific genes, and that changes how the labels should be read.
    disc = {ct: [g for g in (discriminators.get(ct) or panel[ct]) if g in present]
            for ct in panel}
    disc_note = "" if discriminators else (
        "no `discriminators` given, so the full panel was used for the raw check. Shared "
        "markers therefore discriminate less well; supply lineage-specific genes per type.")

    for ct, gs in panel.items():
        sc.tl.score_genes(adata, gs, score_name=f"score_{ct}", use_raw=use_raw)
    cols = [f"score_{ct}" for ct in panel]
    scores = adata.obs.groupby(cluster_key, observed=True)[cols].mean()
    scores.columns = [c[len("score_"):] for c in scores.columns]
    scores.to_csv(tables / "celltype_scores_by_cluster.csv")

    # A cell type whose score is constant across clusters (std 0, or a single cluster) carries
    # no information about which cluster is which; -inf keeps it out of the argmax instead of
    # leaving a NaN row, which pandas warns about and will eventually raise on.
    z = ((scores - scores.mean()) / scores.std().replace(0, np.nan)).fillna(float("-inf"))
    first_pass = z.idxmax(axis=1)

    disc_genes = sorted({g for gs in disc.values() for g in gs})
    raw_means = sc.get.obs_df(adata, keys=[*disc_genes, cluster_key], use_raw=use_raw) \
                  .groupby(cluster_key, observed=True)[disc_genes].mean()

    labels: dict[str, dict[str, Any]] = {}
    for cl in [str(c) for c in scores.index]:
        per_lineage = {ct: float(raw_means.loc[cl, gs].mean()) for ct, gs in disc.items() if gs}
        ranked = sorted(per_lineage.items(), key=lambda kv: kv[1], reverse=True)
        best, best_val = ranked[0] if ranked else ("", 0.0)
        runner, runner_val = ranked[1] if len(ranked) > 1 else ("", 0.0)
        dominant = best_val >= min_mean and (runner_val <= 0 or best_val >= ratio * runner_val)
        fp = str(first_pass.get(cl, "")) if cl in first_pass.index else ""
        labels[cl] = {
            "cell_type": best if dominant else "Unassigned",
            "first_pass_label": fp,
            "corrected_by_raw_check": bool(dominant and fp and fp != best),
            "n_cells": int((adata.obs[cluster_key].astype(str) == cl).sum()),
            "confidence": ("high" if dominant and best_val >= 2 * min_mean
                           else "medium" if dominant else "none"),
            "discriminator_mean": round(best_val, 4),
            "runner_up": runner, "runner_up_mean": round(runner_val, 4),
            "evidence": ", ".join(f"{g}={raw_means.loc[cl, g]:.2f}"
                                  for g in disc.get(best, [])[:3]) if dominant
                        else "no dominant lineage signal",
        }

    adata.obs["cell_type"] = adata.obs[cluster_key].map(
        lambda c: labels[str(c)]["cell_type"]).astype("category")
    adata.obs["cluster_note"] = adata.obs[cluster_key].map(lambda c: labels[str(c)]["evidence"])
    # h5py rejects '/' in obs keys (score_Pericyte/SMC) — sanitize before writing.
    adata.obs.columns = [c.replace("/", "_") for c in adata.obs.columns]
    adata.write(work / "adata_annotated.h5ad")

    comp = adata.obs["cell_type"].value_counts()
    total = int(comp.sum()) or 1
    _write_table(tables / "celltype_composition.csv",
                 [{"cell_type": str(k), "n_cells": int(v), "pct": round(100 * int(v) / total, 2)}
                  for k, v in comp.items()], ["cell_type", "n_cells", "pct"])
    _write_table(tables / "cluster_cell_types.csv",
                 [{"cluster": c, **{k: v for k, v in d.items()}} for c, d in sorted(labels.items())],
                 ["cluster", "cell_type", "first_pass_label", "corrected_by_raw_check",
                  "n_cells", "confidence", "discriminator_mean", "runner_up", "runner_up_mean",
                  "evidence"])
    (tables / "cluster_cell_types.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")

    corrected = sorted(c for c, v in labels.items() if v["corrected_by_raw_check"])
    unassigned = sorted(c for c, v in labels.items() if v["cell_type"] == "Unassigned")
    return {
        "status": "ok",
        "step": "annotation",
        "cluster_key": cluster_key,
        "n_clusters": len(labels),
        "labels": {c: v["cell_type"] for c, v in sorted(labels.items())},
        # These three belong in the write-up. An annotation whose corrections and unassigned
        # clusters are invisible cannot be reviewed by anyone.
        "corrected_by_raw_check": corrected,
        "unassigned_clusters": unassigned,
        "lineages_not_testable": not_testable,
        "panel_genes_absent_from_object": absent,
        "thresholds": {"min_discriminator_mean": min_mean, "dominance_ratio": ratio},
        "note": disc_note,
        "read_from": _run_rel(ctx, ckpt),
        "checkpoint": "adata_annotated.h5ad",
        "tables": [_rel(art, tables / "cluster_cell_types.csv"),
                   _rel(art, tables / "celltype_composition.csv"),
                   _rel(art, tables / "celltype_scores_by_cluster.csv")],
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_marker_annotation`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_marker_annotation",
        "Assign a cell type to each cluster from a curated marker `panel` "
        "({cell type: [symbols]}), with optional `discriminators` ({cell type: [2-4 "
        "lineage-SPECIFIC symbols]}). Signature scores give a first-pass z-argmax; the "
        "final label comes from RAW marker expression and is assigned only when that "
        "lineage's discriminators dominate, so shared markers (LAMP3 across AT2 and DC, "
        "SLC1A3 across Muller glia and astrocyte) cannot silently mislabel a cluster. "
        "Clusters with no dominant signal stay 'Unassigned'. `panel` is required and must "
        "match the tissue — see the annotate_clusters_by_markers_v2 skill for how to build "
        "it. Returns which clusters the raw check CORRECTED and which are unassigned; both "
        "belong in the report.",
        {"type": "object", "properties": {
            "panel": {"type": "object"}, "discriminators": {"type": "object"},
            "cluster_key": {"type": "string"},
            "min_discriminator_mean": {"type": "number"},
            "dominance_ratio": {"type": "number"},
            "input": _INPUT_SPEC}},
        run_marker_annotation,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
