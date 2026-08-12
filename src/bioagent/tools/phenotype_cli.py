"""In-container CLI: run ONE phenotype→disease step (LIRICAL) against a workspace + optional VCF, emit
result JSON. The phenotype-line counterpart of :mod:`bioagent.tools.variant_cli`.

Runs INSIDE ``lirical.sif`` on an HPC3 CPU node, driven by
:class:`bioagent.gateway.slurm_analysis.SlurmAnalysisExecutor` (with the LIRICAL data + the Exomiser
variant DB bind-mounted read-only). It imports the SAME
:func:`bioagent.tools.phenotype_dx.run_lirical` the eyeserver would call, so identical code runs
locally (fallback) or on HPC3 — no forked logic.

Deployment config the run needs but the LLM never sends (the LIRICAL data dir, the Exomiser DB paths)
arrives in ``--args`` — the executor injects it there before submitting. The patient's HPO terms come
in ``--args`` too (the gateway infers them from the study description / a structured list upstream).

Result contract (same as ``variant_cli`` / ``scrna_cli``): the tool's result dict is printed on one
marked line::

    BIOAGENT_RESULT_JSON {"status": "ok", ...}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

RESULT_MARKER = "BIOAGENT_RESULT_JSON "


def _lirical_exec(argv: list[str]) -> Any:
    """The real LIRICAL runner inside the container: run the fixed ``lirical prioritize`` argv the pure
    builder produced. LIRICAL is CPU-bound and needs no network (the data is bind-mounted)."""
    return subprocess.run(argv, capture_output=True, text=True)  # noqa: S603 - fixed tool argv


def run_tool(tool: str, workspace: str, dataset_path: str | None,
             args: dict[str, Any] | None) -> dict[str, Any]:
    """Dispatch ONE phenotype step. Only ``run_lirical`` exists today."""
    args = args or {}
    if tool != "run_lirical":
        return {"status": "error", "error": f"unknown phenotype tool: {tool}"}
    hpo_terms = args.get("hpo_terms") or []
    if not hpo_terms:
        return {"status": "error", "error": "run_lirical needs a non-empty hpo_terms list"}
    from .phenotype_dx import run_lirical
    return run_lirical(
        hpo_terms=hpo_terms,
        excluded_hpo=args.get("excluded_hpo") or (),
        vcf_path=dataset_path or str(args.get("vcf_path", "")),
        data_dir=str(args.get("data_dir", "")),
        workspace=workspace,
        assembly=str(args.get("assembly", "hg38") or "hg38"),
        sample_id=str(args.get("sample_id", "sample-1") or "sample-1"),
        exomiser_hg19=str(args.get("exomiser_hg19", "")),
        exomiser_hg38=str(args.get("exomiser_hg38", "")),
        output_prefix=str(args.get("output_prefix", "lirical") or "lirical"),
        labels=args.get("labels") or None,
        exec_fn=_lirical_exec,
    )


def _load_args(raw: str) -> dict[str, Any]:
    """``--args`` is either inline JSON or a path to a JSON file (the runner stages a file)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(Path(raw).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one bioagent phenotype→disease step in-container.")
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
