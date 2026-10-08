---
name: run_enrichment
summary: Over-representation analysis of DE genes against local gene-set libraries, with the genes actually tested as the background.
category: analysis
runs_on: hpc:analysis
order: 230
line: scrna
owner: analysis
---
# run_enrichment

Asks which pathways and GO terms the genes from a DE step over-represent, with gseapy's `enrich` against local `.gmt` libraries (no Enrichr web call: the analysis jobs have no network). The background is the set of genes the DE step tested, read from its `de_<key>_universe.txt`.

## When the agent uses it

- After `run_de` or `run_pseudobulk_de`. Called without `genes`, it finds the DE tables itself, per group.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `top_n_genes` | integer | `0` | optional cap on how many genes enter the over-representation test. 0 means no cap — use every gene passing the thresholds below, which is the standard way ORA is run. Set it only to deliberately shorten a very long list, and say that you did |
| `top_n_terms` | integer | `10` | how many enriched terms to report per group |
| `padj` | number | `0.05` | adjusted-p cutoff a DE gene must clear to enter the test — the same Benjamini-Hochberg cutoff run_de applies |
| `lfc` | number | `0.25` | minimum \|log2 fold-change\| a DE gene must clear to enter the test. Pairing an effect-size floor with the p-value cutoff is what keeps a list of thousands of barely-changed genes from returning only large generic terms; 0.25 is the single-cell convention |
| `gene_sets` | array of string | — | local gene-set libraries to test against |
| `background` | integer | — | ORA background size; omit to use the tested universe |
| `split_direction` | boolean | — | test up- and down-regulated genes separately |
| `groupby` | string | — | which DE table to read, by the key in its file name tables/de_<groupby>_all.csv: the column run_de grouped by (e.g. leiden, majorclass), not the `group` column inside the table; omit to find the table automatically |
| `genes` | array of string | — | explicit gene list; omit so the DE tables are found instead |
<!-- /generated:parameters -->

## Outputs

- `tables/enrichment_<key>.csv` per group and bar plots in `figures/enrichment_<key>.png`.
- Result: the top terms per group, `background_size`, `background_source`, `warnings`, and the DE step's `inference` label.

## Where it runs

<!-- generated:runs-on -->
As a Slurm CPU job inside `analysis.sif` on HPC3, through the analysis line's job entry point `python -m aiscientist.tools.scrna_cli`, reading and writing the run's `work/` and `artifacts/` on dfs3b. When the session has no HPC3 executor it runs in the gateway process.

Needs: `gseapy`.

Reads private data: no.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Only the libraries in `tools/genesets/` exist (`describe_environment` lists them). The `.gmt` files are not in git; deploys keep them in place.
- A cut list throws away ranking information; `run_gsea_prerank` is the whole-ranking complement.
- Marker tables from `run_de` are the top `n_genes` per group, so on large data every row passes the gate and the input is that top-N, not the significant set. The result then says so in `warnings` and `selection.upstream_table`, with the true counts from `de_<key>_significance.json` (or, for older runs, inferred from every row passing).
- Without per-group DE tables it falls back to one pooled list and a fixed 20,000-gene background, which inflates p-values; the result warns when that happens.

## Code and tests

- `tool.py`: `run_enrichment`, the input discovery and the record. Shared helpers and the declared-parameter table (`PARAMS`, `TOOL_SUMMARY`) are in `../_lib/scrna.py`; the defaults listed above come from that table, and `tests/test_declared_params.py` checks that the preset protocols state the same values.
- Tests: `tests/test_scrna_pack.py`, `tests/test_deg_contrast.py`, `tests/test_descriptive_de_enrichment.py`.

## What the model is told

<!-- generated:model-description -->
> Over-representation / pathway enrichment — OFFLINE ORA against LOCAL gene-set (.gmt) files
> (gseapy.enrich), NOT the Enrichr web API (the analysis host has no network). Automatically reads
> the top DE genes from this run's DE table (`tables/de_<groupby>_all.csv`, e.g.
> de_majorclass_all.csv) and runs ORA PER GROUP, writing one enrichment table + bar plot per cell
> class and returning the top enriched terms per group. Do NOT pass a pooled `genes` list — that
> collapses every class into a single 'input' group and loses the per-class pathway biology. Run
> AFTER run_de; it picks up whatever key run_de used (annotated classes preferred over raw
> leiden). When the DE table is a CONTRAST (it carries both up- and down-regulated significant
> genes) each group is split into '<group> (up)' and '<group> (down)' and enriched separately, so
> a down-regulated programme cannot be cancelled out by an up-regulated one; `split_direction:
> false` disables that. `gene_sets` are library names resolved to local .gmt files (default
> GO_Biological_Process_2023 / Reactome_2022 / MSigDB_Hallmark_2020).
<!-- /generated:model-description -->
