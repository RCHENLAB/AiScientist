from __future__ import annotations

import csv
import json
import math
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
                  "note": "read directly by the scanpy analysis tools (no text preflight)"}
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


def _obs_categoricals(obs: Any, *, max_cols: int = 30, list_max: int = 30) -> dict[str, Any]:
    """Category values for the CATEGORICAL obs columns (anndata stores these as a subgroup with
    a ``categories`` child). Surfaces the dataset's experimental design — a condition/group
    column like ``sampleid=[DDX41, WT]`` and existing label columns like ``majorclass`` — to the
    planner, so it can plan a group comparison / reuse labels instead of a generic atlas.

    Low-cardinality columns get their values listed; a high-cardinality column (e.g. a fine
    ``celltype`` with 100+ levels) records only its count, so the prompt stays compact. Never
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
            if not (hasattr(node, "keys") and "categories" in node):
                continue                         # plain (numeric/string) column — not categorical
            raw = node["categories"][:]
            values = [v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v) for v in raw]
            n = len(values)
            out[key] = {"n": n, "values": values[:list_max] if n <= list_max else []}
        except Exception:  # noqa: BLE001 - skip an unreadable column, keep the rest
            continue
    return out


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
