---
name: run_gsea_prerank
summary: Preranked GSEA over every tested gene of each group, offline, against the same gene-set libraries.
category: analysis
runs_on: hpc:analysis
order: 250
line: scrna
owner: analysis
---
# run_gsea_prerank

Runs gseapy's preranked GSEA on the complete ranked gene list of each group (the `.rnk` files `run_de` and `run_pseudobulk_de` write), so it detects coordinated shifts that no per-gene cutoff would keep. It reports a signed NES (positive means up in that group), FDR and the leading-edge genes.

## When the agent uses it

- After `run_de`, as a complement to `run_enrichment`: the two use different null hypotheses, so disagreement between them is not an error.
- On ranked lists a `run_code` step wrote, through `input`.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `gene_sets` | array of string | — |  |
| `groupby` | string | — |  |
| `top_n_terms` | integer | — |  |
| `min_size` | integer | — |  |
| `max_size` | integer | — |  |
| `permutations` | integer | — |  |
| `fdr_max` | number | — |  |
| `seed` | integer | — |  |
| `input` | string | `''` | ranked gene list(s) to test INSTEAD of the rank files run_de wrote — a .rnk file (gene<TAB>score per line) or a file-name pattern such as `my_rank_*.rnk`, inside this run's work/ or artifacts/ directory. Each file is one group, named by its file stem, ranked by the score as given. Empty = run_de's rank files. |
<!-- /generated:parameters -->

## Outputs

- `tables/gsea_<group>.csv` per group.
- Result: the top terms per group, and the permutations, seed and gene-set size bounds used.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `gseapy`.

Reads private data: no.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Permutation-based and seeded; few permutations or small libraries give coarse FDR values.
- A group where nothing passes FDR is a null result to report, not a group to drop.

## Code and tests

- `tool.py`: `run_gsea_prerank` and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_pack.py`, `tests/test_tool_input.py`.

## What the model is told

<!-- generated:model-description -->
> Preranked GSEA (gseapy.prerank) over the COMPLETE ranked gene list per group — OFFLINE, against
> the same local .gmt files as run_enrichment. Reads the `tables/rank_<groupby>_<group>.rnk` files
> run_de writes (every tested gene, ranked by Wilcoxon z), so it detects coordinated shifts that
> no per-gene cutoff would keep, and returns a SIGNED NES (positive = up in that group) plus FDR
> and leading-edge genes. Writes `tables/gsea_<group>.csv` per group. Run AFTER run_de. This is a
> COMPLEMENT to run_enrichment, not a replacement: ORA thresholds a top-N list, GSEA walks the
> whole ranking, and the two use different null hypotheses — do NOT treat disagreement between
> them as an error, and do NOT drop a group because nothing passed FDR (report the null result).
<!-- /generated:model-description -->
