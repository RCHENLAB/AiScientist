from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_single_cell_qc_execution(decisions: dict[str, Any]) -> dict[str, Any]:
    dataset = decisions.get("dataset_result", {})
    dataset_kind = dataset.get("dataset_kind", "unknown")

    if dataset_kind == "single_cell_counts":
        mean_total = float(dataset.get("mean_total_counts", 0.0))
        mean_mito = float(dataset.get("mean_mito_fraction", 0.0))
        qc_flags = []
        if mean_total <= 0:
            qc_flags.append("mean total counts is zero")
        if mean_mito >= 0.2:
            qc_flags.append("mean mitochondrial fraction is high")
        status = "completed_lightweight_qc"
        recommendation = "pass_lightweight_qc" if not qc_flags else "review_qc_flags"
        metrics = {
            "cells": dataset.get("cells"),
            "genes": dataset.get("genes"),
            "mean_total_counts": mean_total,
            "mean_mito_fraction": mean_mito,
            "qc_flags": qc_flags,
        }
        limitations = [
            "Toy CSV QC is a smoke test, not a production Scanpy QC workflow.",
            "No PCA, clustering, doublet detection, or batch QC was performed.",
        ]
    elif dataset_kind == "h5ad_single_cell":
        status = "completed_h5ad_preflight_qc"
        recommendation = "ready_for_scanpy_qc_tool_pack"
        metrics = {
            "cells": dataset.get("cells"),
            "genes": dataset.get("genes"),
            "density": dataset.get("density"),
            "sampled_cells": dataset.get("sampled_cells"),
            "mean_total_counts_sample": dataset.get("mean_total_counts_sample"),
            "mean_genes_detected_sample": dataset.get("mean_genes_detected_sample"),
            "qc_flags": [],
        }
        limitations = [
            "Only H5AD matrix-level preflight metrics were computed.",
            "Scanpy QC plots and filtering thresholds are not implemented yet.",
        ]
    else:
        status = "not_applicable"
        recommendation = "needs_single_cell_dataset"
        metrics = {"qc_flags": ["dataset is not recognized as single-cell expression data"]}
        limitations = ["Single-cell QC execution requires single-cell CSV or H5AD input."]

    return {
        "agent": "single_cell_qc_execution_agent",
        "business_scenario": "retina_single_cell_qc",
        "status": status,
        "recommendation": recommendation,
        "dataset_kind": dataset_kind,
        "metrics": metrics,
        "limitations": limitations,
        "raw_data_to_llm": False,
        "slurm_required_now": False,
    }


def build_de_marker_execution(decisions: dict[str, Any]) -> dict[str, Any]:
    dataset = decisions.get("dataset_result", {})
    dataset_kind = dataset.get("dataset_kind", "unknown")
    effects = dataset.get("condition_effects", {})

    if effects:
        ranked = sorted(
            (
                {
                    "gene": gene,
                    "baseline": values.get("baseline"),
                    "comparison": values.get("comparison"),
                    "baseline_mean": values.get("baseline_mean"),
                    "comparison_mean": values.get("comparison_mean"),
                    "log2_fold_change": values.get("log2_fold_change"),
                    "abs_log2_fold_change": abs(float(values.get("log2_fold_change", 0.0))),
                }
                for gene, values in effects.items()
            ),
            key=lambda item: item["abs_log2_fold_change"],
            reverse=True,
        )
        status = "completed_lightweight_marker_screen"
        recommendation = "review_top_marker_candidates"
        limitations = [
            "This is an effect-size screen only; no statistical test or multiple-testing correction was run.",
            "Use a production DE tool after metadata, contrasts, and QC thresholds are confirmed.",
        ]
    elif dataset_kind == "h5ad_single_cell":
        ranked = []
        status = "blocked_missing_condition_metadata"
        recommendation = "collect_obs_metadata_before_de"
        limitations = [
            "PBMC3k raw H5AD preflight does not expose a confirmed condition column.",
            "DE requires reviewed group labels, covariates, and filtering thresholds.",
        ]
    else:
        ranked = []
        status = "not_applicable"
        recommendation = "needs_condition_annotated_expression_dataset"
        limitations = ["Marker/DE execution needs condition-annotated expression data."]

    return {
        "agent": "differential_expression_execution_agent",
        "business_scenario": "retina_condition_marker_screen",
        "status": status,
        "recommendation": recommendation,
        "dataset_kind": dataset_kind,
        "top_gene_effects": ranked[:10],
        "effect_count": len(ranked),
        "limitations": limitations,
        "raw_data_to_llm": False,
        "slurm_required_now": False,
    }


def write_single_cell_qc_artifacts(decisions: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = build_single_cell_qc_execution(decisions)
    result_path = output_dir / "single_cell_qc_execution.json"
    summary_path = output_dir / "single_cell_qc_execution_summary.md"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary_path.write_text(render_single_cell_qc_summary(result), encoding="utf-8")
    return {"result_path": result_path, "summary_path": summary_path}


def write_de_marker_artifacts(decisions: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = build_de_marker_execution(decisions)
    result_path = output_dir / "de_marker_execution.json"
    summary_path = output_dir / "de_marker_execution_summary.md"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary_path.write_text(render_de_marker_summary(result), encoding="utf-8")
    return {"result_path": result_path, "summary_path": summary_path}


def render_single_cell_qc_summary(result: dict[str, Any]) -> str:
    lines = [
        "# Single-Cell QC Execution",
        "",
        f"Status: `{result['status']}`",
        f"Recommendation: `{result['recommendation']}`",
        f"Dataset kind: `{result['dataset_kind']}`",
        f"Raw data sent to LLM: `{str(result['raw_data_to_llm']).lower()}`",
        "",
        "## Metrics",
        "",
    ]
    for key, value in result["metrics"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in result["limitations"])
    return "\n".join(lines) + "\n"


def render_de_marker_summary(result: dict[str, Any]) -> str:
    lines = [
        "# Differential Expression / Marker Execution",
        "",
        f"Status: `{result['status']}`",
        f"Recommendation: `{result['recommendation']}`",
        f"Dataset kind: `{result['dataset_kind']}`",
        f"Effect count: `{result['effect_count']}`",
        f"Raw data sent to LLM: `{str(result['raw_data_to_llm']).lower()}`",
        "",
        "## Top Gene Effects",
        "",
    ]
    if result["top_gene_effects"]:
        for item in result["top_gene_effects"]:
            lines.append(
                "- {gene}: log2FC `{log2_fold_change}` ({baseline} -> {comparison})".format(**item)
            )
    else:
        lines.append("- No marker effects were computed.")
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in result["limitations"])
    return "\n".join(lines) + "\n"
