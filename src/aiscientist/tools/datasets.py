from __future__ import annotations

import csv
import json
import math
import re
import urllib.request
from pathlib import Path
from statistics import mean
from typing import Any


PBMC3K_URL = "https://falexwolf.de/data/pbmc3k_raw.h5ad"
MAX_PUBLIC_DATASET_BYTES = 500 * 1024 * 1024


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def numeric_columns(rows: list[dict[str, str]]) -> list[str]:
    if not rows:
        return []
    columns: list[str] = []
    for key in rows[0]:
        try:
            [float(row[key]) for row in rows if row.get(key, "") != ""]
        except ValueError:
            continue
        columns.append(key)
    return columns


def infer_dataset_kind(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "empty"
    headers = {key.lower() for key in rows[0]}
    if "cell_id" in headers or "cell_type" in headers:
        return "single_cell_counts"
    if "spot_id" in headers or "region" in headers:
        return "spatial_expression"
    return "tabular_expression"


def group_mean(rows: list[dict[str, str]], group_col: str, value_col: str) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row[group_col], []).append(float(row[value_col]))
    return {key: mean(values) for key, values in grouped.items()}


def run_single_cell_smoke(rows: list[dict[str, str]], genes: list[str]) -> dict[str, Any]:
    qc_rows: list[dict[str, Any]] = []
    mito_genes = [gene for gene in genes if gene.upper().startswith("MT-")]
    for row in rows:
        counts = [float(row[gene]) for gene in genes]
        total = sum(counts)
        mito_total = sum(float(row[gene]) for gene in mito_genes)
        qc_rows.append(
            {
                "cell_id": row.get("cell_id", ""),
                "condition": row.get("condition", "unknown"),
                "cell_type": row.get("cell_type", "unknown"),
                "total_counts": total,
                "genes_detected": sum(1 for value in counts if value > 0),
                "mito_fraction": mito_total / total if total else 0.0,
            }
        )

    condition_effects = {}
    if "condition" in rows[0]:
        conditions = sorted({row["condition"] for row in rows})
        if len(conditions) >= 2:
            baseline, comparison = conditions[0], conditions[1]
            for gene in genes:
                means = group_mean(rows, "condition", gene)
                condition_effects[gene] = {
                    "baseline": baseline,
                    "comparison": comparison,
                    "baseline_mean": means[baseline],
                    "comparison_mean": means[comparison],
                    "log2_fold_change": math.log2((means[comparison] + 1) / (means[baseline] + 1)),
                }

    return {
        "dataset_kind": "single_cell_counts",
        "cells": len(rows),
        "genes": len(genes),
        "mean_total_counts": mean(row["total_counts"] for row in qc_rows) if qc_rows else 0,
        "mean_mito_fraction": mean(row["mito_fraction"] for row in qc_rows) if qc_rows else 0,
        "qc_preview": qc_rows[:5],
        "condition_effects": condition_effects,
    }


def run_spatial_smoke(rows: list[dict[str, str]], genes: list[str]) -> dict[str, Any]:
    region_summary = {}
    if "region" in rows[0]:
        regions = sorted({row["region"] for row in rows})
        for region in regions:
            region_rows = [row for row in rows if row["region"] == region]
            region_summary[region] = {
                gene: mean(float(row[gene]) for row in region_rows)
                for gene in genes
            }

    condition_effects = {}
    if "condition" in rows[0]:
        conditions = sorted({row["condition"] for row in rows})
        if len(conditions) >= 2:
            baseline, comparison = conditions[0], conditions[1]
            for gene in genes:
                means = group_mean(rows, "condition", gene)
                condition_effects[gene] = {
                    "baseline": baseline,
                    "comparison": comparison,
                    "baseline_mean": means[baseline],
                    "comparison_mean": means[comparison],
                    "log2_fold_change": math.log2((means[comparison] + 1) / (means[baseline] + 1)),
                }

    return {
        "dataset_kind": "spatial_expression",
        "spots": len(rows),
        "genes": len(genes),
        "regions": len(region_summary),
        "region_summary": region_summary,
        "condition_effects": condition_effects,
    }


def run_dataset_smoke_analysis(dataset_path: Path, output_dir: Path) -> dict[str, Any]:
    if dataset_path.suffix.lower() == ".h5ad":
        return run_h5ad_preflight(dataset_path, output_dir)

    # A VCF is variant calls, not an expression matrix — recognise it here so the planner routes it
    # to the variant-annotation workflow (Ensembl VEP + ClinVar) instead of the scanpy line. Uses the
    # SAME single-file/folder upload UI; only the preflight labelling differs.
    name = dataset_path.name.lower()
    if name.endswith(".vcf") or name.endswith(".vcf.gz"):
        return run_vcf_preflight(dataset_path, output_dir)

    # Non-CSV/H5AD formats (10x .h5, .loom, mtx dir, ...) are read directly by the
    # scanpy analysis tools; preflight only does the text-table smoke path, so for
    # those formats just record the path and let the analysis line handle it.
    if dataset_path.suffix.lower() not in {".csv", ".tsv", ".txt"} or dataset_path.is_dir():
        result = {"dataset_kind": "single_cell_other", "format": dataset_path.suffix.lower() or "dir",
                  "dataset_path": str(dataset_path),
                  "note": "read directly by the scanpy analysis tools (no text preflight)"}
        if dataset_path.suffix.lower() == ".h5" and dataset_path.is_file():
            result.update(_tenx_h5_profile(dataset_path))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "dataset_results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return {"result": result, "result_path": output_dir / "dataset_results.json"}

    rows = load_rows(dataset_path)
    genes = [
        column
        for column in numeric_columns(rows)
        if column.lower() not in {"x", "y", "row", "col"}
    ]
    kind = infer_dataset_kind(rows)
    if kind == "single_cell_counts":
        result = run_single_cell_smoke(rows, genes)
    elif kind == "spatial_expression":
        result = run_spatial_smoke(rows, genes)
    else:
        result = {
            "dataset_kind": kind,
            "rows": len(rows),
            "numeric_columns": genes,
            "column_means": {
                column: mean(float(row[column]) for row in rows)
                for column in genes
            },
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "dataset_results.json"
    summary_path = output_dir / "dataset_summary.md"
    manifest_path = output_dir / "dataset_manifest.json"
    metrics_path = output_dir / "qc_metrics.json"
    preflight_path = output_dir / "qc_preflight_summary.md"
    trace_path = output_dir / "agent_decision_trace.json"
    manifest = build_dataset_manifest(dataset_path, result)
    trace = build_decision_trace(dataset_path, result, "csv_smoke_preflight")
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary_path.write_text(render_dataset_summary(dataset_path, result), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    metrics_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    preflight_path.write_text(render_dataset_summary(dataset_path, result), encoding="utf-8")
    trace_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return {
        "result": result,
        "result_path": result_path,
        "summary_path": summary_path,
        "manifest_path": manifest_path,
        "metrics_path": metrics_path,
        "preflight_path": preflight_path,
        "trace_path": trace_path,
    }


def _tenx_h5_profile(dataset_path: Path) -> dict[str, Any]:
    """The profile of a 10x Cell Ranger matrix ``.h5`` ({} when the file is not one): its size, and a
    note stating what it holds, so the planner does not schedule a hand-written audit to find out."""
    from ._lib.cellranger import RAW, describe_10x_h5, is_count_matrix, matrix_facts

    matrix = describe_10x_h5(str(dataset_path))
    if not matrix:
        return {}
    profile: dict[str, Any] = {"tenx_h5": matrix}
    # A raw (unfiltered) matrix's barcodes are mostly empty droplets: they are not cells.
    if RAW not in dataset_path.name:
        profile.update(cells=matrix["n_barcodes"], genes=matrix["n_features"])
    ruling = ("These are integer UMI counts as Cell Ranger writes them: their provenance needs no "
              "`run_code` audit." if is_count_matrix(matrix) else
              "⚠ Cell Ranger writes integer UMI counts only, so this file was changed after Cell "
              "Ranger: establish what it holds before any count-based step.")
    profile["note"] = (f"a 10x Cell Ranger feature-barcode matrix, read at upload: "
                       f"{matrix_facts(matrix)}. {ruling}")
    return profile


def fetch_public_dataset(name: str, output_dir: Path, max_bytes: int = MAX_PUBLIC_DATASET_BYTES) -> Path:
    if name != "pbmc3k":
        raise ValueError(f"Unsupported public dataset: {name}")

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "pbmc3k_raw.h5ad"
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    request = urllib.request.Request(PBMC3K_URL, headers={"User-Agent": "BioAgentPrototype/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        length_header = response.headers.get("Content-Length")
        if length_header and int(length_header) > max_bytes:
            raise ValueError(f"Dataset is larger than limit: {length_header} bytes > {max_bytes}")
        downloaded = 0
        with destination.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    destination.unlink(missing_ok=True)
                    raise ValueError(f"Dataset exceeded size limit: {downloaded} bytes > {max_bytes}")
                handle.write(chunk)
    return destination


def run_vcf_preflight(dataset_path: Path, output_dir: Path) -> dict[str, Any]:
    """Lightweight preflight for a VCF (``.vcf`` / ``.vcf.gz``): the sample names from the ``#CHROM``
    header line + a (capped) variant count. No scanpy — a VCF holds variant calls, so this just
    profiles + labels it (``dataset_kind="vcf_variants"``) so the planner routes it to the
    variant-annotation workflow. Reads at most a bounded number of lines; never raises."""
    import gzip

    samples: list[str] = []
    n_sampled = 0
    truncated = False
    err = ""
    _open = gzip.open if dataset_path.suffix.lower() == ".gz" else open
    try:
        with _open(dataset_path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("##"):
                    continue
                if line.startswith("#CHROM"):
                    cols = line.rstrip("\n").split("\t")
                    samples = cols[9:] if len(cols) > 9 else []
                    continue
                if line.strip():
                    n_sampled += 1
                    if n_sampled >= 20000:      # a count SAMPLE — enough to gauge scale, bounded cost
                        truncated = True
                        break
    except OSError as exc:  # unreadable file — still label it a VCF so routing is correct
        err = f"{type(exc).__name__}: {exc}"

    result: dict[str, Any] = {
        "dataset_kind": "vcf_variants",
        "format": ".vcf.gz" if dataset_path.name.lower().endswith(".vcf.gz") else ".vcf",
        "dataset_path": str(dataset_path),
        "file_size_bytes": dataset_path.stat().st_size if dataset_path.exists() else 0,
        "n_samples": len(samples),
        "samples": samples[:20],
        "n_variants_sampled": n_sampled,
        "variant_count_truncated": truncated,
        "note": ("variant calls — annotate with the variant_annotation workflow "
                 "(Ensembl VEP + ClinVar), NOT the scanpy single-cell line"),
    }
    if err:
        result["error"] = err
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "dataset_results.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return {"result": result, "result_path": result_path}


def run_h5ad_preflight(dataset_path: Path, output_dir: Path) -> dict[str, Any]:
    try:
        import h5py  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("h5py is required for .h5ad preflight analysis") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    with h5py.File(dataset_path, "r") as handle:
        result = inspect_h5ad(handle)

    result["dataset_kind"] = "h5ad_single_cell"
    result["dataset_path"] = str(dataset_path)
    result["file_size_bytes"] = dataset_path.stat().st_size

    manifest = build_dataset_manifest(dataset_path, result)
    trace = build_decision_trace(dataset_path, result, "h5ad_preflight")

    result_path = output_dir / "dataset_results.json"
    summary_path = output_dir / "dataset_summary.md"
    manifest_path = output_dir / "dataset_manifest.json"
    metrics_path = output_dir / "qc_metrics.json"
    preflight_path = output_dir / "qc_preflight_summary.md"
    trace_path = output_dir / "agent_decision_trace.json"

    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary = render_dataset_summary(dataset_path, result)
    summary_path.write_text(summary, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    metrics_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    preflight_path.write_text(summary, encoding="utf-8")
    trace_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return {
        "result": result,
        "result_path": result_path,
        "summary_path": summary_path,
        "manifest_path": manifest_path,
        "metrics_path": metrics_path,
        "preflight_path": preflight_path,
        "trace_path": trace_path,
    }


# How many rows of a PLAIN obs column to read when counting its distinct values. Bounded because
# an obs column can be millions of rows and this runs in the run-start preflight; large enough that
# a real design column (sample / donor / condition) shows all its levels well inside it.
_PLAIN_COL_SCAN = 50_000


def _decode(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def _is_group(node: Any) -> bool:
    # an h5py Group has keys and no dtype; a Dataset has no keys; a pandas Series (an obs passed
    # as a DataFrame) has both
    return hasattr(node, "keys") and not hasattr(node, "dtype")


def _is_categorical(node: Any) -> bool:
    return _is_group(node) and "categories" in node and "codes" in node


def _h5_head(node: Any, n: "int | None" = None) -> "tuple[Any, Any]":
    """The first ``n`` entries (all when None) of a NON-categorical obs element, plus its
    missing-value mask (None when the encoding has none).

    anndata writes the same logical column more than one way, and these readers must take all of
    them: a plain dataset (``array`` / ``string-array``), or a group holding ``values`` and a
    boolean ``mask`` (True = missing). The group form has always been used for ``nullable-integer``
    and ``nullable-boolean``; since anndata 0.13 under pandas 3, whose default ``str`` dtype is
    nullable, it is also ``nullable-string-array`` — the obs INDEX and every string column not
    converted to a categorical. Reading those with ``node[:n]`` / ``node.shape`` raised inside the
    callers' never-raise guards, so a file written by a current anndata silently lost its
    ``design_by_arm`` table. Raises on anything else; callers catch."""
    if _is_group(node):
        values = node["values"][:n]
        return values, (node["mask"][:n] if "mask" in node else None)
    return node[:n], None


def _h5_len(node: Any) -> int:
    """Row count of an obs element in any of the encodings above."""
    if _is_group(node):
        node = node["codes"] if _is_categorical(node) else node["values"]
    return int(node.shape[0])


def _categories(node: Any) -> list[str]:
    raw, _ = _h5_head(node["categories"])
    return [_decode(v) for v in raw]


def _obs_categoricals(obs: Any, *, max_cols: int = 30, list_max: int = 30) -> dict[str, Any]:
    """Category values for the CATEGORICAL obs columns (anndata stores these as a subgroup with
    a ``categories`` child). Surfaces the dataset's experimental design — a condition/group
    column like ``sampleid=[DDX41, WT]`` and existing label columns like ``majorclass`` — to the
    planner, so it can plan a group comparison / reuse labels instead of a generic atlas.

    Low-cardinality columns get their values listed; a high-cardinality column (e.g. a fine
    ``celltype`` with 100+ levels) records only its count, so the prompt stays compact.

    A PLAIN (non-Categorical) column is counted too, not skipped. Whether a column arrived as a
    pandas Categorical is a serialization detail of whoever wrote the file, and treating it as the
    test for "does this describe the design" put a decisive column in the wrong bucket: a dataset
    whose ``orig.ident`` was a plain string array holding ONE value ('0', i.e. a single library and
    therefore zero biological replication) was omitted here, so the planner's profile filed it
    under "numeric / high-cardinality" — advertising many donors where there was one. The plan that
    came back proposed aggregating counts "per donor", which the data could not support. Never
    raises — a malformed column is skipped."""
    out: dict[str, Any] = {}
    try:
        keys = list(obs.keys())
    except Exception:  # noqa: BLE001 - preflight must never crash on an odd file
        return out
    for key in keys[:max_cols]:
        if key.startswith("_"):
            continue
        try:
            node = obs[key]
            if _is_categorical(node):
                values = _categories(node)
                n = len(values)
                out[key] = {"n": n, "values": values[:list_max] if n <= list_max else []}
                continue
            # Plain column. Read a bounded head rather than the whole thing (an obs column can be
            # millions of rows) and only record it when it is genuinely low-cardinality — a float
            # measurement or a per-cell barcode has nothing to say about the design.
            head, mask = _h5_head(node, _PLAIN_COL_SCAN)
            if getattr(head, "dtype", None) is not None and head.dtype.kind == "f":
                continue                          # continuous measurement, not a design column
            missing = mask if mask is not None else [False] * len(head)
            seen: list[str] = []
            for v, m in zip(head, missing):
                if m:
                    continue                      # a missing entry is not a level, as NA is no category
                s = _decode(v)
                if s not in seen:
                    seen.append(s)
                    if len(seen) > list_max:
                        break
            if not seen or len(seen) > list_max:
                continue
            entry: dict[str, Any] = {"n": len(seen), "values": sorted(seen)}
            if _h5_len(node) > _PLAIN_COL_SCAN:
                entry["scanned"] = _PLAIN_COL_SCAN   # count is from a head sample, not the column
            out[key] = entry
        except Exception:  # noqa: BLE001 - skip an unreadable column, keep the rest
            continue
    return out


# Numeric per-cell QC columns most pipelines leave in obs (Seurat / scanpy names). Only these are
# summarised per arm — a float column that is not one of these is a measurement, not a QC metric.
_QC_NUMERIC_COLS = ("nCount_RNA", "nFeature_RNA", "percent.mt", "pct_counts_mt", "total_counts",
                    "n_genes_by_counts", "n_genes", "n_counts", "nuclear_fraction", "pANN")
_ARM_TABLE_MAX_CELLS = 400_000


def _obs_column_values(obs: Any, key: str, n: int) -> "list[str] | None":
    """The first ``n`` values of an obs column as strings — categorical (codes → categories),
    plain or nullable (masked entries → "NA") — or None when unreadable."""
    try:
        node = obs[key]
        if _is_categorical(node):
            cats = _categories(node)
            codes = node["codes"][:n]
            return [cats[int(c)] if 0 <= int(c) < len(cats) else "NA" for c in codes]
        raw, mask = _h5_head(node, n)
        missing = mask if mask is not None else [False] * len(raw)
        return ["NA" if m else _decode(v) for v, m in zip(raw, missing)]
    except Exception:  # noqa: BLE001
        return None


def _design_by_arm(obs: Any, cats: dict[str, Any], n_cells: int) -> "dict[str, Any] | None":
    """Per-ARM cell counts by label column, plus per-arm medians of the QC columns the file
    already carries — the table a reviewer needs before reading any comparison.

    Built because a production report narrated "translation machinery up in every cell type" as
    DDX41 biology while nothing in the run had asked whether the two arms were sequenced to the
    same depth or quality; a direction-biased shift in every stratum with ribosomal genes on top
    is the classic signature of exactly that, and the numbers to check it (nCount / nFeature /
    percent.mt per arm) sat in obs the whole time. Deterministic, bounded, never raises. Returns
    None when the profile shows no 2-3-level condition column."""
    import statistics
    def _numeric_values(info: dict) -> bool:
        vals = info.get("values") or []
        if not vals:
            return False
        try:
            [float(v) for v in vals]
            return True
        except (TypeError, ValueError):
            return False

    # A condition column holds a few NAMED levels. An integer QC column that happens to take
    # only two or three distinct values (a tiny synthetic object's nFeature) is not a design.
    cond = next((c for c, i in cats.items()
                 if isinstance(i, dict) and isinstance(i.get("n"), int) and 2 <= i["n"] <= 3
                 and not _looks_like_label_col(c) and not _looks_like_qc_col(c)
                 and c not in _QC_NUMERIC_COLS and not _numeric_values(i)), None)
    if not cond or not n_cells:
        return None
    n = min(int(n_cells), _ARM_TABLE_MAX_CELLS)
    arm = _obs_column_values(obs, cond, n)
    if not arm:
        return None
    out: dict[str, Any] = {"condition_column": cond,
                           "cells_scanned": n, "cells_total": int(n_cells)}
    arms = sorted(set(arm))
    out["cells_by_arm"] = {a: sum(1 for x in arm if x == a) for a in arms}
    # counts per label column per arm (the coarsest label column, <= 30 levels)
    label_col = next((c for c, i in cats.items()
                      if _looks_like_label_col(c) and not _looks_like_qc_col(c)
                      and isinstance(i, dict) and isinstance(i.get("n"), int)
                      and 2 <= i["n"] <= 30), None)
    if label_col:
        lab = _obs_column_values(obs, label_col, n)
        if lab:
            tab: dict[str, dict[str, int]] = {}
            for a, l in zip(arm, lab):
                tab.setdefault(l, {}).setdefault(a, 0)
                tab[l][a] += 1
            out["label_column"] = label_col
            out["cells_by_label_and_arm"] = {l: {a: tab[l].get(a, 0) for a in arms}
                                             for l in sorted(tab)}
    # per-arm medians of whatever QC columns exist
    qc: dict[str, dict[str, float]] = {}
    for col in _QC_NUMERIC_COLS:
        try:
            if col not in obs.keys():
                continue
            vals, mask = _h5_head(obs[col], n)     # a nullable-integer count column is a group
            if getattr(vals, "dtype", None) is None or vals.dtype.kind not in "fiu":
                continue
            missing = mask if mask is not None else [False] * len(vals)
            per: dict[str, float] = {}
            for a in arms:
                # a masked or NaN entry is a missing measurement, not a value to take the median of
                sel = [float(v) for v, x, m in zip(vals, arm, missing) if x == a and not m and v == v]
                if sel:
                    per[a] = round(statistics.median(sel), 3)
            if per:
                qc[col] = per
        except Exception:  # noqa: BLE001
            continue
    if qc:
        out["qc_median_by_arm"] = qc
        # A flag the PI and the writer can act on without re-deriving it: depth differing by
        # >1.5x between arms means a pooled/global shift must be read as technical first.
        depth_col = next((c for c in ("nCount_RNA", "total_counts", "n_counts") if c in qc), None)
        if depth_col and len(qc[depth_col]) >= 2:
            vals = sorted(qc[depth_col].values())
            if vals[0] > 0 and vals[-1] / vals[0] > 1.5:
                out["depth_imbalance"] = (
                    f"median {depth_col} differs {vals[-1] / vals[0]:.1f}x between arms "
                    f"({qc[depth_col]}); a global, same-direction expression shift across cell "
                    "types is consistent with this depth difference and must not be read as "
                    "biology without a per-cell-type depth-matched check.")
        # snRNA hint: a `nuclear_fraction` column is the fingerprint of a single-NUCLEUS protocol
        # (or of a nuclei-fraction QC pass). Nuclei carry almost no mitochondria, so the standard
        # 10% mitochondrial threshold is far too lax for them (1-5% is the working range) — and a
        # low percent.mt median corroborates it. The chain (column exists -> likely snRNA -> mt
        # threshold guidance) sat un-connected across the prompt in run 97dfc89dc5aa; compute it
        # here so no model has to make the connection itself.
        try:
            has_nf = any("nuclear_fraction" in str(c).lower() for c in obs.keys())
        except Exception:  # noqa: BLE001 - the hint is an extra, never a blocker
            has_nf = False
        if has_nf:
            mt = (qc.get("percent.mt") or qc.get("pct_counts_mt") or {})
            mt_med = max(mt.values()) if mt else None
            out["snrna_hint"] = (
                "a `nuclear_fraction` column is present"
                + (f" and median mitochondrial content is low ({mt_med:.1f}%)" if mt_med is not None else "")
                + " — this is likely single-NUCLEUS (snRNA-seq) data. For nuclei the mitochondrial "
                "QC threshold should be 1-5%, not the 10% single-cell default; state the protocol "
                "assumption explicitly in the report.")
    return out


# Ensembl gene-ID prefixes, most specific first ("ENSG" is a prefix of none of the others, but the
# order keeps a future ENSxxxG entry from being shadowed).
_ENSEMBL_SPECIES = (("ENSMUSG", "mouse"), ("ENSRNOG", "rat"), ("ENSDARG", "zebrafish"),
                    ("ENSG", "human"))
_TITLE_SYMBOL = re.compile(r"^[A-Z][a-z0-9][a-z0-9.\-]*$")     # Gfap, Rpl13a, Actb
_UPPER_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]*[A-Z0-9]$")     # GFAP, RPL13A, ACTB
_HOUSEKEEPING = ("Actb", "Gapdh", "Rho", "Glul", "Rlbp1", "Malat1")


def _gene_symbol_species(var: Any) -> "dict[str, Any] | None":
    """The species the gene identifiers point to, read off the var index.

    A plan writes gene symbols, and a mouse matrix spells them `Gfap` while a human one spells
    them `GFAP`. The profile never said which, so a DDX41 plan on a mouse retina declared the
    species "unknown" and wrote every marker and signature gene in human upper case — symbols an
    exact-match lookup does not find. The case convention (and the mitochondrial prefix: mouse
    `mt-`, rat `Mt-`, human `MT-`) settles it from the file, without a model guessing.
    ``None`` when the index cannot be read or says nothing either way."""
    try:
        idx_key = var.attrs.get("_index", "_index") if hasattr(var, "attrs") else "_index"
        idx_key = _decode(idx_key)
        if idx_key not in var:
            return None
        names = _obs_column_values(var, idx_key, _h5_len(var[idx_key])) or []
    except Exception:  # noqa: BLE001 - the hint is an extra, never a blocker
        return None
    if not names:
        return None
    ids = [n for n in names if n.startswith("ENS")]
    if len(ids) > len(names) / 2:
        for prefix, species in _ENSEMBL_SPECIES:
            if sum(1 for n in ids if n.startswith(prefix)) > len(ids) / 2:
                return {"identifiers": "ensembl", "species_hint": species,
                        "evidence": f"{len(ids)} of {len(names)} var names are Ensembl {prefix} IDs"}
        return {"identifiers": "ensembl", "species_hint": None,
                "evidence": f"{len(ids)} of {len(names)} var names are Ensembl IDs"}
    title = sum(1 for n in names if _TITLE_SYMBOL.match(n))
    upper = sum(1 for n in names if _UPPER_SYMBOL.match(n))
    mito = {p: sum(1 for n in names if n.startswith(p)) for p in ("mt-", "Mt-", "MT-")}
    if title + upper == 0:
        return None
    frac_title = title / (title + upper)
    mito_note = ", ".join(f"{k}: {v}" for k, v in mito.items() if v)
    evidence = (f"{title} Title-case vs {upper} upper-case symbols"
                + (f"; mitochondrial prefix {mito_note}" if mito_note else ""))
    if frac_title >= 0.8:
        species = "rat" if mito["Mt-"] > mito["mt-"] else "mouse"
        present = set(names)
        return {"identifiers": "symbols", "symbol_case": "Title-case (e.g. Gfap)",
                "species_hint": species, "evidence": evidence,
                "examples": [g for g in _HOUSEKEEPING if g in present][:4]}
    if frac_title <= 0.2:
        present = set(names)
        return {"identifiers": "symbols", "symbol_case": "UPPER-case (e.g. GFAP)",
                "species_hint": "human", "evidence": evidence,
                "examples": [g.upper() for g in _HOUSEKEEPING if g.upper() in present][:4]}
    return {"identifiers": "symbols", "symbol_case": "mixed", "species_hint": None,
            "evidence": evidence}


def _looks_like_label_col(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("celltype", "cell_type", "majorclass", "cluster", "leiden",
                                "louvain", "annotation", "label", "subclass", "class"))


def _looks_like_qc_col(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("classification", "doublet", "singlet", "qc", "filter", "pass",
                                "phase", "batch_key")) or n.startswith("df.")


def inspect_h5ad(handle: Any) -> dict[str, Any]:
    x = handle.get("X")
    obs = handle.get("obs")
    var = handle.get("var")
    result: dict[str, Any] = {
        "format": "h5ad",
        "cells": int(obs.shape[0]) if obs is not None and hasattr(obs, "shape") and obs.shape else None,
        "genes": int(var.shape[0]) if var is not None and hasattr(var, "shape") and var.shape else None,
        "obs_keys": list(obs.keys())[:30] if obs is not None and hasattr(obs, "keys") else [],
        "var_keys": list(var.keys())[:30] if var is not None and hasattr(var, "keys") else [],
    }
    if obs is not None and hasattr(obs, "keys"):
        cats = _obs_categoricals(obs)
        if cats:
            result["obs_categoricals"] = cats
            try:
                n_obs = result.get("cells")
                if not n_obs:
                    idx_key = obs.attrs.get("_index", "_index") if hasattr(obs, "attrs") else "_index"
                    idx_key = idx_key.decode() if isinstance(idx_key, bytes) else str(idx_key)
                    # the index is a `nullable-string-array` GROUP under anndata 0.13 + pandas 3
                    n_obs = _h5_len(obs[idx_key]) if idx_key in obs else 0
                design = _design_by_arm(obs, cats, n_obs)
            except Exception:  # noqa: BLE001 - the arm table is an extra, never a blocker
                design = None
            if design:
                result["design_by_arm"] = design
    if var is not None and hasattr(var, "keys"):
        symbols = _gene_symbol_species(var)
        if symbols:
            result["gene_symbols"] = symbols

    if x is None:
        result["x_encoding"] = "missing"
        return result

    if hasattr(x, "shape"):
        shape = tuple(int(part) for part in x.shape)
        result["x_shape"] = list(shape)
        if len(shape) == 2:
            result["cells"] = shape[0]
            result["genes"] = shape[1]
        result["x_encoding"] = "dense"
        sample_rows = min(shape[0], 200) if len(shape) == 2 else 0
        if sample_rows:
            sample = x[:sample_rows, :]
            row_sums = [float(sum(row)) for row in sample]
            result["sampled_cells"] = sample_rows
            result["mean_total_counts_sample"] = mean(row_sums) if row_sums else 0
        return result

    result["x_encoding"] = x.attrs.get("encoding-type", "sparse_group")
    if all(key in x for key in ("data", "indices", "indptr")):
        data = x["data"]
        indptr = x["indptr"]
        shape_value = x.attrs.get("shape", x.attrs.get("h5sparse_shape", (len(indptr) - 1, 0)))
        shape = tuple(int(part) for part in shape_value)
        result["x_shape"] = list(shape)
        if len(shape) == 2:
            result["cells"] = shape[0]
            result["genes"] = shape[1]
            denominator = shape[0] * shape[1]
            result["density"] = float(len(data) / denominator) if denominator else 0
        result["nonzero_entries"] = int(len(data))
        sample_cells = min(max(len(indptr) - 1, 0), 500)
        totals: list[float] = []
        genes_detected: list[int] = []
        for row_idx in range(sample_cells):
            start = int(indptr[row_idx])
            end = int(indptr[row_idx + 1])
            values = data[start:end]
            totals.append(float(values[:].sum()))
            genes_detected.append(end - start)
        result["sampled_cells"] = sample_cells
        result["mean_total_counts_sample"] = mean(totals) if totals else 0
        result["mean_genes_detected_sample"] = mean(genes_detected) if genes_detected else 0
    return result


def build_dataset_manifest(dataset_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_path": str(dataset_path),
        "file_name": dataset_path.name,
        "file_suffix": dataset_path.suffix,
        "file_size_bytes": dataset_path.stat().st_size if dataset_path.exists() else None,
        "dataset_kind": result.get("dataset_kind"),
        "cells": result.get("cells"),
        "genes": result.get("genes"),
        "privacy": {
            "raw_data_sent_to_llm": False,
            "local_preflight_only": True,
        },
    }


def build_decision_trace(dataset_path: Path, result: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "dataset_path": str(dataset_path),
        "dataset_kind": result.get("dataset_kind"),
        "selected_capability": "data_qc_preflight",
        "scientific_claim_level": "preflight_only",
        "raw_data_sent_to_llm": False,
        "next_recommended_agent": "real_scanpy_qc_agent",
    }


def render_dataset_summary(dataset_path: Path, result: dict[str, Any]) -> str:
    lines = [
        "# Dataset Smoke Analysis",
        "",
        f"Dataset: `{dataset_path}`",
        f"Kind: `{result['dataset_kind']}`",
        "",
    ]
    for key in (
        "format",
        "cells",
        "spots",
        "rows",
        "genes",
        "regions",
        "file_size_bytes",
        "x_encoding",
        "density",
        "nonzero_entries",
        "sampled_cells",
        "mean_total_counts",
        "mean_total_counts_sample",
        "mean_genes_detected_sample",
        "mean_mito_fraction",
    ):
        if key in result:
            lines.append(f"- {key}: {result[key]}")
    if result.get("condition_effects"):
        lines.extend(["", "## Largest Condition Effects", ""])
        effects = sorted(
            result["condition_effects"].items(),
            key=lambda item: abs(item[1]["log2_fold_change"]),
            reverse=True,
        )
        for gene, effect in effects[:5]:
            lines.append(
                f"- {gene}: log2FC={effect['log2_fold_change']:.3f} "
                f"({effect['baseline']}={effect['baseline_mean']:.2f}, "
                f"{effect['comparison']}={effect['comparison_mean']:.2f})"
            )
    lines.append("")
    return "\n".join(lines)
