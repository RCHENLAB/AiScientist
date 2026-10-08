---
name: run_cellqc
summary: CellQC on a folder of 10x Cell Ranger libraries — ambient correction, filtering, doublets — then the merged, normalised checkpoint.
category: qc
runs_on: hpc:analysis
order: 205
line: scrna
owner: analysis
image: "bioconda:cellqc=0.3.6 r-doubletfinder python=3.12"
cpus: 16
mem_gb: 80
time_limit: "08:00:00"
---
# run_cellqc

Quality control for raw 10x Cell Ranger output, with [CellQC](https://github.com/lijinbio/cellqc)
(Jin Li; a Snakemake + R pipeline): SoupX ambient-RNA correction from the raw matrix,
count/feature/mitochondrial filtering, DoubletFinder doublet removal with scDblFinder as a second
opinion, and the nuclear fraction (intronic share of reads) from the BAM. It then merges the
libraries and writes the same checkpoint `run_scanpy_qc` writes, so every later tool reads it
unchanged.

## When the agent uses it

- The bound dataset is a **folder of Cell Ranger outputs**: one or more libraries, each with a raw
  AND a filtered feature-barcode matrix (`.h5`, an unpacked directory, or a 10x Cloud `.tar.gz`).
  The gateway recognises this layout at run start and tells the planner (`⚠ INPUT is a 10x Cell
  Ranger delivery …`).
- Single-nucleus or single-cell data alike; set `max_pct_mt` for nuclei (5 in the CellQC reference
  run) versus whole cells (CellQC's default 10).

## When the agent does NOT use it

- The input is one matrix or an `.h5ad` (no raw matrix): use `run_scanpy_qc`. Ambient correction
  needs the raw matrix, which such inputs do not carry.
- The input was already processed by CellQC (obs has `doubletfinder_class`, `nuclear_fraction`):
  QC is done; go to normalisation/clustering instead of filtering twice.
- Do not follow it with `run_doublet_detection`: doublets are already called and removed here
  (the obs keep both callers' scores).

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `species` | string (`auto`, `human`, `mouse`, `macaque`) | `'auto'` | the reference the libraries were aligned to; it decides the mitochondrial, ribosomal and hemoglobin gene sets. 'auto' reads it from Cell Ranger's web summary (Transcriptome), else from the gene names |
| `max_pct_mt` | number | `10.0` | drop a barcode whose counts are more than this percent mitochondrial. CellQC's default 10 suits whole cells; single NUCLEI should carry almost no mitochondrial signal, and the CellQC reference snRNA-seq run used 5 |
| `min_counts` | integer | `500` | drop a barcode with fewer total counts than this (counted after ambient correction) |
| `min_features` | integer | `300` | drop a barcode detecting fewer genes than this |
| `ambient_method` | string (`soupx`, `decontx`, `none`) | `'soupx'` | ambient-RNA correction APPLIED to the counts: soupx (estimated from Cell Ranger's clustering), decontx, or none |
| `ambient_compare` | string | `'decontx'` | a second ambient method that is estimated and reported but NOT applied, to show where the methods disagree; '' for none |
| `chemistry` | string (`auto`, `next_gem`, `gem_x`) | `'auto'` | the 10x chemistry, which sets the expected doublet rate: next_gem (3' v2/v3/v3.1, ~0.8% per 1,000 cells) or gem_x (3' v4, about half that). 'auto' reads it from Cell Ranger's web summary |
| `nreaction` | integer | `1` | 10x reactions (GEM wells) pooled into each library. It only changes the expected doublet rate (rate x cells / (nreaction x capacity)), describes how the libraries were made and is NOT measured from the data: confirm it with whoever prepared them |
| `doublet_decider` | string (`doubletfinder`, `scdblfinder`) | `'doubletfinder'` | the doublet caller whose calls REMOVE cells; the other one only scores them, and the two are compared with Cohen's kappa |
| `n_top_genes` | integer | `2000` | highly-variable genes kept for the embedding after the libraries are merged |
| `seed` | integer | `42` | random seed for CellQC's stochastic steps, so a rerun reproduces |
| `warn_ambient_frac` | number | `0.25` | flag a library whose mean SoupX contamination is above this. An ENGINEERING guard from the CellQC validation guide, not a literature threshold |
| `warn_retained_below` | number | `0.5` | flag a library that keeps less than this fraction of its Cell Ranger cells. An ENGINEERING guard: losing half a library usually means a threshold, or the library, is the problem |
| `samples` | array of string | — | only these libraries (by sample id = folder name); empty = all |
<!-- /generated:parameters -->

## Outputs

- `work/adata_qc.h5ad`: the merged libraries (obs `sampleid`), counts in `layers["counts"]`,
  log-normalised `.X` and `.raw`, highly-variable genes (per library when there are several). The
  obs keep CellQC's columns: `pct_counts_mt`/`_ribo`/`_hb`, the uncorrected `raw_*` metrics, both
  doublet callers' scores and classes, and `nuclear_fraction` when a BAM was present.
- `artifacts/cellqc/`: CellQC's own `report.html`, `report_slides.pdf`, `metrics.csv`,
  `qc_status.csv`, `manifest.tsv` and the per-library doublet summaries; `tables/cellqc_metrics.csv`;
  a per-library QC violin in `figures/`.
- Result: per-library cells in → after filtering → after doublets, which threshold removed them
  (`removed_only_by`), ambient load, doublet counts and caller agreement (kappa), nuclear fraction;
  the assumptions it made (species, chemistry, doublet rate, `nreaction`) and where each came from;
  and `warnings` — the CellQC validation checklist (a failed or fallback step, an excluded library,
  an inert mitochondrial filter, a library with low retention or high ambient load, poor doublet
  agreement).

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Its job runs in its OWN image instead of the line's: `bioconda:cellqc=0.3.6 r-doubletfinder python=3.12`, built on first use from conda-forge + bioconda (cellqc=0.3.6 r-doubletfinder python=3.12) into the shared containers directory on HPC3 and reused by every later run.

Slurm resources for its job: 16 CPUs, 80 GB, up to 08:00:00.

Needs: `scanpy`.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- CellQC does not re-call cells: Cell Ranger's cell calls are what enter, so over-calling shows up
  only as low retention.
- `nreaction` and the chemistry change the expected doublet rate and are not measured from the data.
  The chemistry is read from Cell Ranger's web summary when present; `nreaction` defaults to 1 and
  the result says so.
- One setting per run. Libraries of different species are refused (`species_by_library` says which
  is which; pass `samples` to QC one species at a time). Libraries of different chemistries run
  with the majority's doublet rate and a warning.
- The image pins Python 3.12. The BioContainers image (`quay.io/biocontainers/cellqc:0.3.6`) ships
  Python 3.14, on which CellQC's nuclear-fraction step fails (multiprocessing start-method change),
  so this tool builds its own image from the same bioconda packages.

## Code and tests

- `tool.py`: staging, configuration, the CellQC run, validation, and the merge. Library detection
  is `../_lib/cellranger.py`, shared with the gateway.
- Tests: `tests/test_run_cellqc.py`.

## What the model is told

<!-- generated:model-description -->
> CellQC quality control for a FOLDER of 10x Cell Ranger outputs (one or more libraries with raw
> AND filtered matrices): SoupX ambient-RNA correction, count/feature/mitochondrial filtering,
> DoubletFinder doublet removal (scDblFinder as a second opinion), and the nuclear fraction from
> the BAM. Merges the libraries (obs 'sampleid') and writes the normalized checkpoint the later
> tools read. Use it INSTEAD of run_scanpy_qc when the data are Cell Ranger outputs; do not run
> run_doublet_detection after it.
<!-- /generated:model-description -->
