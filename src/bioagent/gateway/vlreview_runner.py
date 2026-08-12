"""Gateway-side runner for the render-level VL review: the bridge between the LOCAL render
(the report/PDF is produced on the eye server) and the REMOTE GPU that can actually *see* it.

The eye server has no GPU, so each render is shipped to shared DFS and audited by a short-lived
``gpu:1`` Singularity batch job on HPC3 (Route C — see :mod:`bioagent.gateway.vlreview_job`):

    stage the rendered PDF onto shared DFS  (executor.put_file)
        -> run_vlreview: gpu:1 Singularity batch job (gateway/vlreview_job.py)
        -> read review.json back                 (returned parsed by run_vlreview)

:func:`build_vlreview_review_fn` returns the ``review_fn(pdf_path) -> review dict`` callback the
render↔re-render loop (:func:`bioagent.tools.visual_review.render_with_visual_review`) calls once
per render pass. Each pass stages to its OWN sub-dir so a re-rendered PDF never clobbers the
previous pass's inputs/outputs. This mirrors :mod:`bioagent.gateway.scgpt_runner` — same remote
substrate, invoked in the finalization pipeline instead of as an agent tool, because the PDF it
audits only exists AFTER the research loop has finished.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .executor import RemoteExecutor
from .settings import HPCSettings
from .vlreview_job import run_vlreview


def build_vlreview_review_fn(
    executor: RemoteExecutor,
    settings: HPCSettings,
    *,
    cluster_user_dir: str,
    run_id: str,
    local_review_dir: Path | None = None,
    emit: Callable[[str, str, str], None] | None = None,
) -> Callable[[str], dict]:
    """Return a ``review_fn(local_pdf_path) -> review dict`` that stages the PDF to shared DFS,
    runs the VL review GPU job, and returns the parsed ``review.json``. On any failure it returns
    a permissive ``{"clean": True, ...}`` with a note, so a flaky GPU/queue never blocks the run
    or forces a pointless re-render — the deliverable still ships, just unaudited that pass."""

    state = {"pass": 0}

    def _review(local_pdf_path: str) -> dict:
        state["pass"] += 1
        n = state["pass"]
        base = f"{cluster_user_dir.rstrip('/')}/vlreview/{run_id}/pass{n}"
        remote_pdf = f"{base}/report.pdf"
        out_dir = f"{base}/out"
        try:
            executor.put_file(local_pdf_path, remote_pdf)
            result = run_vlreview(
                executor, settings, pdf=remote_pdf, out_dir=out_dir, emit=emit,
            )
        except Exception as exc:  # noqa: BLE001 - never block the deliverable on a review hiccup
            return {"clean": True, "defects": [], "fix_directives": [],
                    "note": f"vlreview unavailable ({type(exc).__name__}: {exc})"}

        # Best-effort: keep this pass's review.json in the run bundle for the audit trail.
        if local_review_dir is not None:
            try:
                local_review_dir.mkdir(parents=True, exist_ok=True)
                executor.get_file(result.review_json, str(local_review_dir / f"visual_review_pass{n}.json"))
            except Exception:  # noqa: BLE001 - the parsed review is already in hand
                pass
        return result.review

    return _review
