---
name: explore-atlas-expression-with-aiscientist-mcp
description: Answer a gene expression query such as "show LRAT across retinal cell types" on a large single-cell or single-nucleus atlas .h5ad with raw counts, by calling the atlas-expression-exploration MCP server from the aiscientist-mcp repository. Its tools resolve gene names (case, HGNC alias, previous symbol, ID), run the full-atlas step as a Slurm job that streams the counts and needs little memory, and return donor-aware summary tables at major-class, cell-type and region-by-class resolution, scanpy dot, violin and UMAP feature plots, and a short Markdown report. Use when asked where a gene is expressed in an atlas, to compare a few genes across annotated cell types, to produce standard expression figures for a gene list, or to install or register this MCP server. Not a differential expression test.
license: MIT
compatibility: Requires conda or mamba with the aiscientist-mcp packages (common, atlas-expression-exploration) installed from GitHub, or apptainer with the pinned server image, and an MCP client (Claude Code or any stdio MCP client) or the bundled aiscientist-mcp CLI client. The full-atlas step streams the count matrix in chunks (2.2 GB peak for 8.3e9 non-zeros; server 0.0.3 and earlier loaded it whole, 218 GiB); Slurm optional. Network only to install and for the one-time HGNC download.
metadata:
  category: single-cell
  version: "0.0.2"
  status: community
  author: "Jin Li"
  tags: "scrna-seq, snrna-seq, gene-expression, mcp, scanpy, dot-plot, violin-plot, umap, cell-atlas, retina"
  tested-with: "aiscientist-mcp common 0.0.2, atlas-expression-exploration 0.0.2 (conda), 0.0.3 (image) and 0.0.4 (image, with common 0.0.4, on the full atlas), mcp 2.3.0, scanpy 1.12.5, anndata 0.13.4, Python 3.13.18, Claude Code 2.1.309"
  compute: "full-atlas step with server 0.0.4: Slurm job, 4 CPUs, 2.2 GB peak, 8 min for 3.18M nuclei (0.0.2 loaded the atlas whole: 218 GiB peak, 10.5-12.5 min); plot and report calls 5-9 min each from a conda env on a network filesystem (mostly server start and imports), 9-19 s each in the image on a 40,000-nucleus subset"
  image: "docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:725b31a8cc4f19472144bd6058d9736823718a753a3edf68b1e279f497a6fbaf"
  cpus: "4"
  mem_gb: "32"
  time_limit: "04:00:00"
  requires-gpu: "false"
  scheduler: "optional"
  requires-network: "install"
  requires-secrets: "none"
---

# Explore gene expression in an atlas with the atlas-expression-exploration MCP server

The [aiscientist-mcp](https://github.com/lijinbio/aiscientist-mcp) server
`atlas-expression-exploration` exposes the gene-expression query as MCP tools. This skill holds no
analysis code: every step is a tool call.

- In AiScientist, use the typed tools in [On AiScientist](#on-aiscientist).
- In another MCP client such as Claude Code, call the server's tools directly.
- In a shell or batch script, use the bundled client:
  `aiscientist-mcp --server atlas-expression-exploration-mcp call <tool> '<json>'`.

Only one step is long. `submit_query` reads the whole atlas's counts in chunks, keeping the query
genes and each cell's total, as a Slurm job, and writes a small `query.h5ad` (all cells, query genes only) plus the tables.
The plot and report tools read only that file.

The server was tested end to end on the Human Retina Cell Atlas snRNA-seq object:

- 3,177,310 nuclei x 36,601 genes, raw counts in `X` (CSR, 8.30e9 non-zeros, 19.5 GB gzip h5ad);
- 10 major classes, 123 cell types, 3 retinal regions, 104 donor labels.

The tested runs:

- **Full atlas, conda env, Slurm.** The LRAT query through §5 with the one-shot CLI (every call
  starts the server), with common 0.0.2 and atlas-expression-exploration 0.0.2. The tables and
  PNGs were identical to those of an earlier commit and agreed with a direct scanpy run to a
  relative 3.7e-6. Version 0.0.3 changes only the image font, and its install command (§1) was
  run as written.
- **Image, apptainer.** The four-gene query `LRAT,CRALBP,rho,ABCR` on a 40,000-nucleus subset,
  with one `apptainer exec --containall` per call and `AISCI_EXECUTOR=inline`. The tables and the
  PNGs were identical to those of the conda env.
- **Full atlas, image 0.0.4, apptainer in a Slurm job.** The query `RPE65 LRAT rho` on the full
  object, one `apptainer exec --containall` with `AISCI_EXECUTOR=inline` in a 4-CPU job: 7 min 59 s
  and 2.2 GB peak (Slurm MaxRSS). The LRAT and RHO rows of `summary_majorclass.tsv` equal those
  written by 0.0.3 for the same atlas in every numeric column (20 rows). The conda install of
  §1 was not run again with the 0.0.4 tags.

Replace every `/path/to/...`, `<partition>` and `<account>` with your own values.

| Tree | Holds | Example |
| --- | --- | --- |
| server install | a conda env with the two aiscientist-mcp packages | env `visionkb_mcp` |
| data dir (`AISCI_DATA_DIR`) | `atlas-expression-exploration/<query_id>/`: `query.h5ad`, tables, figures, report, job record | `/path/to/data/aiscientist-mcp` |

The atlas is only read, never written.

## When to use

- "Show gene X (and Y) across cell types or classes" in an annotated atlas with raw counts: a dot
  plot, a violin plot, a UMAP feature plot, and a citable table.
- Checking where a candidate gene is expressed before a donor-aware DE or a follow-up experiment.
- Installing, registering or troubleshooting the `atlas-expression-exploration` MCP server.

## When NOT to use

- Comparing conditions or cell types statistically: use a donor-aware pseudobulk DE. The
  percentages and means here are descriptive.
- An atlas whose `X` is normalized and has no raw counts: the normalization assumes raw counts.
- Many genes or a signature: score the gene set instead.

## On AiScientist

AiScientist's pinned MCP bridge exposes typed tools when this skill is installed and allowed,
its image matches the server's digest pin, and the deployment has an approved atlas configured.
Attach that atlas or its authorized session copy, then call:

`resolve_genes` → `submit_query` → `query_status` → `get_summary`, `plot_dotplot`,
`plot_violin`, `plot_featureplot` → `atlas_write_report(query_id)`.

Use the returned `query_id` for results and report calls. `list_queries` lists previous queries
in this run. `atlas_write_report` is the AiScientist name for the server's `write_report` tool;
it creates the query's `report.md`. AiScientist's separate `write_report` plan step requests the
whole run's manuscript. Use `atlas_write_report` for the Atlas query report. The generic MCP
and CLI examples below retain the server's name `write_report`.

The bridge supplies the approved dataset mount, `ATLAS_H5AD`, `AISCI_DATA_DIR`,
`AISCI_EXECUTOR=inline`, job resources and artifact collection. Pass `genes` unchanged; if
specifying `atlas`, use the attached dataset or its authorized source. The bridge validates
that path and omits it from the MCP arguments so the server reads the bound session copy.
`submit_query` finishes inside its outer CPU allocation before returning. Figures,
`summary_*.tsv`, `gene_resolution.tsv` and `report.md` appear in the run's
`gene_expression/<query_id>/` artifacts.

Direct `run_in_environment` shell calls for this skill are refused. If the typed tools are
unavailable, check the deployed bridge, skill/image binding and approved atlas configuration.
The installation, environment and client setup in §§1–3 below are for standalone clients;
AiScientist supplies those settings through its deployment.

## 1. Install the server

The install is pinned to release tags of `lijinbio/aiscientist-mcp`: `common-v0.0.4` and
`atlas-expression-exploration-v0.0.4`.

```bash
mamba create -y -n visionkb_mcp -c conda-forge python=3.13.18 scanpy=1.12.5 anndata=0.13.4 pyarrow=26.0.0 mcp=2.3.0 pytest ruff

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate visionkb_mcp
for t in common-v0.0.4 atlas-expression-exploration-v0.0.4; do
  pip install --no-deps --force-reinstall "git+https://github.com/lijinbio/aiscientist-mcp@${t}#subdirectory=packages/${t%-v*}"
done
aiscientist-mcp --server atlas-expression-exploration-mcp tools   # lists the 9 tools below
```

The solve resolved to numpy 2.5.4, pandas 3.1.0, scipy 1.18.1, matplotlib 3.11.2, seaborn 0.13.2
and h5py 3.16.0. To run the server's tests, clone the repository and run
`python -m pytest packages` (10 tests; about 5 min on a network filesystem, most of it importing
scanpy).

**Container.** The same server as a pinned image, with Nimbus Sans for the Helvetica figure text.
With `--containall` the settings of §2 pass as `APPTAINERENV_<name>`, and each call is a fresh
container, so use `AISCI_EXECUTOR=inline` inside a Slurm job:

```bash
apptainer pull mcp-atlas-expression-exploration_0.0.4.sif docker://ghcr.io/lijinbio/mcp-atlas-expression-exploration@sha256:725b31a8cc4f19472144bd6058d9736823718a753a3edf68b1e279f497a6fbaf
export APPTAINERENV_ATLAS_H5AD=/path/to/atlas.h5ad APPTAINERENV_AISCI_DATA_DIR=/path/to/data/aiscientist-mcp APPTAINERENV_AISCI_EXECUTOR=inline
S="apptainer exec --containall --bind /path/to mcp-atlas-expression-exploration_0.0.4.sif aiscientist-mcp --server atlas-expression-exploration-mcp"
$S tools
```

Then use this `$S` in §5. The image runs each call in 9-19 s on a 40,000-nucleus subset, against
minutes for a conda env on a network filesystem, where importing scanpy alone took 1-2.5 min.

## 2. Configure

The server reads its settings from environment variables. Set them where you register it (§3),
or export them before using the CLI.

| Variable | Meaning | Tested value |
| --- | --- | --- |
| `ATLAS_H5AD` | default atlas `.h5ad`: raw counts in `X`, `obsm["X_umap"]` | `/path/to/atlas.h5ad` |
| `AISCI_DATA_DIR` | data root; this server writes under `<root>/atlas-expression-exploration/` | `/path/to/data/aiscientist-mcp` |
| `ATLAS_HGNC` | HGNC complete set; downloaded on first use if missing | `<root>/atlas-expression-exploration/hgnc_complete_set.txt` |
| `ATLAS_OBS_COLS` | obs columns: class, cell type, region, donor, then extras | `majorclass,celltype,tissue,donor,sampleid` |
| `ATLAS_CLASS_ORDER` | display order of classes; others are appended alphabetically | `Rod,Cone,HC,BC,AC,RGC,MG,Astrocyte,Microglia,RPE` |
| `AISCI_EXECUTOR` | `slurm` (sbatch), `local` (background process on this host) or `inline` (foreground; the call returns when the step ends) | `slurm` |
| `AISCI_SLURM` | sbatch options of the first attempt | `--partition=<partition> --account=<account> --cpus-per-task=4 --mem=32G --time=04:00:00` |
| `AISCI_SLURM_FALLBACK` | sbatch options after a preemption, e.g. a non-preemptible partition | same, with the fallback partition |
| `AISCI_JOB_PREFIX` | optional; prepended to the job command, e.g. `apptainer exec --bind /data image.sif` | unset |

Check that the class, cell-type, region and donor columns have no missing values. Cells with
missing labels drop out of every group silently. Choose the executor by how the server is called:

- `slurm` for a server on a login node, persistent (an MCP client) or one-shot (the CLI).
- `local` only for a persistent server that itself runs on a compute node: the job is a
  detached child of the server and ends if the server or its container exits.
- `inline` when every call is a fresh process or container on a compute node, e.g. one
  `apptainer exec` per call; `submit_query` then blocks until the step ends.

## 3. Register the server

**Claude Code.** This is the command that was tested. The `timeout` (ms) covers the plot tools on
millions of cells:

```bash
claude mcp add-json --scope project atlas-expression-exploration '{"type":"stdio","command":"/path/to/envs/visionkb_mcp/bin/atlas-expression-exploration-mcp","env":{"ATLAS_H5AD":"/path/to/atlas.h5ad","AISCI_DATA_DIR":"/path/to/data/aiscientist-mcp","AISCI_SLURM":"--partition=<partition> --account=<account> --cpus-per-task=4 --mem=32G --time=04:00:00","AISCI_SLURM_FALLBACK":"--partition=<partition> --account=<account> --cpus-per-task=4 --mem=32G --time=04:00:00"},"timeout":900000}'
claude mcp get atlas-expression-exploration   # project scope: "Pending approval" until you run `claude` and approve
```

`--scope project` writes `.mcp.json` in the current directory, which can be committed for a team.
Use `--scope user` for yourself everywhere. **Other MCP clients:** put the same object under
`"mcpServers": {"atlas-expression-exploration": {...}}` in the client's configuration.

## 4. Tools

| Tool | Arguments | Returns | Time (tested) |
| --- | --- | --- | --- |
| `resolve_genes` | `genes` (comma- or space-separated), `atlas` | `resolution`: query, atlas_gene, method, candidates | seconds |
| `submit_query` | `genes`, `atlas`, `rerun` | `query_id`, `state`, `job_id`, `resolution` (returns at once, or at the end with `inline`; reuses a finished or running query) | seconds |
| `query_status` | `query_id` | `state` (PENDING, RUNNING, COMPLETED, FAILED), `job_id`, `note`, `elapsed_s` | seconds |
| `get_summary` | `query_id`, `level` (`majorclass`, `celltype`, `region_class`), `gene`, `top` | `rows` ranked by pseudobulk CP10k, `table` path | seconds |
| `plot_dotplot` | `query_id` | `files`: `dotplot_<class>.pdf/.png`, `dotplot_<celltype>.pdf/.png` | 6-9 min* |
| `plot_violin` | `query_id`, `groupby` | `files`: `violin_<groupby>.pdf/.png` | 7 min* |
| `plot_featureplot` | `query_id` | `files`: `featureplot.pdf/.png` | 6-7 min* |
| `write_report` | `query_id` | `file`, `markdown` | 5-6 min* |
| `list_queries` | none | `queries`: query_id, state, genes | seconds |

"seconds" is the tool's own work. \* Full atlas, one-shot CLI call from a conda env on a network
filesystem. Most of that time is starting the server and importing scanpy, which every one-shot
call pays: there, even the first `submit_query` took 6.5 min to reach `sbatch`. A persistent MCP
session pays it once, and the image ran each call in 9-19 s on the subset.

The tools behave as follows:

- `query_status` resubmits a preempted Slurm job with `AISCI_SLURM_FALLBACK` and says so in
  `note`.
- Every tool rejects a `query_id` outside the data directory.
- The plot and report tools refuse to run until the query is `COMPLETED`.

## 5. Run a query

These are the calls of the tested run, in order. Each is shown as the MCP tool call and its
shell form. In Claude Code, the same calls are the tools
`mcp__atlas-expression-exploration__resolve_genes`, `..._submit_query`, and so on, with the same
JSON arguments.

```bash
S="aiscientist-mcp --server atlas-expression-exploration-mcp"

# 1. Check the names first (seconds; a typo or an ambiguous alias shows up here).
$S call resolve_genes '{"genes": "LRAT"}'

# 2. Start the full-atlas step; note the query_id (here HRCA_snRNA_allcells__LRAT).
$S call submit_query '{"genes": "LRAT"}'

# 3. Poll until COMPLETED (tested with 0.0.4: 8 min, 2.2 GB peak).
$S call query_status '{"query_id": "HRCA_snRNA_allcells__LRAT"}'

# 4. Results.
$S call get_summary '{"query_id": "HRCA_snRNA_allcells__LRAT", "level": "majorclass", "top": 3}'
$S call plot_dotplot '{"query_id": "HRCA_snRNA_allcells__LRAT"}'
$S call plot_violin '{"query_id": "HRCA_snRNA_allcells__LRAT"}'
$S call plot_featureplot '{"query_id": "HRCA_snRNA_allcells__LRAT"}'
$S call write_report '{"query_id": "HRCA_snRNA_allcells__LRAT"}'
```

For several genes, pass them in one call (`"LRAT,CRALBP,rho,ABCR"`): the atlas is loaded once for
all of them.

## 6. Validate before using the output

Each query folder `$AISCI_DATA_DIR/atlas-expression-exploration/<query_id>/` holds the following
files:

| File | Contents |
| --- | --- |
| `gene_resolution.tsv` | query, atlas gene, method, candidates |
| `query.h5ad` | all cells x query genes: `X` log1p(CP10k) using full-transcriptome totals, `layers["counts"]`, ordered categories, `uns["query"]` |
| `summary_{majorclass,celltype,region_class}.tsv` | per group and gene: `n_cells`, `n_expressing`, `pct_expressing`, `mean_lognorm`, `pseudobulk_cp10k`, `n_donors`, `donor_median_cp10k`, `donor_q25_cp10k`, `donor_q75_cp10k`, `frac_donors_detected`, `rank_pseudobulk` |
| figures | `dotplot_*`, `violin_*`, `featureplot` as PDF (editable Helvetica text, rasterized scatter) and PNG |
| `report.md` | summary of the above |
| `state.json`, `done.json`, `job.sbatch`, `job.log` | job record: command, options, job IDs, final state, `/usr/bin/time -v` output |

Check these, in this order:

1. **Resolution** (`resolution` in the `submit_query` result).
   - Every query should have an `atlas_gene`.
   - Report `hgnc_alias` and `hgnc_previous` as reassignments.
   - `ambiguous` lists the candidates; query one of them explicitly.
   - `unresolved` lists close matches.
2. **The job** (`done.json` and `job.log`).
   - `COMPLETED`, with the expected numbers of cells and genes in the logged AnnData.
   - A peak memory close to the request means the next run needs more.
3. **Donor support** for the top groups (`get_summary`): `n_donors` and `frac_donors_detected`.
   - A high percentage in a group with few donors is weak evidence.
   - In the tested atlas, RPE had 863 nuclei and 19 donors with at least 10 nuclei.
4. **Ambient RNA.** Low-level detection across many classes is expected for abundant
   transcripts. Rod genes are found in 5-30% of non-rod nuclei. Small clusters that light up for
   several unrelated rod genes point to contamination or doublets, not expression.

What the reference run looked like:

- LRAT peaks in RPE (6.29 pseudobulk CP10k, 75.7% of nuclei, all 19 donors), 27-fold above
  MG (0.231 CP10k, 7.1% of nuclei).
- Positive controls in one query, `LRAT,CRALBP,rho,ABCR`:
  - `rho` → RHO (case-insensitive), highest in Rod;
  - `ABCR` → ABCA4 (HGNC previous symbol), in Rod and Cone;
  - `CRALBP` → RLBP1 (HGNC alias), in RPE and MG.

## Report

Include:

- the query and each name's resolution method;
- the atlas (path, cells, genes) and the normalization;
- the `query_id`, the Slurm job ID(s) and partition from `state.json`;
- the aiscientist-mcp release tags (or the image digest);
- the top groups, each with `pct_expressing`, `pseudobulk_cp10k` and donor support;
- the figures;
- the caveats: nuclear transcripts, ambient RNA, descriptive statistics, and pooled donor labels
  counted as one donor.

Keep "the run completed" separate from "the pattern is supported across donors".
