# What this deployment has, and where it is

Generated from the live code and settings by `scripts/write_environment_doc.py`.
Do not edit by hand — regenerate it.

## Assets

| what | where | path | agent may read |
|---|---|---|---|
| Gene-set libraries (.gmt) for run_enrichment / run_gsea_prerank | gateway | `/Users/yijunsun/Documents/BioAgentPrototype./.claude/worktrees/vigorous-tereshkova-768267/src/bioagent/tools/genesets` | — |
| Analysis image (run_code, scanpy line) | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/containers/analysis.sif` | — |
| scGPT image | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/containers/scgpt.sif` | — |
| scGPT weights + vocabulary + label map | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/scgpt_model` | — |
| Vision model for the post-render report review | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/vlreview_model` | — |
| Shared Python package cache (preflight installs land here) | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/pkgs` | — |
| Lab reference data (VEP caches, LIRICAL data, SpliceAI models) | hpc3 | `/dfs3b/ruic20_lab/software/reference` | — |
| Gene-set libraries (.gmt), the copy HPC3 jobs can read | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/pysrc/<user>/bioagent/tools/genesets` | — |
| Per-user process files (swept on a TTL) | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/Temp/<user>` | — |
| Per-user uploads (never swept) | hpc3 | `/dfs3b/ruic20_lab/software/AiScientist/uploads/<user>` | — |

**Gene-set libraries (.gmt) for run_enrichment / run_gsea_prerank** — Read OFFLINE by the enrichment tools — no network, no Enrichr API. Present: NONE — run scripts/fetch_genesets.py. BIOAGENT_GENESETS_DIR overrides this location. A pathway summary computed by hand should read these same files rather than fetch a collection.

**Analysis image (run_code, scanpy line)** — Fixed at build time and read-only. A package it lacks is resolved into the shared cache below, not installed into the image.

**scGPT weights + vocabulary + label map** — best_model.pt, vocab.json, id2type.json, dev_train_args.yml. scgpt_annotate binds this read-only into its GPU job. HUMAN RETINA reference: a human-symbol vocabulary (TFRC, not Tfrc) and 123 human retinal cell types (HAC*, HRGC*, DB1-6, RB, Rod, S_Cone, ML_Cone, MG, Astrocyte, Microglia, RPE; NO endothelial or pericyte class). A mouse query is case-folded onto it automatically and the transfer is recorded in species_harmonization.json. Measured on the DDX41 mouse retina: neurons agree with the supplied labels (rods 99.8%), but 73% of Muller glia come back 'Astrocyte' and every endothelial cell 'Microglia', at confidence >= 0.94 — confidence is not accuracy.

**Shared Python package cache (preflight installs land here)** — Immutable and content-keyed; appended to sys.path inside the container by a generated sitecustomize.

**Gene-set libraries (.gmt), the copy HPC3 jobs can read** — The same files as the gateway copy, synced with the live source for analysis jobs. They list human (upper-case) symbols: run_enrichment / run_gsea_prerank matched mouse genes in past runs, but code that reads a .gmt directly must upper-case mouse symbols first.

## This session's filesystem permissions

_No live cluster session — roots are built per session from the HPC3 account._

## Tools

`source` is where the function actually lives, so a tool can be found without grepping.

| tool | what it does | source | available |
|---|---|---|---|
| `run_clustering` | PCA → neighbors → Leiden clustering → UMAP on the QC'd data. | `src/bioagent/tools/run_clustering/tool.py:100` | yes |
| `run_composition` | Cell-type proportions per sample and how they shift between conditions — 'which populations expand or shrink'. | `src/bioagent/tools/run_composition/tool.py:27` | yes |
| `run_de` | Differential expression via rank_genes_groups (Wilcoxon), in EITHER of two shapes. | `src/bioagent/tools/run_de/tool.py:159` | yes |
| `run_depth_matched_de` | Separate biology from sequencing depth. | `src/bioagent/tools/run_depth_matched_de/tool.py:33` | yes |
| `run_enrichment` | Over-representation / pathway enrichment — OFFLINE ORA against LOCAL gene-set (.gmt) files (gseapy.enrich), NOT the Enrichr web API (the analysis host has no network). | `src/bioagent/tools/run_enrichment/tool.py:93` | yes |
| `run_gsea_prerank` | Preranked GSEA (gseapy.prerank) over the COMPLETE ranked gene list per group — OFFLINE, against the same local .gmt files as run_enrichment. | `src/bioagent/tools/run_gsea_prerank/tool.py:26` | yes |
| `run_integration` | Correct sample/donor/batch effects before clustering. | `src/bioagent/tools/run_integration/tool.py:25` | yes |
| `run_marker_annotation` | Assign a cell type to each cluster from a curated marker `panel` ({cell type: [symbols]}), with optional `discriminators` ({cell type: [2-4 lineage-SPECIFIC symbols]}). | `src/bioagent/tools/run_marker_annotation/tool.py:25` | yes |
| `run_pseudobulk_de` | Differential expression BETWEEN CONDITIONS, aggregated to one profile per sample. | `src/bioagent/tools/run_pseudobulk_de/tool.py:77` | yes |
| `run_scanpy_qc` | REAL scanpy QC on the uploaded single-cell dataset: per-cell metrics, cell/gene filtering, normalization, log1p, and HVG selection. | `src/bioagent/tools/run_scanpy_qc/tool.py:51` | yes |
| `annotate_variants` | Annotate a VCF's variants with functional consequence, the affected gene, predicted impact, and ClinVar clinical significance (pathogenicity), via the Ensembl VEP REST API. | `src/bioagent/tools/annotate_variants/tool.py:656` | yes |
| `diagnose_disease` | FULL differential diagnosis: combines LIRICAL's calibrated post-test probability with the published literature (deep_literature / PaperQA2) and returns ONE ranked list. | `src/bioagent/tools/diagnose_disease/tool.py:375` | yes |
| `map_phenotype_to_hpo` | Convert a patient's clinical description in FREE TEXT (any language — a referral note, a diagnosis line, a symptom list) into validated HPO term IDs for run_lirical. | `src/bioagent/tools/map_phenotype_to_hpo/tool.py:463` | yes |
| `run_lirical` | Phenotype-driven DIFFERENTIAL DIAGNOSIS: rank candidate DISEASES by a calibrated post-test probability, given the patient's phenotype (as HPO terms) and — when a VCF is loaded — the variant findings. | `src/bioagent/tools/run_lirical/tool.py:452` | yes |
| `scgpt_annotate` | scGPT REFERENCE-BASED per-cell cell-type annotation (a pretrained foundation model, run on a GPU). | `src/bioagent/tools/scgpt_annotate/tool.py:32` | **no** |
| `describe_environment` | What this deployment HAS and where it is: the container images, model weights (scGPT, vision review), gene-set (.gmt) libraries, reference data and package cache — each with its PATH and whether this session may read it | `src/bioagent/agents/registry.py:136` | yes |
| `run_code` | Write and run a short Python snippet (CodeAct) for custom ANALYSIS the other tools do not cover — scanpy/pandas/numpy etc. | `src/bioagent/agents/research_lab.py:616` | yes |
| `make_schematic` | Draw a SCHEMATIC diagram (analysis workflow, signaling pathway, or mechanism) by writing its structure as text — a deterministic renderer turns it into a figure in the gallery (NO AI draws pixels). | `src/bioagent/tools/make_schematic/tool.py:198` | **no** |
| `deep_literature` | Answer a focused scientific question against the published literature with a grounded, CITED answer (PaperQA2 deep RAG: search -> gather evidence -> synthesize). | `src/bioagent/tools/deep_literature/tool.py:395` | **no** |
| `literature_search` | Search published biomedical literature (Europe PMC) for REAL citations to ground the report. | `src/bioagent/tools/literature_search/tool.py:299` | yes |
| `inspect_dataset` | Skim an uploaded data file — ANY type, known extension or not — and return a structured 'what is this': format, genome assembly and sample id(s) for a VCF, the group tree for an .h5ad/HDF5 matrix, columns for a table, or | `src/bioagent/tools/inspect_dataset/tool.py:692` | yes |
| `run_doublet_detection` | Scrublet doublet scoring on raw counts, run AFTER run_scanpy_qc and BEFORE run_clustering. | `src/bioagent/tools/run_doublet_detection/tool.py:28` | yes |

## Host capabilities

anndata: yes · d2: no · europepmc: yes · graphviz: no · gseapy: yes · literature_remote: no · matplotlib: yes · mermaid: no · pandoc: yes · paper-qa: no · scanpy: yes
