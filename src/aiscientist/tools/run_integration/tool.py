"""The ``run_integration`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_advanced.py`` by ``scripts/refactor/split_tools.py``: the code is the
old module's text, verbatim, with only the imports rewritten. Helpers that several tools use live
in ``aiscientist.tools._lib.scrna``.
"""

from __future__ import annotations

from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
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


def run_integration(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Correct sample/donor/batch effects before clustering, so clusters are cell types.

    Without this, a multi-sample object clusters by donor and every downstream cell-type label
    is really a donor label — with no visible symptom, because donor-driven clusters look just
    as clean as biology-driven ones.

    Prefers Harmony (fast, operates on the PCA embedding, the scRNA default) and falls back to
    ComBat, which ships inside scanpy and needs no extra dependency. Which one ran is REPORTED,
    not implied, because the two are not equivalent. Reports batch silhouette before and after:
    it should DROP (batches mixing). If it doesn't, integration did not work and the run should
    not proceed as though it did.
    """
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt, err = _step_input(ctx, args, (work / "adata_qc.h5ad",),
                            "run_scanpy_qc must run first (adata_qc.h5ad missing)")
    if err:
        return {"status": "error", "step": "integration", "error": err["error"]}

    batch_key = str(args.get("batch_key", "")).strip()
    if not batch_key:
        return {"status": "error", "step": "integration",
                "error": "batch_key is required — name the obs column holding sample/donor/batch"}
    method = str(args.get("method", "auto")).lower()
    n_pcs = int(args.get("n_pcs", 30))

    adata = sc.read_h5ad(ckpt)
    if batch_key not in adata.obs:
        return {"status": "error", "step": "integration",
                "error": f"batch_key '{batch_key}' not in obs; available: {list(adata.obs.columns)}"}
    batches = _obs_series(adata, batch_key)
    counts_by_batch = {str(k): int(v) for k, v in batches.value_counts().items()}
    if len(counts_by_batch) < 2:
        return {"status": "error", "step": "integration",
                "error": (f"'{batch_key}' has only {len(counts_by_batch)} level "
                          f"({list(counts_by_batch)}) — there is nothing to integrate. Skip this "
                          "step and cluster directly."),
                "batch_sizes": counts_by_batch}

    hvg = (adata.var["highly_variable"].values
           if "highly_variable" in adata.var.columns else None)
    work_ad = adata[:, hvg].copy() if hvg is not None and bool(hvg.any()) else adata.copy()

    def _silhouette(emb: Any) -> float | None:
        """How separated the BATCHES are in the embedding. Lower = better mixed."""
        try:
            import numpy as np
            from sklearn.metrics import silhouette_score
            n = emb.shape[0]
            idx = np.arange(n)
            if n > 5000:                          # silhouette is O(n^2); subsample honestly
                idx = np.random.default_rng(0).choice(n, 5000, replace=False)
            return float(silhouette_score(emb[idx], batches.values[idx]))
        except Exception:                          # noqa: BLE001 - a diagnostic, never fatal
            return None

    sc.pp.scale(work_ad, max_value=10)
    sc.tl.pca(work_ad, n_comps=min(n_pcs + 20, min(work_ad.shape) - 1),
              svd_solver="arpack", random_state=0)
    before = _silhouette(work_ad.obsm["X_pca"][:, :n_pcs])

    method_used, note = "", ""
    if method in ("auto", "harmony"):
        try:
            import scanpy.external as sce
            sce.pp.harmony_integrate(work_ad, batch_key, random_state=0)
            adata.obsm["X_integrated"] = work_ad.obsm["X_pca_harmony"][:, :n_pcs]
            method_used = "harmony"
        except Exception as exc:                   # noqa: BLE001 - fall through to ComBat
            if method == "harmony":
                return {"status": "error", "step": "integration",
                        "error": (f"harmony was requested but is unavailable "
                                  f"({type(exc).__name__}: {exc}). Install harmonypy, or use "
                                  "method='combat' (bundled with scanpy).")}
            note = (f"harmonypy unavailable ({type(exc).__name__}), used ComBat instead. "
                    "ComBat is a linear location/scale correction — for strong donor effects "
                    "Harmony or scVI is the better tool; install harmonypy to get it.")
    if not method_used:
        cb = work_ad.copy()
        cb.X = cb.raw.to_adata()[:, cb.var_names].X.copy() if cb.raw is not None else cb.X
        sc.pp.combat(cb, key=batch_key)
        sc.pp.scale(cb, max_value=10)
        sc.tl.pca(cb, n_comps=min(n_pcs, min(cb.shape) - 1), svd_solver="arpack", random_state=0)
        adata.obsm["X_integrated"] = cb.obsm["X_pca"]
        method_used = "combat"

    after = _silhouette(adata.obsm["X_integrated"])
    adata.uns["integration"] = {"method": method_used, "batch_key": batch_key}
    adata.write(work / "adata_integrated.h5ad")

    rows = [{"batch": b, "n_cells": n} for b, n in sorted(counts_by_batch.items())]
    _write_table(tables / "integration_batches.csv", rows, ["batch", "n_cells"])

    warning = ""
    if before is not None and after is not None and after >= before:
        warning = (f"batch silhouette did NOT improve ({before:.3f} → {after:.3f}). The batches "
                   "are not mixed; treat any cell-type label from this embedding as suspect.")
    return {
        "status": "ok",
        "step": "integration",
        "method_used": method_used,               # which one ACTUALLY ran, not which was asked for
        "batch_key": batch_key,
        "n_batches": len(counts_by_batch),
        "batch_sizes": counts_by_batch,
        "batch_silhouette_before": None if before is None else round(before, 4),
        "batch_silhouette_after": None if after is None else round(after, 4),
        "note": note,
        "warning": warning,
        "embedding": "X_integrated",
        "read_from": _run_rel(ctx, ckpt),
        "checkpoint": "adata_integrated.h5ad",
        "tables": [_rel(art, tables / "integration_batches.csv")],
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_integration`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_integration",
        "Correct sample/donor/batch effects before clustering. REQUIRED whenever the object "
        "holds more than one sample: without it the cells cluster by donor and every "
        "cell-type label downstream is really a donor label, with no visible symptom. "
        "`batch_key` names the obs column. Uses Harmony when harmonypy is installed and "
        "falls back to ComBat (bundled with scanpy); the method that ACTUALLY ran is "
        "returned as `method_used`. Writes adata_integrated.h5ad and reports batch "
        "silhouette before/after — it must DROP, and a warning is returned if it does not.",
        {"type": "object", "properties": {
            "batch_key": {"type": "string"},
            "method": {"type": "string", "enum": ["auto", "harmony", "combat"]},
            "n_pcs": {"type": "integer"},
            "input": _INPUT_SPEC}},
        run_integration,
        reads_private_data=True, category="analysis", requires=("scanpy",),
    )
