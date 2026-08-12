"""Build the gateway-side ``scgpt_runner`` that the ``scgpt_annotate`` tool calls.

This is the closure injected into ``build_scientist_catalog(scgpt_runner=...)``, exactly how
the CodeAct sandbox is injected as ``code_executor``. It closes over the live SSH executor +
HPC settings and does the full remote round-trip for one annotation:

    stage the run's .h5ad onto shared DFS  (executor.put_file)
        -> run_scgpt_inference: gpu:1 Singularity batch job (gateway/scgpt_job.py)
        -> fetch predictions.csv back into the run artifacts  (executor.get_file)
        -> summarise the per-cell label distribution (no raw cells, no fabricated labels)

Only wired for a LIVE session with the scGPT image configured; in mock mode / offline the
tool gets ``scgpt_runner=None`` and reports not-enabled instead.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .executor import RemoteExecutor
from .scgpt_job import run_scgpt_inference, scgpt_job_name
from .settings import HPCSettings
from .slurm_job import SlurmJobError

if TYPE_CHECKING:
    from ..agents.research_harness import HarnessContext


def _label_counts(csv_path: Path) -> tuple[int, dict[str, int]]:
    """Read predictions.csv and return (n_cells, {label: count}). Tolerates the
    column being named ``predictions`` or ``prediction``."""
    n = 0
    counts: dict[str, int] = {}
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        col = "predictions" if "predictions" in (reader.fieldnames or []) else "prediction"
        for row in reader:
            n += 1
            label = (row.get(col) or "").strip() or "unknown"
            counts[label] = counts.get(label, 0) + 1
    return n, dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def build_scgpt_runner(executor: RemoteExecutor, settings: HPCSettings, *, cluster_user_dir: str):
    """Return a ``(args, ctx) -> dict`` runner, or raise if prerequisites are unmet at
    build time. ``cluster_user_dir`` is this user's process-file area on shared DFS
    (``<shared_root>/Temp/<user>``) — the staged query.h5ad + predictions under ``scgpt/<run_id>``
    are scratch and get swept with the rest of Temp once they go cold."""

    def _run(args: dict[str, Any], ctx: "HarnessContext") -> dict[str, Any]:
        local_h5ad = Path(str(ctx.decisions["dataset_path"]))
        if not local_h5ad.is_file():
            return {"status": "error", "error": f"dataset not found locally: {local_h5ad}"}
        model_dir = str(args.get("model_dir") or settings.scgpt_model_dir)

        # Per-run scratch on shared DFS (the GPU node reads/writes here, not $HOME).
        run_id = ctx.workspace.name if ctx.workspace else "run"
        base = f"{cluster_user_dir.rstrip('/')}/scgpt/{run_id}"
        remote_h5ad = f"{base}/query.h5ad"
        out_dir = f"{base}/out"

        # Fetch the GPU job's Slurm/stderr log into the run bundle (process/scgpt_job.log) on BOTH
        # success and failure — so a failed annotation (the real error, e.g. a partition/gres or
        # OOM constraint) is visible in the result, not stranded on HPC3. Log path mirrors
        # build_analysis_script's --output ({out_dir}/{name}-{job_id}.log). Best-effort.
        name = scgpt_job_name(executor.username)

        def _fetch_job_log(job_id: "str | None") -> None:
            if not job_id or not ctx.workspace:
                return
            proc_dir = ctx.workspace / "artifacts" / "process"
            try:
                proc_dir.mkdir(parents=True, exist_ok=True)
                executor.get_file(f"{out_dir}/{name}-{job_id}.log", str(proc_dir / "scgpt_job.log"))
            except Exception:  # noqa: BLE001 - log capture must never mask the real result/error
                pass

        executor.put_file(str(local_h5ad), remote_h5ad)
        try:
            result = run_scgpt_inference(
                executor, settings, input_h5ad=remote_h5ad, model_dir=model_dir, out_dir=out_dir,
                job_name=name,
            )
        except SlurmJobError as exc:
            _fetch_job_log(getattr(exc, "job_id", None))
            raise
        _fetch_job_log(result.job.job_id)

        # Pull predictions back into this run's artifacts (categorized under data/).
        local_out = (ctx.workspace / "artifacts" / "data") if ctx.workspace else Path(".")
        local_out.mkdir(parents=True, exist_ok=True)
        local_csv = local_out / "scgpt_predictions.csv"
        executor.get_file(result.predictions_csv, str(local_csv))

        n_cells, counts = _label_counts(local_csv)
        return {
            "status": "ok",
            "tool": "scgpt_annotate",
            "n_cells": n_cells,
            "n_cell_types": len(counts),
            "label_counts": counts,
            "predictions_csv": str(local_csv),
            "job": result.job.as_dict(),
            "model_dir": model_dir,
        }

    return _run
