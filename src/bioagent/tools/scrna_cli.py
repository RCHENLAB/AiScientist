"""In-container CLI: run ONE analysis-line step against a workspace + dataset, emit result JSON.

Phase 4 of the HPC3 offload. This runs INSIDE ``analysis.sif`` on an HPC3 CPU node, driven by
:class:`bioagent.gateway.slurm_analysis.SlurmAnalysisExecutor`. It imports the SAME tool functions
the eyeserver uses (``scrna_pack`` / dataset preflight) and calls the named one with a reconstructed
context, so identical code runs locally (fallback) or on HPC3 — no forked analysis logic.

The dataset + the run's ``work/``+``artifacts/`` all live on shared DFS (dfs3b), bind-mounted into
the container, so checkpoints accumulate in place across steps with no round-trip.

Result contract: the tool's result dict is printed on a single marked line::

    BIOAGENT_RESULT_JSON {"status": "ok", ...}

so the runner can parse it out of the captured stdout regardless of any other tool logging.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RESULT_MARKER = "BIOAGENT_RESULT_JSON "


class _Ctx:
    """Minimal stand-in for the harness context the scrna tools read (``.workspace`` +
    ``.decisions['dataset_path']``)."""

    def __init__(self, workspace: str, dataset_path: str | None) -> None:
        self.workspace = Path(workspace)
        self.decisions: dict[str, Any] = {"dataset_path": dataset_path} if dataset_path else {}


def _analysis_tools() -> dict[str, Any]:
    from . import scrna_advanced, scrna_pack
    return {
        "run_scanpy_qc": scrna_pack.run_scanpy_qc,
        "run_clustering": scrna_pack.run_clustering,
        "run_de": scrna_pack.run_de,
        "run_enrichment": scrna_pack.run_enrichment,
        "run_gsea_prerank": scrna_pack.run_gsea_prerank,
        "run_doublet_detection": scrna_advanced.run_doublet_detection,
        "run_integration": scrna_advanced.run_integration,
        "run_pseudobulk_de": scrna_advanced.run_pseudobulk_de,
        "run_composition": scrna_advanced.run_composition,
        "run_marker_annotation": scrna_advanced.run_marker_annotation,
    }


def run_tool(tool: str, workspace: str, dataset_path: str | None, args: dict[str, Any] | None) -> dict[str, Any]:
    """Dispatch ONE step against the workspace. ``preflight`` is the dataset smoke analysis
    (different signature); the rest are the standard ``(args, ctx) -> dict`` scrna tools."""
    args = args or {}
    if tool == "preflight":
        from .datasets import run_dataset_smoke_analysis
        if not dataset_path:
            return {"status": "error", "error": "preflight needs a dataset path"}
        out_dir = Path(workspace) / "artifacts" / "data"
        return run_dataset_smoke_analysis(Path(dataset_path), out_dir)
    fn = _analysis_tools().get(tool)
    if fn is None:
        return {"status": "error", "error": f"unknown analysis tool: {tool}"}
    return fn(args, _Ctx(workspace, dataset_path))


def _load_args(raw: str) -> dict[str, Any]:
    """``--args`` is either inline JSON or a path to a JSON file (the runner stages a file)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(Path(raw).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one bioagent analysis step in-container.")
    ap.add_argument("--tool", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dataset", default="")
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args(argv)
    try:
        args = _load_args(ns.args)
    except (json.JSONDecodeError, OSError) as exc:
        result: dict[str, Any] = {"status": "error", "error": f"bad --args: {exc}"}
    else:
        result = run_tool(ns.tool, ns.workspace, ns.dataset or None, args)
    print(RESULT_MARKER + json.dumps(result))
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
