# vlreview — render-level VL review (Route C GPU batch job)

Qwen3.6 (the main LLM) is **text-only**: it can audit the numbers behind a chart, but it
cannot *see* render defects — text-over-text overlap, a caption printed on the figure,
clipped cells, a table that overran its box. Those exist only in the final rendered pdf, and
only a vision model finds them. This kit runs a **separate small VL model** (Qwen2.5-VL-7B)
that looks at the rendered pages and reports layout defects, so the render step can **rework
the formatting and re-render**.

Same shape as `deploy/scgpt/` — a short-lived, on-demand `gpu:1` batch job, fully contained
in a `.sif`, **not** co-located on Qwen's vLLM GPU and **not** a persistent second GPU.

## Files
- `vlreview.def` — Singularity image (transformers + qwen-vl-utils + PyMuPDF). Weights are
  **not** baked in — bind-mounted read-only at run time.
- `run_review.py` — in-container CLI: rasterize pdf → deterministic bbox-overlap pre-check
  (no GPU) → Qwen2.5-VL page review → `review.json` (defects + fix directives).

Build + weight staging is driven by `scripts/hpc3_vlreview_setup.sh` (mirrors
`scripts/hpc3_vllm_setup.sh` — RCIC compute-node build, cache off `$HOME`, typed-gres probe).

## Gateway / loop
- `src/bioagent/gateway/vlreview_job.py` — submits + supervises the batch job (Route C).
- `src/bioagent/tools/visual_review.py` — the render → review → **re-render with escalated
  format** loop; residual defects go to the technical-report Diagnostics only.
- `src/bioagent/tools/report.py` — `build_pdf_report(format_overrides=...)` are the knobs the
  loop escalates (table font, body font, margins, table-wrap threshold, landscape, fig width).

## Build + stage (on HPC3 — macOS cannot build .sif)
```bash
# 0. get the kit onto HPC3 (the cluster can't git-pull the private repo).
#    Transfers go through access-hpc3, NOT a login node (RCIC 2026-08-06); same $HOME either way.
rsync -av deploy/vlreview/ scripts/hpc3_vlreview_setup.sh <ucinetid>@access-hpc3.rcic.uci.edu:~/vlreview-build/
# 1. on a COMPUTE node (RCIC forbids login-node builds):
ssh <ucinetid>@hpc3.rcic.uci.edu && newgrp ruic20_hpc
srun -c 4 -p free --time=2:00:00 --pty /bin/bash -i
cd ~/vlreview-build && bash hpc3_vlreview_setup.sh
#   custom .def has no fakeroot on HPC3 (no /etc/subuid entry) -> it builds via --remote
#   (Sylabs): `singularity remote login` once, then BIOAGENT_VLREVIEW_BUILD_MODE=remote.
```
Produces `…/containers/vlreview.sif` + `…/vlreview_model/` on dfs3b.

## Enable (opt-in)
Defaults already match the RUIC20 cluster (paid `gpu` partition, `gpu:A30:1`, image/model at
the dfs3b paths above), so on the eye server you only need:
```bash
export BIOAGENT_VLREVIEW_ENABLED=1
# override only if needed, e.g. a different card: BIOAGENT_VLREVIEW_GRES='gpu:RTX6000:1'
```

## Cost
Billed by time to `ruic20_lab_gpu` on the paid `gpu` partition (bought priority beats the
slow-queuing free partitions). One short (~30 min, `vlreview_time_limit`) job on a cheap 24GB
**A30** (7B VL ≈ 16GB). Do NOT use an A100 for layout review; V100 (16GB, no flash-attn) is
too tight. `free-gpu`/`free-gpu32` (A30/RTX6000) exist for a zero-charge override if wanted.
