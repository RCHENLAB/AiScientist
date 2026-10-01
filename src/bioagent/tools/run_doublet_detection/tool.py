"""The ``run_doublet_detection`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/scrna_advanced.py`` by ``scripts/refactor/split_tools.py``: the code is the
old module's text, verbatim, with only the imports rewritten. Helpers that several tools use live
in ``bioagent.tools._lib.scrna``.
"""

from __future__ import annotations

from typing import Any
from .._lib.scrna import (
    _INPUT_SPEC,
    _NO_COUNTS,
    _counts_matrix,
    _dirs,
    _import_scanpy,
    _missing,
    _p,
    _rel,
    _run_rel,
    _schema,
    _step_input,
    _write_table,
)
from ..sdk import HarnessTool


def run_doublet_detection(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Scrublet doublet scoring on the raw counts, BEFORE clustering.

    Two cells captured in one droplet express both parents' programmes, so they land between
    the parent clusters and read as a plausible "transitional"/"intermediate" population.
    Without this step nothing in the pipeline can tell that apart from real biology.

    Writes the score and call into obs and (by default) FILTERS the predicted doublets, since
    leaving them in is the failure mode this exists to prevent. Set ``filter: false`` to
    annotate only. Reports the rate — an implausible rate (say >20%) usually means the
    threshold, not the data, and is worth reporting rather than acting on silently.
    """
    try:
        sc = _import_scanpy()
    except ImportError as exc:
        return _missing(getattr(exc, "name", None) or "scanpy")

    work, art, figs, tables = _dirs(ctx)
    ckpt, err = _step_input(ctx, args, (work / "adata_qc.h5ad",),
                            "run_scanpy_qc must run first (adata_qc.h5ad missing)")
    if err:
        return {"status": "error", "step": "doublets", "error": err["error"]}

    do_filter = bool(args.get("filter", True))
    batch_key = str(args.get("batch_key", "")).strip() or None
    threshold = args.get("threshold")
    expected_rate = float(_p("run_doublet_detection", "expected_doublet_rate", args))

    adata = sc.read_h5ad(ckpt)
    counts = _counts_matrix(adata)
    if counts is None:
        return {"status": "error", "step": "doublets", "error": _NO_COUNTS}
    if batch_key and batch_key not in adata.obs:
        return {"status": "error", "step": "doublets",
                "error": f"batch_key '{batch_key}' not in obs; available: {list(adata.obs.columns)}"}

    # Scrublet simulates doublets from the observed counts, so it must see counts, not the
    # log-normalized .X this checkpoint carries.
    scored = adata.copy()
    scored.X = counts.copy()
    kwargs: dict[str, Any] = {"expected_doublet_rate": expected_rate}
    if threshold is not None:
        kwargs["threshold"] = float(threshold)
    if batch_key:
        kwargs["batch_key"] = batch_key          # per-batch simulation; rates differ by run
    try:
        sc.pp.scrublet(scored, **kwargs)
    except ImportError as exc:                   # scanpy's own optional dep for thresholding
        return _missing(getattr(exc, "name", None) or "scikit-image")
    except ValueError as exc:
        # scanpy raises ValueError (not ImportError) when scikit-image is absent and no
        # explicit threshold was given. Report the actionable form rather than the raw text.
        # The wording differs by version: 1.12 names "scikit-image", while 1.11.5 (the one in
        # analysis.sif) says "requires skimage, but skimage is not installed". Matching only the
        # first turned every doublet call on HPC3 into an opaque error.
        if "scikit-image" in str(exc) or "skimage" in str(exc):
            return {"status": "dependency_missing", "step": "doublets",
                    "dependency": "scikit-image",
                    "note": ("automatic threshold selection needs scikit-image "
                             "(`pip install scanpy[scrublet]`). Alternatively pass an explicit "
                             "`threshold`, but choose it from the score histogram — do not guess.")}
        return {"status": "error", "step": "doublets", "error": f"{type(exc).__name__}: {exc}"}

    adata.obs["doublet_score"] = scored.obs["doublet_score"].values
    adata.obs["predicted_doublet"] = scored.obs["predicted_doublet"].values
    n_before = int(adata.n_obs)
    n_doublets = int(scored.obs["predicted_doublet"].sum())
    rate = n_doublets / n_before if n_before else 0.0

    if do_filter and n_doublets:
        adata = adata[~adata.obs["predicted_doublet"].values].copy()
    # In place, on the file it read: adata_qc.h5ad (which later steps read), or the caller's own
    # `input` — never onto adata_qc.h5ad from some other file.
    adata.write(ckpt)

    _write_table(tables / "doublet_summary.csv", [{
        "cells_before": n_before, "predicted_doublets": n_doublets,
        "rate": round(rate, 4), "cells_after": int(adata.n_obs),
        "filtered": do_filter, "expected_doublet_rate": expected_rate,
    }], ["cells_before", "predicted_doublets", "rate", "cells_after", "filtered",
         "expected_doublet_rate"])

    warning = ""
    if rate > float(_p("run_doublet_detection", "flag_rate_above", args)):
        warning = (f"{rate:.0%} of cells called doublets — implausibly high for most protocols. "
                   "Inspect the score histogram before trusting this; the threshold is the "
                   "likelier problem than the data.")
    return {
        "status": "ok",
        "step": "doublets",
        "cells_before": n_before,
        "predicted_doublets": n_doublets,
        "doublet_rate": round(rate, 4),
        "cells_after": int(adata.n_obs),
        "filtered": do_filter,
        "threshold_source": "explicit" if threshold is not None else "scrublet_auto",
        "batch_key": batch_key or "",
        "warning": warning,
        "tables": [_rel(art, tables / "doublet_summary.csv")],
        "read_from": _run_rel(ctx, ckpt),
        "checkpoint": "adata_qc.h5ad" if ckpt == work / "adata_qc.h5ad" else _run_rel(ctx, ckpt),
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``run_doublet_detection`` record for the Scientist's catalog (see ``TOOL.md``)."""
    return HarnessTool(
        "run_doublet_detection",
        "Scrublet doublet scoring on raw counts, run AFTER run_scanpy_qc and BEFORE "
        "run_clustering. Two cells in one droplet express both parents' programmes and form "
        "an 'intermediate' cluster that reads as a novel transitional cell type — this is "
        "how a single-cell analysis invents a population. Filters predicted doublets by "
        "default (`filter: false` to annotate only) and returns the rate; a rate above ~20% "
        "usually means the threshold, not the biology.",
        _schema("run_doublet_detection",
                filter={"type": "boolean",
                        "description": "remove the predicted doublets (default) or only annotate them"},
                batch_key={"type": "string",
                           "description": "obs column to simulate doublets within, per batch"},
                threshold={"type": "number",
                           "description": "explicit score cutoff; omit to let scrublet choose one"},
                input={**_INPUT_SPEC, "description": (
                    _INPUT_SPEC["description"] + " Doublets are scored and filtered IN PLACE, "
                    "in that file.")}),
        run_doublet_detection,
        reads_private_data=True, category="qc", requires=("scanpy",),
    )
