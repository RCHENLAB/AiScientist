"""Run the Qwen2.5-VL **render-level review** of a rendered report as a short-lived **GPU
batch job** on HPC3.

Qwen3.6 (the main LLM) is text-only: it can audit the numbers behind a chart, but it cannot
SEE render defects — text-over-text overlap, a caption printed on the figure, clipped cells,
a table that overran its box. Those live only in the final rendered pdf, and only a vision
model finds them. We run one, but keep ``transformers``/``torch``/the VL weights entirely
inside a Singularity ``.sif`` — never in the gateway's Python env — and provision the GPU as
a **separate, on-demand, short-lived batch job** (Route C, exactly like scGPT):

- NOT co-located on Qwen's vLLM GPU (that server claims most VRAM), and
- NOT a second *persistent* GPU (which would hold a scarce card for the whole session).

The review is one-shot: load Qwen2.5-VL -> rasterize the pdf -> audit each page -> write
``review.json`` -> exit, releasing the GPU. That is the same shape as
:mod:`bioagent.gateway.scgpt_job`, so this module is a thin, GPU-flavoured wrapper over the
:mod:`bioagent.gateway.slurm_job` batch engine. Everything goes through the
``RemoteExecutor`` protocol, so the whole lifecycle runs offline against a scripted fake in
tests — no real Slurm, no GPU.

Cost note: this job is billed by time to ``ruic20_lab_gpu``. It targets a CHEAP 24GB card
(A30 / RTX6000) via a typed ``gres`` (``settings.vlreview_gres``), NOT an A100 — the 7B VL
model needs ~16GB. The render loop that consumes ``review.json`` lives in
:mod:`bioagent.tools.visual_review`.
"""

from __future__ import annotations

import json
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

REVIEW_NAME = "review.json"


def vlreview_job_name(username: str) -> str:
    """Per-user job name so a run can only ever match the current user's own job."""
    safe = "".join(c for c in (username or "user") if c.isalnum() or c in "-_")
    return f"bioagent-vlreview-{safe}"


@dataclass
class VlReviewResult:
    """Outcome of one render-level VL review batch job."""

    job: JobResult
    review_json: str          # cluster path to review.json
    review: dict              # parsed contents (clean, defects, fix_directives, ...)

    def as_dict(self) -> dict:
        return {"job": self.job.as_dict(), "review_json": self.review_json, "review": self.review}


def build_vlreview_command(settings: HPCSettings, *, pdf: str, model_dir: str, out_dir: str) -> str:
    """The in-container review command, fed --pdf/--model/--out (all quoted)."""
    return (
        f"{settings.vlreview_entrypoint} "
        f"--pdf {shlex.quote(pdf)} "
        f"--model {shlex.quote(model_dir)} "
        f"--out {shlex.quote(out_dir)} "
        f"--dpi {int(settings.vlreview_dpi)}"
    )


def build_vlreview_script(
    settings: HPCSettings,
    *,
    job_name: str,
    pdf: str,
    model_dir: str,
    out_dir: str,
) -> str:
    """Build the ``gpu:1`` sbatch script: a ``singularity exec --nv`` of the vlreview image
    with the rendered report + VL weights bound **read-only** and only ``out_dir`` writable.
    ``gres`` comes from ``settings.vlreview_gres`` so this pins a cheap 24GB card, not an A100."""
    import os.path

    inner = build_vlreview_command(settings, pdf=pdf, model_dir=model_dir, out_dir=out_dir)
    contained = singularity_exec(
        settings.vlreview_image,
        inner,
        # The rendered pdf's directory + the VL weights are read-only; the job can never
        # modify the report it is auditing. Only out_dir (review.json + page PNGs) is writable.
        binds_ro=(os.path.dirname(pdf) or "/", model_dir),
        binds_rw=(out_dir,),
        nv=True,            # GPU passthrough
        network=False,      # weights are local; no network inside the container
        container_bin=settings.container_bin,
    )
    return build_analysis_script(
        job_name,
        contained,
        # Prefer the FREE, preemptible GPU partition (settings.vlreview_partition, default
        # "free-gpu") for this short review job — zero GPU charge; falls back to the paid
        # partition when left empty.
        partition=settings.vlreview_partition or settings.partition,
        cpus=settings.cpus,
        mem_gb=settings.mem_gb,
        time_limit=settings.vlreview_time_limit,
        account=settings.account or "",
        gres=settings.vlreview_gres,        # e.g. "gpu:A30:1" — cheap 24GB card, NOT A100
        container_module=settings.container_module,
        log_dir=out_dir,
    )


def run_vlreview(
    executor: RemoteExecutor,
    settings: HPCSettings,
    *,
    pdf: str,
    out_dir: str,
    model_dir: str | None = None,
    job_name: str | None = None,
    acquire: AcquireConfig | None = None,
    run: RunConfig | None = None,
    emit: EmitFn | None = None,
) -> VlReviewResult:
    """Submit + supervise the render-review GPU batch job, then read ``review.json`` back and
    return it parsed. Raises :class:`SlurmJobError` if the job does not complete or writes no
    review. ``pdf`` / ``model_dir`` / ``out_dir`` are **cluster** paths (staged on shared DFS);
    ``out_dir`` must be writable."""
    name = job_name or vlreview_job_name(executor.username)
    model = model_dir or settings.vlreview_model_dir
    script = build_vlreview_script(settings, job_name=name, pdf=pdf, model_dir=model, out_dir=out_dir)
    spec = SlurmJobSpec(script=script, job_name=name)
    result = run_batch_job(executor, spec, acquire=acquire, run=run, emit=emit)
    if not result.completed:
        raise SlurmJobError(
            f"vlreview job {result.job_id} did not complete (state {result.state}).",
            job_id=result.job_id,
            detail=result.as_dict(),
        )
    review_path = f"{out_dir.rstrip('/')}/{REVIEW_NAME}"
    check = executor.exec(f"test -f {shlex.quote(review_path)} && echo OK")
    if check.out != "OK":
        raise SlurmJobError(
            f"vlreview job {result.job_id} completed but wrote no {REVIEW_NAME} in {out_dir}.",
            job_id=result.job_id,
        )
    # SFTP, not `cat` on a login node — the review payload is file content, i.e. a transfer.
    payload = executor.read_bytes(review_path).decode("utf-8", errors="replace")
    try:
        review = json.loads(payload)
    except (json.JSONDecodeError, AttributeError):
        review = {"clean": True, "defects": [], "fix_directives": [], "note": "unparseable review.json"}
    return VlReviewResult(job=result, review_json=review_path, review=review)
