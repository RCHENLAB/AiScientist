"""In-container CLI: run ONE offline variant-annotation step against a workspace + VCF, emit result
JSON. The variant-line counterpart of :mod:`bioagent.tools.scrna_cli`.

Runs INSIDE ``vep.sif`` on an HPC3 CPU node, driven by
:class:`bioagent.gateway.slurm_analysis.SlurmAnalysisExecutor` (with the VEP cache + ClinVar VCF
bind-mounted read-only). It imports the SAME :func:`bioagent.tools.vcf_offline.run_offline_annotation`
the eyeserver would call, so identical code runs locally (fallback) or on HPC3 — no forked logic.

Deployment config the offline annotation needs but the LLM never sends (the cache dir, the ClinVar
VCF, the fork width) arrives in ``--args`` — the executor injects it there before submitting.

Result contract (same as ``scrna_cli``): the tool's result dict is printed on one marked line::

    BIOAGENT_RESULT_JSON {"status": "ok", ...}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RESULT_MARKER = "BIOAGENT_RESULT_JSON "


def run_tool(tool: str, workspace: str, dataset_path: str | None,
             args: dict[str, Any] | None) -> dict[str, Any]:
    """Dispatch ONE variant step. Only ``annotate_variants`` (offline VEP) exists today."""
    args = args or {}
    if tool != "annotate_variants":
        return {"status": "error", "error": f"unknown variant tool: {tool}"}
    if not dataset_path:
        return {"status": "error", "error": "annotate_variants needs a VCF dataset path"}
    from .vcf_offline import run_offline_annotation
    return run_offline_annotation(
        dataset_path, workspace,
        cache_dir=str(args.get("cache_dir", "")),
        assembly=str(args.get("assembly", "GRCh38") or "GRCh38"),
        fork=int(args.get("fork", 8) or 8),
        clinvar_vcf=str(args.get("clinvar_vcf", "")),
        pass_only=bool(args.get("pass_only", True)),
        regions_bed=str(args.get("regions_bed", "")),
        sample=str(args.get("sample", "")),
        max_variants=int(args.get("max_variants", 0) or 0),
        max_pop_af=float(args.get("max_pop_af", 0) or 0),
        genes=args.get("genes") or None,
        # predictor-plugin config injected by the gateway (paths resolve inside vep.sif)
        plugins_enabled=args.get("plugins_enabled"),
        cadd_snv=str(args.get("cadd_snv", "")), cadd_indels=str(args.get("cadd_indels", "")),
        alphamissense=str(args.get("alphamissense", "")), revel=str(args.get("revel", "")),
        plugins_dir=str(args.get("plugins_dir", "")), ref_fasta=str(args.get("ref_fasta", "")),
        # SpliceAI (OpenSpliceAI) config — conda-env binary + model dir, injected by the gateway
        spliceai_enabled=args.get("spliceai_enabled"),
        spliceai_bin=str(args.get("spliceai_bin", "")), spliceai_models=str(args.get("spliceai_models", "")),
        spliceai_max_variants=int(args.get("spliceai_max_variants", 0) or 0),   # 0 = no cap
        # IRD annotation layers (HGMD / retina exon / ATAC / dbscSNV) — injected by the gateway
        ird_annotate=args.get("ird_annotate"),
        hgmd_path=str(args.get("hgmd_path", "")), retina_bed=str(args.get("retina_bed", "")),
        atac_path=str(args.get("atac_path", "")), dbscsnv_template=str(args.get("dbscsnv_template", "")),
    )


def _load_args(raw: str) -> dict[str, Any]:
    """``--args`` is either inline JSON or a path to a JSON file (the runner stages a file)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(Path(raw).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one bioagent variant-annotation step in-container.")
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
