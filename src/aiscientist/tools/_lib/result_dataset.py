"""The processed dataset a run hands on: one AnnData with everything the analysis added.

A run's checkpoints (``work/adata_qc.h5ad``, ``adata_clustered.h5ad``, ``adata_annotated.h5ad`` …)
were deleted once the report existed, and nothing copied them anywhere, so a run delivered a report
but no dataset the next analysis could start from (run f3b8268c4fd4: a 39 MB bundle of tables and
figures for a 16,750-cell library, no .h5ad). This builds that dataset before the checkpoints go:

- the BASE is the QC checkpoint: every gene that passed QC, ``X`` log-normalised, the integer
  counts in ``layers["counts"]`` (AMBIENT-CORRECTED when run_cellqc made the checkpoint; the QC tool
  says which in ``uns["counts_layer"]``, and the pointer repeats it). (A later checkpoint may hold only the highly variable genes in a
  scaled ``X``, which no next analysis can start from.)
- every later checkpoint adds what it computed: ``obs`` columns (clusters, cell types, scores,
  doublet calls) and ``obsm`` embeddings (PCA, UMAP), aligned by cell barcode, newest first.
- a cluster-level label table (``tables/cluster_cell_types.csv``) fills ``obs["cell_type"]`` when no
  checkpoint carries it.

It is written OUTSIDE ``artifacts/`` (``<workspace>/result/``): on HPC3 everything under artifacts
is copied back to the gateway after every step, and a multi-GB matrix has no business there. Into
``artifacts`` go the small things — ``tables/cells.csv`` (one row per cell) and
``data/result_dataset.json``, the pointer the console uses to offer "save to my data".

The next run of the same conversation starts from that file (:func:`adopt`): it is written back as
that run's own checkpoints, so every tool reads it exactly as it would read the QC and clustering
steps' output, and the run plans only what its request adds.

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
CLUSTERED_CHECKPOINT = "adata_clustered.h5ad"
POINTER = "result_dataset.json"
# CellQC's complete result/ directory, kept with the run by run_cellqc (which writes this pointer).
CELLQC_POINTER = "cellqc_result.json"
# obs columns that hold a clustering, and ones that hold cell-type labels, in the order tried.
CLUSTER_KEYS = ("leiden", "louvain", "cluster", "clusters", "seurat_clusters")
LABEL_KEYS = ("cell_type", "celltype", "final_label", "annotation", "majorclass")
# Stems that say nothing about the data: name the result after the folder holding them instead.
_GENERIC_STEMS = {"filtered_feature_bc_matrix", "raw_feature_bc_matrix", "matrix", "data",
                  "adata", "counts", "sample_filtered_feature_bc_matrix"}


_PROCESSED_TAIL = re.compile(r"\.processed\.[0-9]{8}-[0-9]{4}\.[A-Za-z0-9]+$")


def result_name(dataset_path: str, run_id: str, when: "_dt.datetime | None" = None) -> str:
    """``<input>.processed.<YYYYmmdd-HHMM>.<run_id>.h5ad`` — the data it came from, that it is
    processed, when, and by which run, readable in a plain directory listing."""
    return _named(dataset_path, run_id, when, "processed") + ".h5ad"


def cellqc_result_name(dataset_path: str, run_id: str, when: "_dt.datetime | None" = None) -> str:
    """``<input>.cellqc.<YYYYmmdd-HHMM>.<run_id>`` — the folder CellQC's result directory becomes
    when it is saved to the user's data."""
    return _named(dataset_path, run_id, when, "cellqc")


def _named(dataset_path: str, run_id: str, when: "_dt.datetime | None", what: str) -> str:
    p = Path(dataset_path or "dataset")
    stem = p.name
    for suffix in (".h5ad", ".h5", ".loom", ".gz", ".csv", ".tsv", ".txt"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    # A run that started from an earlier run's result names its own after the ORIGINAL input,
    # not "<input>.processed.<time>.<run>.processed.<time>.<run>".
    stem = _PROCESSED_TAIL.sub("", stem)
    if stem.lower() in _GENERIC_STEMS and p.parent.name:
        stem = p.parent.name
        if stem.lower() in {"outs", "filtered_feature_bc_matrix"} and p.parent.parent.name:
            stem = p.parent.parent.name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "dataset"
    stamp = (when or _dt.datetime.now()).strftime("%Y%m%d-%H%M")
    run = re.sub(r"[^A-Za-z0-9]+", "", run_id or "")[:32] or "run"
    return f"{stem[:80]}.{what}.{stamp}.{run}"


def _checkpoints(work: Path) -> "list[Path]":
    """The tools' checkpoints, plus any other ``work/adata_*.h5ad`` a step wrote: generic code names
    its own (run 74873590ca7b's ISG scoring wrote ``adata_scored_isg.h5ad``, and the processed
    dataset it handed on had no score). Each adds only the obs columns and embeddings the base lacks,
    aligned by barcode, and one with no cell in common is skipped."""
    found = [work / n for n in CHECKPOINTS if (work / n).is_file()]
    known = {p.name for p in found}
    found += [p for p in sorted(work.glob("adata_*.h5ad")) if p.is_file() and p.name not in known]
    rank = {n: i for i, n in enumerate(CHECKPOINTS)}
    return sorted(found, key=lambda p: (-p.stat().st_mtime, rank.get(p.name, len(CHECKPOINTS))))


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
    # A run that started from an earlier run's dataset says so: the earlier runs, oldest first.
    earlier = handed_on(adata)
    if earlier and earlier.get("run_id") and earlier.get("run_id") != provenance.get("run_id"):
        provenance["builds_on"] = [*[str(r) for r in (earlier.get("builds_on") or [])],
                                   str(earlier["run_id"])]
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
        "layers": sorted(adata.layers.keys()),
        "counts_layer": str(adata.uns.get("counts_layer") or "integer counts kept by the QC step"), "obs_columns": [str(c) for c in adata.obs.columns],
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


def handed_on(adata: Any) -> "dict[str, Any] | None":
    """The provenance record of a dataset an earlier run handed on (``uns["aiscientist"]`` written
    by :func:`export`), or None for any other AnnData."""
    try:
        rec = adata.uns.get("aiscientist")
    except Exception:  # noqa: BLE001 - an object without uns is simply not one of ours
        return None
    if not hasattr(rec, "get") or not rec.get("processed"):
        return None
    return {str(k): (v.tolist() if hasattr(v, "tolist") else v) for k, v in dict(rec).items()}


def already_processed(adata: Any) -> "str | None":
    """Why a QC tool must not run on ``adata``, or None. A dataset a run handed on is already
    filtered and log-normalised; QC on it would filter on log values and normalise a second time,
    with no symptom — every later number would simply be wrong."""
    rec = handed_on(adata)
    if rec is None:
        return None
    run = str(rec.get("run_id") or "an earlier run")
    return (f"this dataset was already quality-controlled and log-normalised by run {run} (its "
            "integer counts are in layers['counts']). Running QC again would filter on log values "
            f"and normalise twice. It is already in place as this run's checkpoint "
            f"({BASE_CHECKPOINT}): call the next tool directly.")


def adopt(workspace: "str | Path", source: "str | Path") -> "dict[str, Any]":
    """Start a run from the dataset an earlier run handed on, by writing it as this run's checkpoints.

    The tools find their input by a fixed name (``adata_qc.h5ad`` → ``adata_clustered.h5ad`` …), and
    a new run has none. The handed-on dataset is the QC checkpoint plus what the later steps added,
    so it goes back as ``work/adata_qc.h5ad`` with ``.raw`` restored — :func:`export` dropped it as
    a copy of ``X``, and the DE and annotation tools read the log-normalised matrix from there —
    and, when it carries a clustering, as ``work/adata_clustered.h5ad`` too. A checkpoint this run
    already has is never overwritten. ``status`` is ``skipped`` (with ``reason``) for a file no
    run handed on, which is then used as an ordinary input."""
    try:
        import anndata as ad
    except ImportError as exc:                       # pragma: no cover - the images ship it
        return {"status": "error", "error": f"cannot read the handed-on dataset: {exc}"}
    src = Path(source)
    if not src.is_file() or src.suffix.lower() != ".h5ad":
        return {"status": "skipped", "reason": f"{src.name} is not an .h5ad file a run handed on"}
    work = Path(workspace) / "work"
    if (work / BASE_CHECKPOINT).is_file():
        return {"status": "skipped", "reason": f"this run already has {BASE_CHECKPOINT}"}
    try:
        adata = ad.read_h5ad(src)
    except Exception as exc:  # noqa: BLE001 - reported, and the run goes on from the plain input
        return {"status": "error", "error": f"could not read {src.name}: {type(exc).__name__}: {exc}"}
    rec = handed_on(adata)
    if rec is None:
        return {"status": "skipped",
                "reason": f"{src.name} carries no record of the run that produced it"}

    adata.raw = adata                                  # the full log-normalised matrix, as QC left it
    work.mkdir(parents=True, exist_ok=True)
    written = [BASE_CHECKPOINT]
    tmp = work / ("." + BASE_CHECKPOINT + ".part")
    adata.write_h5ad(tmp)
    os.replace(tmp, work / BASE_CHECKPOINT)
    cluster_key = next((k for k in CLUSTER_KEYS if k in adata.obs.columns), None)
    label_key = next((k for k in LABEL_KEYS if k in adata.obs.columns), None)
    if cluster_key and not (work / CLUSTERED_CHECKPOINT).exists():
        # The same object under the name the DE and annotation tools look for first. A COPY, never
        # a link: run_clustering rewrites this name in place, which through a link would replace
        # the QC checkpoint as well.
        import shutil
        shutil.copyfile(work / BASE_CHECKPOINT, work / CLUSTERED_CHECKPOINT)
        written.append(CLUSTERED_CHECKPOINT)

    def levels(key: "str | None") -> "int | None":
        return int(adata.obs[key].astype(str).nunique()) if key else None

    adopted = {
        "from": src.name, "source_run": str(rec.get("run_id") or ""),
        "builds_on": [str(r) for r in (rec.get("builds_on") or [])],
        "checkpoints": written, "n_cells": int(adata.n_obs), "n_genes": int(adata.n_vars),
        "cluster_key": cluster_key, "n_clusters": levels(cluster_key),
        "label_key": label_key, "n_labels": levels(label_key),
        "obs_columns": [str(c) for c in adata.obs.columns],
        "obsm": sorted(str(k) for k in adata.obsm.keys()),
        "layers": sorted(str(k) for k in adata.layers.keys()),
        "counts_layer": str(adata.uns.get("counts_layer") or ""),
    }
    summary = (f"started from {src.name}: {adata.n_obs} cells x {adata.n_vars} genes"
               + (f", {adopted['n_clusters']} clusters ({cluster_key})" if cluster_key else "")
               + (f", {adopted['n_labels']} cell-type labels ({label_key})" if label_key else ""))
    return {"status": "ok", "adopted": adopted, "summary": summary}


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
