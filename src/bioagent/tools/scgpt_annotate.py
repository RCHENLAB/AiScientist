"""The ``scgpt_annotate`` tool — scGPT per-cell, reference-based cell-type annotation.

scGPT is a gene/expression transformer (NOT an LLM), so this is a fundamentally different
capability from the marker-based annotation the Scientist does by reasoning over DE genes:
it transfers per-cell labels (+ data-calibrated confidence) from a pretrained reference
atlas. Inference runs as a short-lived ``gpu:1`` Singularity batch job on HPC3 (Route C),
orchestrated by ``gateway/scgpt_job.run_scgpt_inference`` — ``scgpt``/``torch`` live only in
the image, never here.

The heavy lifting (stage the dataset to dfs3b, submit the GPU job, read predictions back) is
an injected ``runner`` closure built by the gateway from the live SSH executor + HPC settings
— exactly how ``run_code`` gets its sandbox. Without a runner (offline, tests, no cluster
session) the tool reports ``not_enabled`` instead of pretending to annotate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..agents.research_harness import HarnessContext, HarnessTool

# (args, ctx) -> result dict. Built by the gateway; closes over the SSH executor + settings.
ScgptRunner = Callable[[dict[str, Any], "HarnessContext"], dict[str, Any]]


def make_scgpt_annotate_tool(runner: ScgptRunner | None) -> "HarnessTool":
    """Build the ``scgpt_annotate`` HarnessTool. ``runner`` is the gateway-injected remote
    executor of the GPU batch job; ``None`` => the tool is present but reports not-enabled."""
    from ..agents.research_harness import HarnessTool

    def _exec(args: dict[str, Any], ctx: "HarnessContext") -> dict[str, Any]:
        if runner is None:
            return {
                "status": "not_enabled",
                "note": (
                    "scGPT annotation needs a live HPC3 session + the scGPT Singularity image "
                    "(BIOAGENT_SCGPT_IMAGE). Not configured for this run."
                ),
            }
        dataset_path = str(ctx.decisions.get("dataset_path", "") or "")
        if not dataset_path:
            return {"status": "error", "error": "no dataset loaded for this run."}
        if not dataset_path.endswith(".h5ad"):
            return {
                "status": "not_applicable",
                "error": "scGPT requires an AnnData .h5ad query dataset; this run's dataset "
                f"is not .h5ad ({dataset_path}).",
            }
        return runner(args, ctx)

    return HarnessTool(
        name="scgpt_annotate",
        description=(
            "scGPT REFERENCE-BASED per-cell cell-type annotation (a pretrained foundation "
            "model, run on a GPU). Transfers a cell-type label + confidence to EVERY cell of "
            "the loaded .h5ad query from a reference atlas — distinct from marker-based "
            "cluster naming. Use when per-cell, reference-consistent labels with calibrated "
            "confidence are wanted. Runs a short GPU batch job (may queue). Returns the "
            "predictions location + a label distribution; does NOT fabricate cell types."
        ),
        parameters={"type": "object", "properties": {
            "model_dir": {"type": "string",
                          "description": "Optional override of the reference model dir on the cluster."}}},
        executor=_exec,
        reads_private_data=True, category="annotation", requires=("scgpt-image",),
    )
