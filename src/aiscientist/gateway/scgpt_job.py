"""Run scGPT per-cell annotation inference as a short-lived **GPU batch job** on HPC3.

scGPT is a gene/expression transformer (NOT an LLM), so its reference-based, per-cell
label transfer cannot be done by Qwen. We run it, but keep ``scgpt``/``torch`` entirely
inside a Singularity ``.sif`` — never in the gateway's Python env — and provision the GPU
as a **separate, on-demand, short-lived batch job** (Route C in
``docs/scgpt_workflow_integration.md``):

- NOT co-located on Qwen's vLLM GPU (that server claims most VRAM), and
- NOT a second *persistent* GPU (which would hold a scarce A100 for the whole session).

step-2 inference is one-shot: load ``best_model.pt`` -> infer the (vocab-aligned) query
h5ad -> write ``predictions.csv`` -> exit, releasing the GPU. That is exactly the shape of
the existing :mod:`aiscientist.gateway.slurm_job` batch engine, so this module is a thin,
GPU-flavoured wrapper over it: build a ``gpu:1`` sbatch whose body is a ``singularity exec
--nv`` of the scGPT image, submit + supervise via :func:`run_batch_job`, then read the
predictions path back. Everything goes through the ``RemoteExecutor`` protocol, so the
whole lifecycle runs offline against a scripted fake in tests — no real Slurm, no GPU.

The merge of ``predictions.csv`` back into the AnnData ``obs`` is a separate CPU/scanpy
concern (the analysis line), deliberately kept out of the GPU job.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .executor import RemoteExecutor
from .settings import HPCSettings
from .slurm_job import (
    AcquireConfig,
    EmitFn,
    JobResult,
    RunConfig,
    SlurmJobError,
    SlurmJobSpec,
    build_analysis_script,
    run_batch_job,
    singularity_exec,
)

PREDICTIONS_NAME = "predictions.csv"


def scgpt_job_name(username: str) -> str:
    """Per-user job name so a run can only ever match the current user's own job."""
    safe = "".join(c for c in (username or "user") if c.isalnum() or c in "-_")
    return f"bioagent-scgpt-{safe}"


@dataclass
class ScgptInferenceResult:
    """Outcome of one scGPT inference batch job."""

    job: JobResult
    predictions_csv: str   # cluster path to the per-cell predictions + confidence

    def as_dict(self) -> dict:
        return {"job": self.job.as_dict(), "predictions_csv": self.predictions_csv}


#: Name of the audit record the species step leaves next to the predictions.
HARMONIZATION_NAME = "species_harmonization.json"

# Runs INSIDE the scGPT container, before inference. The shipped model's vocabulary is HUMAN gene
# symbols (59,958 of its 60,697 entries are upper-case: TFRC, APOE, RHO) and it predicts 123 human
# RETINA cell types. A mouse query spells the same genes Tfrc, Apoe, Rho, so exact matching kept 17
# of 33,696 genes on the DDX41 retina object; normalising 15,307 cells over 17 genes divides by zero
# and the job died in log1p with "Input contains NaN". Case-folding recovers 16,599 genes and all 18
# canonical retinal markers — what stays unmatched is overwhelmingly Gm*/…Rik/ENSMUSG predicted genes
# with no human name at all.
#
# Applied only when it clearly helps (>=5x the exact overlap AND >=1000 more genes), so a human query
# — whose exact overlap is already high — passes through untouched. Either way the decision and both
# overlap counts are written to species_harmonization.json, because cross-species label transfer is
# an ASSUMPTION the report has to state, not a detail it may leave out.
_HARMONIZE_PY = r"""
import json, sys
import anndata as ad
inp, vocab_path, out_h5ad, report_path = sys.argv[1:5]
vocab = json.load(open(vocab_path))
a = ad.read_h5ad(inp)
genes = [str(g) for g in a.var_names]
exact = sum(g in vocab for g in genes)
upper = sum(g.upper() in vocab for g in genes)
rule = "none"
if upper >= max(5 * exact, exact + 1000):
    rule = "uppercase"
    a.var_names = [g.upper() for g in genes]
    a.var_names_make_unique()
    if a.raw is not None:
        raw = a.raw.to_adata()
        raw.var_names = [str(g).upper() for g in raw.var_names]
        raw.var_names_make_unique()
        a.raw = raw
    a.write_h5ad(out_h5ad)
json.dump({"genes": len(genes), "exact_match": exact, "uppercase_match": upper, "rule": rule,
           "assumption": ("query gene symbols were case-folded onto the model's human-symbol "
                          "vocabulary; orthology is assumed by NAME, and labels are transferred "
                          "across species") if rule == "uppercase" else None},
          open(report_path, "w"), indent=2)
print("species harmonization: %s (exact %d -> uppercase %d of %d)" % (rule, exact, upper, len(genes)))
"""


def build_scgpt_command(settings: HPCSettings, *, input_h5ad: str, model_dir: str, out_dir: str) -> str:
    """The in-container scGPT step-2 command, fed --input/--model/--out (all quoted), preceded by
    the species-harmonization step. The harmonizer is shipped base64-encoded in the command itself
    so it needs no source sync and no image rebuild, and is written to ``out_dir`` for audit."""
    import base64

    out = out_dir.rstrip("/")
    prog = f"{out}/harmonize_species.py"
    harmonized = f"{out}/query_harmonized.h5ad"
    b64 = base64.b64encode(_HARMONIZE_PY.encode()).decode()
    q = shlex.quote
    return (
        # Fail HERE, with the harmonizer's own error, rather than letting inference run on an input
        # it cannot use and die later in log1p with a NaN that points nowhere near the cause.
        f"echo {b64} | base64 -d > {q(prog)} && "
        f"python {q(prog)} {q(input_h5ad)} {q(model_dir.rstrip('/') + '/vocab.json')} "
        f"{q(harmonized)} {q(out + '/' + HARMONIZATION_NAME)} || exit 1; "
        # Use the harmonized copy only if the step wrote one; otherwise the original, unchanged.
        f"INPUT={q(input_h5ad)}; if [ -f {q(harmonized)} ]; then INPUT={q(harmonized)}; fi; "
        f"{settings.scgpt_entrypoint} "
        f"--input \"$INPUT\" "
        f"--model {q(model_dir)} "
        f"--out {q(out_dir)}"
    )


def build_scgpt_script(
    settings: HPCSettings,
    *,
    job_name: str,
    input_h5ad: str,
    model_dir: str,
    out_dir: str,
) -> str:
    """Build the full ``gpu:1`` sbatch script: a ``singularity exec --nv`` of the scGPT
    image with the dataset + model bound **read-only** and only ``out_dir`` writable."""
    import os.path

    inner = build_scgpt_command(settings, input_h5ad=input_h5ad, model_dir=model_dir, out_dir=out_dir)
    contained = singularity_exec(
        settings.scgpt_image,
        inner,
        # The query h5ad's directory + the reference model are read-only; the raw inputs
        # can never be modified or deleted by the contained job. Only out_dir is writable.
        binds_ro=(os.path.dirname(input_h5ad) or "/", model_dir),
        binds_rw=(out_dir,),
        nv=True,            # GPU passthrough — the one thing the vLLM serve job also needs
        network=False,      # weights are local; no network inside the container
        container_bin=settings.container_bin,
    )
    return build_analysis_script(
        job_name,
        contained,
        partition=settings.partition,
        cpus=settings.cpus,
        mem_gb=settings.mem_gb,
        time_limit=settings.time_limit,
        account=settings.account or "",
        gres=settings.scgpt_gres,           # "gpu:1" (any card) — decoupled from the LLM's A100 pin
        exclude=settings.exclude or "",     # keep GPU jobs off known-dead nodes
        container_module=settings.container_module,
        log_dir=out_dir,
    )


def run_scgpt_inference(
    executor: RemoteExecutor,
    settings: HPCSettings,
    *,
    input_h5ad: str,
    model_dir: str,
    out_dir: str,
    job_name: str | None = None,
    acquire: AcquireConfig | None = None,
    run: RunConfig | None = None,
    emit: EmitFn | None = None,
) -> ScgptInferenceResult:
    """Submit + supervise the scGPT inference GPU batch job, then return the predictions
    path. Raises :class:`SlurmJobError` if the job does not complete or writes no
    predictions. ``input_h5ad`` / ``model_dir`` / ``out_dir`` are **cluster** paths
    (staged on shared DFS); ``out_dir`` must be writable."""
    name = job_name or scgpt_job_name(executor.username)
    script = build_scgpt_script(
        settings, job_name=name, input_h5ad=input_h5ad, model_dir=model_dir, out_dir=out_dir
    )
    spec = SlurmJobSpec(script=script, job_name=name)
    result = run_batch_job(executor, spec, acquire=acquire, run=run, emit=emit)
    if not result.completed:
        raise SlurmJobError(
            f"scGPT inference job {result.job_id} did not complete (state {result.state}).",
            job_id=result.job_id,
            detail=result.as_dict(),
        )
    predictions = f"{out_dir.rstrip('/')}/{PREDICTIONS_NAME}"
    check = executor.exec(f"test -f {shlex.quote(predictions)} && echo OK")
    if check.out != "OK":
        raise SlurmJobError(
            f"scGPT inference job {result.job_id} completed but wrote no {PREDICTIONS_NAME} "
            f"in {out_dir}.",
            job_id=result.job_id,
        )
    return ScgptInferenceResult(job=result, predictions_csv=predictions)
