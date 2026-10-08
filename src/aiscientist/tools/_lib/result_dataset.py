"""The processed dataset a run hands on: one AnnData with everything the analysis added.

A run's checkpoints (``work/adata_qc.h5ad``, ``adata_clustered.h5ad``, ``adata_annotated.h5ad`` …)
were deleted once the report existed, and nothing copied them anywhere, so a run delivered a report
but no dataset the next analysis could start from (run f3b8268c4fd4: a 39 MB bundle of tables and
figures for a 16,750-cell library, no .h5ad). This builds that dataset before the checkpoints go:

- the BASE is the QC checkpoint: every gene that passed QC, ``X`` log-normalised, the raw integer
  counts in ``layers["counts"]``. (A later checkpoint may hold only the highly variable genes in a
  scaled ``X``, which no next analysis can start from.)
- every later checkpoint adds what it computed: ``obs`` columns (clusters, cell types, scores,
  doublet calls) and ``obsm`` embeddings (PCA, UMAP), aligned by cell barcode, newest first.
- a cluster-level label table (``tables/cluster_cell_types.csv``) fills ``obs["cell_type"]`` when no
  checkpoint carries it.

It is written OUTSIDE ``artifacts/`` (``<workspace>/result/``): on HPC3 everything under artifacts
is copied back to the gateway after every step, and a multi-GB matrix has no business there. Into
``artifacts`` go the small things — ``tables/cells.csv`` (one row per cell) and
``data/result_dataset.json``, the pointer the console uses to offer "save to my data".

Pure data handling; no model, no network. Runs wherever the checkpoints are (the HPC3 analysis job
through ``scrna_cli``, or in-process for a local run).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path
from typing import Any

# Checkpoints the scRNA line writes. Order only breaks ties between equal modification times.
CHECKPOINTS = ("adata_annotated.h5ad", "adata_de.h5ad", "adata_integrated.h5ad",
               "adata_scored.h5ad", "adata_clustered.h5ad", "adata_qc.h5ad")
BASE_CHECKPOINT = "adata_qc.h5ad"
POINTER = "result_dataset.json"
# Stems that say nothing about the data: name the result after the folder holding them instead.
_GENERIC_STEMS = {"filtered_feature_bc_matrix", "raw_feature_bc_matrix", "matrix", "data",
                  "adata", "counts", "sample_filtered_feature_bc_matrix"}


def result_name(dataset_path: str, run_id: str, when: "_dt.datetime | None" = None) -> str:
    """``<input>.processed.<YYYYmmdd-HHMM>.<run_id>.h5ad`` — the data it came from, that it is
    processed, when, and by which run, readable in a plain directory listing."""
    p = Path(dataset_path or "dataset")
    stem = p.name
    for suffix in (".h5ad", ".h5", ".loom", ".gz", ".csv", ".tsv", ".txt"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    if stem.lower() in _GENERIC_STEMS and p.parent.name:
        stem = p.parent.name
        if stem.lower() in {"outs", "filtered_feature_bc_matrix"} and p.parent.parent.name:
            stem = p.parent.parent.name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "dataset"
    stamp = (when or _dt.datetime.now()).strftime("%Y%m%d-%H%M")
    run = re.sub(r"[^A-Za-z0-9]+", "", run_id or "")[:32] or "run"
    return f"{stem[:80]}.processed.{stamp}.{run}.h5ad"


def _checkpoints(work: Path) -> "list[Path]":
    found = [work / n for n in CHECKPOINTS if (work / n).is_file()]
    rank = {n: i for i, n in enumerate(CHECKPOINTS)}
    return sorted(found, key=lambda p: (-p.stat().st_mtime, rank[p.name]))


def _plain(value: Any) -> Any:
    """A JSON- and h5ad-safe copy of provenance metadata: no numpy scalars, and no None (an h5ad
    cannot store one), dropped from dicts and lists alike."""
    def strip(v: Any) -> Any:
        if isinstance(v, dict):
            return {str(k): strip(x) for k, x in v.items() if x is not None}
        if isinstance(v, list):
            return [strip(x) for x in v if x is not None]
        return v
    return strip(json.loads(json.dumps(value, default=str)))


def export(workspace: "str | Path", name: str, meta: "dict[str, Any] | None" = None) -> "dict[str, Any]":
    """Build ``<workspace>/result/<name>`` from the run's checkpoints, plus ``artifacts/tables/
    cells.csv`` and ``artifacts/data/result_dataset.json``. Returns the tool-style result dict;
    ``status`` is ``skipped`` (with ``reason``) when there is nothing to export."""
    try:
        import anndata as ad
        import numpy as np
        import pandas as pd
        import scipy.sparse as sp
    except ImportError as exc:                       # pragma: no cover - the images ship these
        return {"status": "error", "error": f"cannot export the result dataset: {exc}"}

    ws = Path(workspace)
    work, art = ws / "work", ws / "artifacts"
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.h5ad", name or ""):
        return {"status": "error", "error": f"bad result file name {name!r}"}
    found = _checkpoints(work)
    if not found:
        return {"status": "skipped",
                "reason": "the run wrote no analysis checkpoint (work/adata_*.h5ad) to export"}

    base_path = work / BASE_CHECKPOINT if (work / BASE_CHECKPOINT).is_file() else found[-1]
    adata = ad.read_h5ad(base_path)
    notes: list[str] = []
    if base_path.name != BASE_CHECKPOINT:
        notes.append(f"no {BASE_CHECKPOINT}; built on {base_path.name}")
    # A base that is an HVG subset with the full matrix in .raw (a clustered checkpoint) hands on
    # the full matrix; otherwise .raw only duplicates X and is dropped.
    if adata.raw is not None:
        if adata.raw.n_vars > adata.n_vars:
            full = adata.raw.to_adata()
            full.obs = adata.obs.copy()
            full.obsm = adata.obsm.copy()
            adata = full
            notes.append("X taken from .raw (all genes)")
        adata.raw = None
    if not sp.issparse(adata.X):
        adata.X = sp.csr_matrix(adata.X)
    for key in list(adata.layers):
        if not sp.issparse(adata.layers[key]):
            adata.layers[key] = sp.csr_matrix(adata.layers[key])
    adata.obs_names = adata.obs_names.astype(str)

    used = [base_path.name]
    for path in found:                                    # newest first: the latest value wins
        if path == base_path:
            continue
        try:
            other = ad.read_h5ad(path, backed="r")
        except Exception as exc:  # noqa: BLE001 - one unreadable checkpoint must not lose the rest
            notes.append(f"{path.name} unreadable: {type(exc).__name__}")
            continue
        try:
            names = pd.Index(other.obs_names.astype(str))
            shared = adata.obs_names.intersection(names)
            if len(shared) == 0:
                notes.append(f"{path.name}: no cells in common, skipped")
                continue
            added: list[str] = []
            for col in other.obs.columns:
                if col not in adata.obs.columns:
                    adata.obs[col] = other.obs[col].reindex(adata.obs_names)
                    added.append(col)
            pos = names.get_indexer(adata.obs_names)
            for key in other.obsm.keys():
                if key in adata.obsm:
                    continue
                arr = np.asarray(other.obsm[key])
                if arr.ndim != 2:
                    continue
                full = np.full((adata.n_obs, arr.shape[1]), np.nan, dtype=np.float32)
                ok = pos >= 0
                full[ok] = arr[pos[ok]]
                adata.obsm[key] = full
                added.append(f"obsm[{key}]")
            if added:
                used.append(path.name)
        finally:
            if getattr(other, "file", None) is not None:
                other.file.close()

    if "cell_type" not in adata.obs.columns:
        filled = _labels_from_table(adata, art / "tables" / "cluster_cell_types.csv", pd)
        if filled:
            notes.append(f"cell_type from tables/cluster_cell_types.csv ({filled})")

    # Aligning by barcode leaves NaN in a text column for cells a later checkpoint lacks; h5ad
    # refuses a column that mixes strings and floats.
    for col in adata.obs.columns:
        if adata.obs[col].dtype == object:
            adata.obs[col] = adata.obs[col].astype(object).where(adata.obs[col].notna(), "NA").astype(str)

    created = _dt.datetime.now(_dt.timezone.utc)
    provenance = {"processed": True, "created_at": created.isoformat(timespec="seconds"),
                  "checkpoints": used, "notes": notes, **(meta or {})}
    adata.uns["aiscientist"] = _plain(provenance)

    out = ws / "result" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name("." + out.name + ".part")
    adata.write_h5ad(tmp, compression="gzip")
    os.replace(tmp, out)
    size = out.stat().st_size

    # One row per cell, for anyone who wants labels and coordinates without opening an h5ad.
    cells = adata.obs.copy()
    cells.insert(0, "barcode", adata.obs_names)
    for key, prefix in (("X_umap", "UMAP"), ("X_tsne", "TSNE")):
        if key in adata.obsm and np.asarray(adata.obsm[key]).shape[1] >= 2:
            emb = np.asarray(adata.obsm[key])
            cells[f"{prefix}_1"], cells[f"{prefix}_2"] = emb[:, 0], emb[:, 1]
    (art / "tables").mkdir(parents=True, exist_ok=True)
    cells.to_csv(art / "tables" / "cells.csv", index=False)

    pointer = {
        "name": name, "path": str(out), "size_bytes": size,
        "n_cells": int(adata.n_obs), "n_genes": int(adata.n_vars),
        "X": "log-normalised expression, all genes that passed QC (sparse)",
        "layers": sorted(adata.layers.keys()), "obs_columns": [str(c) for c in adata.obs.columns],
        "obsm": sorted(adata.obsm.keys()), "checkpoints": used, "notes": notes,
        "created_at": provenance["created_at"], "saved_to": None,
        "cells_table": "tables/cells.csv", **_plain(meta or {}),
    }
    (art / "data").mkdir(parents=True, exist_ok=True)
    (art / "data" / POINTER).write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    # The same record travels with the file when it is moved to the user's data.
    (out.parent / (out.name + ".json")).write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    return {"status": "ok", "result_dataset": pointer,
            "summary": (f"processed dataset {name}: {adata.n_obs} cells x {adata.n_vars} genes, "
                        f"{size / 1e6:.0f} MB; obs: {', '.join(map(str, adata.obs.columns[:12]))}")}


def _labels_from_table(adata: Any, table: Path, pd: Any) -> str:
    """Map a cluster -> label table onto ``obs["cell_type"]``. Returns what was used, or ''."""
    if not table.is_file():
        return ""
    try:
        df = pd.read_csv(table)
    except Exception:  # noqa: BLE001
        return ""
    label_col = next((c for c in ("cell_type", "label", "final_label") if c in df.columns), None)
    cluster_col = next((c for c in ("cluster", "group", "leiden") if c in df.columns), None)
    obs_key = next((c for c in ("leiden", "louvain", "cluster") if c in adata.obs.columns), None)
    if not (label_col and cluster_col and obs_key):
        return ""
    mapping = dict(zip(df[cluster_col].astype(str), df[label_col].astype(str)))
    adata.obs["cell_type"] = adata.obs[obs_key].astype(str).map(mapping).fillna("Unassigned")
    return f"{obs_key} -> {label_col}"
