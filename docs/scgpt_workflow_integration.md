# scGPT workflow integration — design & gap analysis

**Status:** design only (no code yet). **Date:** 2026-06-17.
**Source workflow:** `~/Downloads/scGPT_mwe/Prompt.mkd` — end-to-end scGPT cell-type
annotation demo (preprocess → inference → post-processing → manuscript).

## 0. Decision (2026-06-17)

**Deploy scGPT.** It is a different *kind* of model (a transformer over gene/expression
tokens, not an LLM — see §3.1), so Qwen cannot stand in for its per-cell, reference-atlas
label transfer. We will run it, but **fully contained in Singularity** so the lean main
line stays scanpy + self-hosted Qwen only.

GPU provisioning = **Route C: a separate, on-demand, short-lived GPU batch job** (not
co-located on Qwen's GPU, not a second persistent GPU). Rationale in §8.

Consequence: the §4 `run_scgpt_preprocess` tool (vocab alignment) is now **justified** —
scGPT is its consumer.

## 1. Goal & framing

Bring the scGPT reference workflow into our tool registry while keeping the **Python main
line free of `scgpt`/`torch`** — those live only inside a Singularity `.sif` invoked as a
batch job. This preserves the lean, self-hosted-Qwen direction ([architecture-direction],
handoff "no `BioToolRuntime`") even though we now run a foundation model.

Most of the reference is still reproducible on plain scanpy; only step-2 inference needs
the container. **Everything except step-2 model inference is pure scanpy/pandas.** The
reference's own preprocessing class (`scGPT_mwe/scGPT_refactor/utils/misc.py::Preprocessor`)
calls only `sc.pp.filter_genes / filter_cells / normalize_total / log1p /
highly_variable_genes` — no scgpt, no torch. Value binning (the one scGPT-specific
transform) happens at **model-input time** (`utils/data_collator.py`, `n_bins=51`), i.e.
inside inference — so a preprocessing tool does not need it.

## 2. Step-by-step gap map

| Reference step | Needs scGPT? | Pure-scanpy reproducible? | Our status |
|---|---|---|---|
| **step1 preprocess** (vocab align + normalize + log1p + seurat_v3 HVG) | **No** | **Yes** | ⚠️ partial — `run_scanpy_qc` does normalize/log1p/HVG but **no vocab alignment**, default HVG flavor (not seurat_v3) |
| **step2 inference** (`TransformerModel`, `GeneVocab`, `torch.load(best_model.pt)` → `predictions.csv` + confidence) | **Yes — hard requirement** | No | ❌ none; out of scope by design |
| **addmetadata** (merge `predictions.csv` into `obs`) | No | Yes (pandas) | ❌ none |
| **leiden + UMAP** (`neighbors(use_rep='X_scVI')` → leiden → umap plots) | No | Yes | ✅ `run_clustering` (computes its own PCA neighbors; ref reuses precomputed `X_scVI`) |
| **umapby** (UMAP colored by each `obs` column) | No | Yes | ⚠️ partial — clustering emits one UMAP, not per-`obs`-column |
| **violin** (QC + marker-gene violins by `majorclass`) | No | Yes | ⚠️ partial — `run_de` emits a dotplot, no marker violins |
| **manuscript** (publication-ready report) | No | Yes | ✅ `tools/report.py` |

**One irreducible dependency:** step-2 inference. It loads the pretrained transformer
(`best_model.pt`) via `scgpt`'s `TransformerModel` + `GeneVocab` + `torch`. There is no
scanpy substitute — running scGPT *is* running scGPT. If we want model-based label
transfer we must either (a) install `scgpt`+`torch` on a GPU worker behind a tool, or
(b) keep our existing **marker-based** annotation (cluster → DE → Qwen interprets markers),
which is a different, model-free methodology we already lean toward
([report-and-execution-direction], handoff line 213: "marker→celltype annotation tool").

## 3.1 Why Qwen can't substitute for scGPT

scGPT is **not an LLM**. It is a transformer whose tokens are **genes** (plus binned
expression values), whose input is a cell's **numeric expression vector**, and whose
output (in this setup) is one of 123 reference cell-type classes + a confidence. `Qwen3.6`
is a transformer over **text**; it cannot ingest an expression matrix, and feeding 50k
cells × 5k genes as text is nonsensical. The only shared trait is the transformer
architecture (the "GPT" in both names).

So "annotation" splits into two non-interchangeable mechanisms:
- **scGPT** — per-cell, embedding-based label transfer from a reference atlas (fixed
  taxonomy, data-calibrated confidence). Needs the model + torch + GPU.
- **Qwen marker path** (what we already have) — cluster → DE markers → Qwen reasons over
  the marker *symbols* (text) to name each cluster. Cluster-granularity, flexible
  taxonomy, interpretable, no extra GPU.

They are kept as **alternative** annotation routes, not duplicate coverage: scGPT for
per-cell calibrated transfer, the marker path as the model-free default/fallback.

## 3.2 What "vocab alignment" actually is (the rigorous bit we lack)

The reference keeps only genes present in the model's gene vocabulary
(`download/reference_model/vocab.json`), then reports the match rate:

```python
adata.var["id_in_vocab"] = [1 if g in vocab else -1 for g in adata.var[gene_col]]
adata = adata[:, adata.var["id_in_vocab"] >= 0]
logger.info(f"match {n_kept}/{n_total} genes in vocabulary of size {len(vocab)}")
```

`vocab.json` is **just a `{gene_symbol: id}` JSON file** — loading it and filtering
`adata.var` by membership is `json.load` + a boolean mask. It does **not** require the
`scgpt` package (the reference uses `scgpt.GeneVocab.from_file`, but that is only a JSON
reader). So vocab alignment is a *data-file* dependency (ship/point at a `vocab.json`),
not a *package* dependency.

This is the single feature that makes the reference preprocessing "rigorous" relative to
ours: it guarantees the gene space matches a reference, and it reports the overlap honestly
(acceptance criterion: "report the overlap count and any dropped cells").

## 4. Proposed tool — `run_scgpt_preprocess` (no scgpt dependency)

A new tool in the `scrna_pack` style (lazy scanpy import, `_missing()` fallback,
ctx-driven `work/figures/tables`, deterministic, returns a dict + writes artifacts).
Registered as its own provider in `agents/registry.py` so the System page renders it.

**Inputs (tool args, all optional with sane defaults):**
- `vocab_path` (str | None) — path to a reference `vocab.json`. If `None`, skip alignment
  and behave as a rigorous generic preprocess (degrade gracefully, log that alignment was
  skipped — no silent no-op).
- `n_hvg` (int, default 5000), `hvg_flavor` (default `"seurat_v3"`),
  `normalize_total` (default 1e4), `log1p` (default True),
  `filter_gene_by_counts` / `filter_cell_by_counts` (default 0 = off).

**Pipeline (faithful to `Preprocessor` + step1):**
1. Read dataset h5ad (reuse `_read_anndata`).
2. **Vocab alignment** (if `vocab_path`): `json.load`, mark `id_in_vocab`, subset
   `adata[:, mask]`, record `genes_matched / genes_total / vocab_size`.
3. `check_logged` guard (ref's heuristic: max>30 or nonzero-min≥1 ⇒ not logged) to avoid
   double-log1p.
4. `sc.pp.filter_genes` / `filter_cells` (only if thresholds > 0).
5. `sc.pp.normalize_total(target_sum=normalize_total)` → `sc.pp.log1p`.
6. `sc.pp.highly_variable_genes(n_top_genes=n_hvg, flavor=hvg_flavor, subset=True)`.
7. Write `work/adata_scgpt_preprocessed.h5ad`, a `tables/vocab_match.csv` provenance row,
   and a `work/preprocess_config.json` (params actually used — our analogue to the
   reference `train_args.yml`; JSON avoids a new yaml dep though `pyyaml` is already
   present).

**Returns:** `{n_cells, n_genes_in, n_genes_after_vocab, n_hvg, genes_matched,
vocab_size, log1p_applied, artifacts:{...}}` — every number traceable to a real output,
matching the report's "no fabricated numbers" rule.

**New dependency (one, real):** `seurat_v3` HVG in scanpy requires **`scikit-misc`**,
which is **not** in our `analysis` extra today (`skmisc` import fails in the current venv).
Either add `scikit-misc>=0.3` to `[analysis]`, or default `hvg_flavor="seurat"` (scanpy
built-in, no extra) and make `seurat_v3` opt-in with a `_missing("scikit-misc")` guard.
Recommended: add it to `[analysis]` so we match the reference exactly.

## 5. Optional follow-on tools (also no scgpt)

Small, all pure scanpy/pandas; build only if we want fuller report parity:
- `merge_predictions` — pandas-join a `predictions.csv` (barcode,prediction,confidence)
  into `obs`; report overlap count + dropped cells (the reference's required-fix step).
  *Only useful once something produces predictions — i.e. after step-2 exists.*
- `umap_by_obs` — `sc.pl.umap` colored by each `obs` column.
- `marker_violins` — `sc.pl.violin` of canonical markers grouped by a label column.

`leiden+umap` and the manuscript are already covered by `run_clustering` + `tools/report.py`.

## 6. Build plan (ordered)

1. **`run_scgpt_preprocess`** (§4) + add `scikit-misc` to `[analysis]` +
   `tests/test_scgpt_preprocess.py` (tiny synthetic AnnData + a 3-gene `vocab.json`,
   asserting the off-vocab gene is dropped and the match count is reported). Pure scanpy,
   runs on CPU; produces the vocab-aligned h5ad scGPT consumes.
2. **scGPT `.sif` image** (scgpt + torch + the reference weights) staged on dfs3b — the
   only heavy artifact, contained, never in our Python env. See §8.
3. **`run_scgpt_annotate`** — the GPU batch-job tool (§8): submit step-2 inference via
   `gateway/slurm_job.py` with `--gres=gpu:1`, wait, read `predictions.csv` back, merge
   per-cell `predictions`/`confidence` into `obs`. Offline-testable through the
   `RemoteExecutor` protocol + a mock host (no real GPU in CI).
4. **`HPCSettings`** GPU-job fields (partition/gres/mem/time/image path) + a frontend
   note that the annotation run may queue for a GPU.
5. **Defer** the §5 post-processing tools (umap-by-obs, marker violins) until wanted for
   figures; `run_clustering` + `tools/report.py` already cover leiden+UMAP and the
   manuscript.

## 8. Deployment — Route C (separate short-lived GPU batch job)

**Why not the alternatives.** (A) Co-locating scGPT on Qwen's GPU: the vLLM serve job
claims most VRAM via `gpu-memory-utilization`; adding torch+scGPT risks OOM and means
`srun --overlap`-ing a batch job into a persistent server — fragile. (B) A second
*persistent* GPU (`gres=gpu:2`): holds two scarce A100s for the whole session when scGPT
is needed only for the minutes of inference, and 2-GPU allocations queue slower.

**Why Route C fits.** scGPT step-2 is one-shot: load `best_model.pt` → infer query h5ad →
write `predictions.csv` → exit (minutes). That is exactly the **on-demand batch** shape of
the existing [`gateway/slurm_job.py`](../src/bioagent/gateway/slurm_job.py) engine
(`singularity_exec` + `build_analysis_script` + submit→RUNNING→COMPLETED→read-back),
which is already distinct from the persistent `gpu.ensure_serve_job` vLLM server. We reuse
it with `--gres=gpu:1` and the scGPT `.sif`.

**User-steps = zero extra (the key point).** HPC3 auth is a *single* UCInetID + password +
Duo push that opens one persistent SSH session ([`ssh_gateway.py`](../src/bioagent/gateway/ssh_gateway.py)).
Every Slurm submission — the Qwen serve job *and* any analysis/scGPT batch job — reuses
that one session. **Number of jobs ≠ number of user authentications.** So the routing
choice is purely an engineering/cost decision, not a UX one; Route C adds no step the user
must take.

**Lifecycle.** `run_scgpt_annotate` (over `RemoteExecutor`): stage the vocab-aligned h5ad
to dfs3b → `sbatch` a `gpu:1` job that `singularity exec`s the scGPT `.sif` to run step-2
→ poll `squeue` to COMPLETED (cancel on timeout) → read `predictions.csv` back → merge
`predictions`/`confidence` into `obs`. GPU is released the moment inference exits. Fully
offline-testable via a mock host (no real GPU in CI), like `tests/test_slurm_job.py`.

## 9. Honest caveats

- This reproduces scGPT's **preprocessing**, not scGPT. Output of `run_scgpt_preprocess`
  is "data shaped the way scGPT expects," not annotations.
- Without a `vocab.json`, the tool is a rigorous generic preprocess — the *distinctive*
  scGPT-compat value (gene-space alignment) needs that data file.
- Numerical parity with the reference depends on identical scanpy versions and the
  `seurat_v3` flavor (hence `scikit-misc`); minor version drift can shift HVG selection.
