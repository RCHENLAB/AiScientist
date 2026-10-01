---
name: scgpt_annotate
summary: Per-cell cell-type labels with a confidence, transferred from a reference atlas by scGPT on a GPU.
category: annotation
runs_on: gpu:scgpt
entry: tool:make_scgpt_annotate_tool
needs: [runner=scgpt_runner]
order: 700
line: scgpt
owner: analysis
---
# scgpt_annotate

Transfers a cell-type label and a confidence to every cell of the loaded `.h5ad` from a reference atlas, with the scGPT foundation model (a gene-expression transformer, not an LLM) in a short GPU batch job.

## When the agent uses it

- When per-cell, reference-consistent labels are wanted, as opposed to naming clusters from markers.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `model_dir` | string | — | Optional override of the reference model dir on the cluster. |
<!-- /generated:parameters -->

## Outputs

- The location of the predictions and the label distribution; the per-cell labels and confidences are written by the job.

## Where it runs

<!-- generated:runs-on -->
As a GPU Slurm job inside `scgpt.sif`, through the runner the gateway injects (`gateway/scgpt_runner.py`). Without a GPU session it stays listed but reports that it is not enabled.

Needs: `scgpt-image`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Checked on mouse retina (2026-09-28) against a human retina reference: neuronal labels agreed closely, but glia and vascular cells did not (most Müller glia came back as astrocytes at high confidence). Check those classes against markers.
- The GPU job queues; the tool stays listed in sessions without a GPU and then reports that it is not enabled.

## Code and tests

- `tool.py`: the record and the dispatch to the injected runner.
- The job script and the inference itself are platform-side today (`gateway/scgpt_job.py`, `gateway/scgpt_runner.py`); moving them into this folder is follow-up work.
- Tests: `tests/test_scgpt_annotate.py`, `tests/test_preset_compose.py`.

## What the model is told

<!-- generated:model-description -->
> scGPT REFERENCE-BASED per-cell cell-type annotation (a pretrained foundation model, run on a
> GPU). Transfers a cell-type label + confidence to EVERY cell of the loaded .h5ad query from a
> reference atlas — distinct from marker-based cluster naming. Use when per-cell, reference-
> consistent labels with calibrated confidence are wanted. Runs a short GPU batch job (may queue).
> Returns the predictions location + a label distribution; does NOT fabricate cell types.
<!-- /generated:model-description -->
