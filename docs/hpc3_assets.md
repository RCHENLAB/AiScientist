# What AiScientist keeps on HPC3

Most of what the product needs to run is not in the repository: container images, model weights
and annotation databases, about 580 G in all. This file lists what exists, what reads it and how
to get it back. Whoever builds, downloads or stages something on HPC3 adds its row here in the
same change.

[`configs/hpc3_assets.yaml`](../configs/hpc3_assets.yaml) pins the images and model weights the code
uses: each image's source and SHA-256, each model's revision and the SHA-256 of its weight files. A
run's `manifest.json` records the entries it used. `scripts/hpc3_assets_check.py` compares the list
with HPC3.

The session folders (`sessions/<session-uuid>/`) and their cleanup are in
[`hpc3_storage_layout.md`](hpc3_storage_layout.md). Sizes are as `du -sh` reports them.

## Where it lives

| Path | What |
|---|---|
| `/dfs3b/ruic20_lab/software/AiScientist/` | the shared root (`AISCIENTIST_HPC_SHARED_ROOT`), `<root>` below |
| `/dfs3b/ruic20_lab/software/bioagent` | a symlink to the root. Configuration and scripts outside the repository may name this path, so it stays. |
| `/dfs3b/ruic20_lab/software/reference/` | the lab's shared reference directory, `<reference>` below. `vep_annotation/`, `lirical/` and `spliceai/` in it belong to AiScientist. |

## Container images: `<root>/containers/` (84 G)

Every job runs its image with Apptainer (`module load apptainer/1.4.5`).
`AISCIENTIST_CONTAINER_BIN` and `AISCIENTIST_CONTAINER_MODULE` select another runtime without a
code change.

| Image | Size | What runs in it | Setting | Rebuild |
|---|---|---|---|---|
| `vllm-0.28.0.sif` | 7.4 G | vLLM 0.28.0: each session's model server, and the shared reasoning-model server | `AISCIENTIST_VLLM_IMAGE` | `scripts/hpc3_vllm_setup.sh` (pulls `docker://vllm/vllm-openai:v0.28.0`) |
| `vllm.sif` | 7.7 G | vLLM 0.22.1: the image the model list serves `QuantTrio/Qwen3.6-35B-A3B-AWQ` with (`gateway/cluster_models.py`) | the model list | the same script with `VLLM_DOCKER=docker://vllm/vllm-openai:v0.22.1` and `AISCIENTIST_VLLM_IMAGE=<root>/containers/vllm.sif` |
| `analysis.sif` | 431 M | `run_code` and the single-cell tools (scanpy, gseapy), plus bcftools, samtools, tabix/bgzip, bedtools, pysam and cyvcf2 | `AISCIENTIST_ANALYSIS_IMAGE` | `deploy/analysis/build_and_stage.sh` |
| `report.sif` | 444 M | report rendering (pandoc, XeLaTeX) | `AISCIENTIST_REPORT_IMAGE` | `deploy/report/build_and_stage.sh` |
| `vep.sif` | 230 M | offline Ensembl VEP release 112 and bcftools; OpenSpliceAI is executed inside it | `AISCIENTIST_VEP_IMAGE` | `deploy/vep/build_and_stage.sh` |
| `lirical.sif` | 129 M | LIRICAL, phenotype to disease ranking | `AISCIENTIST_LIRICAL_IMAGE` | `deploy/lirical/build_and_stage.sh` |
| `scgpt.sif` | 3.9 G | scGPT per-cell annotation, on a GPU | `AISCIENTIST_SCGPT_IMAGE` | `deploy/scgpt/build_and_stage.sh` |
| `vlreview.sif` | 3.6 G | the vision model that reviews the rendered report | `AISCIENTIST_VLREVIEW_IMAGE` | `scripts/hpc3_vlreview_setup.sh` with `deploy/vlreview/` |
| `cellqc-0.3.6_4509649d69.sif` | 1.0 G | `run_cellqc` and the standalone CellQC skill: CellQC 0.3.6, Python 3.12, R with DoubletFinder | `image:` in `run_cellqc/TOOL.md` | automatic, see below |
| `rockylinux_9.7.20251123.sif` | 83 M | the agent's `run_shell` sandbox: the compute nodes' OS (Rocky Linux 9.7), with RCIC's modules and the host's `/usr` mounted read-only | `SHELL_IMAGE_REF` in `hpc/shell.py` | automatic: pulled on the session's CPU worker on first use (`docker://rockylinux/rockylinux:9.7.20251123`) |

The PaperQA image is with the literature files, under `<root>/retigene/`.

### Building an image

- Build on a compute node, never on a login node, with `apptainer build --fakeroot`. Each
  `deploy/<name>/build_and_stage.sh` prints the `sbatch` line.
- A `%post` that runs `apt-get` must first run
  `echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99-aiscientist-fakeroot`. Under
  `--fakeroot` only root is mapped, and apt fails when it tries to switch to its `_apt` user.
- Point `APPTAINER_CACHEDIR` at `$TMPDIR` on the node. The default `~/.apptainer/cache` fills the
  home quota with base-image layers.

### Images a tool or a skill declares

A tool's `TOOL.md` or a skill's `SKILL.md` can declare `image:`. The first job that needs the image
provisions it on a compute node into `<root>/containers/` (`gateway/tool_images.py`), and every later
job of any lab member reuses the file. A `docker://` reference is pulled. A
`bioconda:<package>=<version> ...` recipe is built without root: conda packages are installed into
a micromamba base and packed into a SIF named after the recipe.

A build writes the exact package list beside the image as `<image>.sif.lock.txt`, and it reads and
extends the lab's conda download cache, `<root>/envs/conda-pkgs/`, which the first build creates. To
rebuild such an image, delete the file: the next job that needs it builds it again.
`cellqc-0.3.6_4509649d69.sif` has no lock file beside it; its package list is inside the image at
`/opt/conda/aiscientist-packages.txt`.

### Other files in `containers/`

These are kept for testing model serving. Nothing in the product's code or in the production
configuration reads them. Together they are 60 G of the directory.

| File | Size | What |
|---|---|---|
| `sglang-0.5.18-cu129.sif`, `sglang-glm53.sif` | 16.1 G, 10.7 G | SGLang builds from model-serving tests |
| `vllm-glm53.sif` | 9.9 G | a vLLM build from the same tests |
| `vllm-0.27.1.sif`, `vllm-0.27.1-patches/` | 7.5 G | vLLM 0.27.1 and one patched source file |
| `vllm-nightly.sif`, `vllm-nightly-20260828.sif` | 7.4 G each | two vLLM nightly builds |
| `cellqc_0.3.6--pyhdfd78af_1.sif` | 1.0 G | the published CellQC BioContainer. It ships Python 3.14, on which CellQC's nuclear-fraction step fails, so `run_cellqc` builds its own image. |
| `analysis.sif.bak-20260711`, `analysis.def` | 392 M | the previous `analysis.sif`, and a copy of a definition file |

## Model weights and Python environments: `<root>/`

| Path | Size | What | Read through | Get it again |
|---|---|---|---|---|
| `models/hf/hub/models--RedHatAI--Qwen3.8-27B-INT4` | 18 G | the default model of a session | `AISCIENTIST_VLLM_MODEL`, `AISCIENTIST_HF_HOME` | `scripts/hpc3_vllm_setup.sh` (`hf download` into `<root>/models/hf`) |
| `models/hf/hub/models--QuantTrio--Qwen3.6-35B-A3B-AWQ` | 24 G | the second model of the model list | the model list | the same script with `AISCIENTIST_VLLM_MODEL` set to it |
| `models/hf/hub/models--MJPansa--DeepSeek-V4-Flash-0731-NVFP4` | 160 G | the model of the shared reasoning-model server | `AISCIENTIST_SHARED_LLM_MODEL` (`configs/aiscientist.example.env`, section 6b) | `hf download` into `<root>/models/hf` on a compute node |
| `models/vlreview/` | 16 G | `Qwen/Qwen2.5-VL-7B-Instruct`, for the report review | `AISCIENTIST_VLREVIEW_MODEL_DIR` | `scripts/hpc3_vlreview_setup.sh` |
| `models/scgpt/` | 198 M | the scGPT reference checkpoint: `best_model.pt`, `vocab.json`, `id2type.json`, `dev_train_args.yml` | `AISCIENTIST_SCGPT_MODEL_DIR` | not downloadable: a copy of the lab's trained reference model (`deploy/scgpt/README.md`) |
| `envs/openspliceai/` | 5.1 G | a conda environment with OpenSpliceAI and PyTorch | `AISCIENTIST_SPLICEAI_BIN` | `deploy/vep/stage_annotation_dbs.sh` |
| `envs/analysis-pydeps/` | 128 M | Python packages the analysis image lacks, added to the analysis jobs' import path: `pydeseq2` and what it needs (`formulaic`, `formulaic_contrasts`, `interface_meta`, `wrapt`) | `AISCIENTIST_HPC_PYDEPS` | `pip install --no-deps --target <root>/envs/analysis-pydeps <package>`, run inside `analysis.sif` so the wheels match its Python |
| `envs/run-code-pkgs/` | under 1 M | the shared cache of packages that `run_code` installs when a snippet needs one the image lacks (`gateway/package_cache.py`) | fixed: `<root>/envs/run-code-pkgs` | fills itself |

Nothing in the product's code or in the production configuration reads the following, about 24 G
together:

| Path | Size | What |
|---|---|---|
| `ollama/` | 24 G | Ollama binaries and `qwen3.6` weights. Ollama is not a serving backend of the product. |
| `envs/analysis-pydeps/flashinfer-*`, `envs/analysis-pydeps/fi-*` | 125 M, part of `analysis-pydeps/` | packages left by the model-serving tests |
| `_archive_2026-10-22/` | 16 M | benchmark scripts, patch sources and job logs set aside for deletion by a storage cleanup (RCHENLAB/AiScientist issue 28). `MANIFEST.txt` lists what came from where. |

## Literature: `<root>/retigene/` (7.5 G)

`deep_literature` and `extract_haploinsufficient_genes` run as HPC3 jobs that read these files.
The literature line owns the directory; the other members' jobs only read it. The corpus scan's job
also starts its own model on a paid-partition GPU from `<root>/containers/vllm-0.28.0.sif` and the
weights in `<root>/models/hf` (`deploy/paperqa/README.md`).

| Path | Size | What | Setting |
|---|---|---|---|
| `paperqa.sif` | 2.8 G | the PaperQA container | `AISCIENTIST_PAPERQA_IMAGE` |
| `papers/` | 3.6 G | the corpus, 1,739 PDFs | `AISCIENTIST_PAPERQA_PAPERS` |
| `index_pubmedbert/` | 215 M | the index `retigene_full_pubmedbert` | `AISCIENTIST_PAPERQA_INDEX_DIR`, `AISCIENTIST_PAPERQA_INDEX_NAME` |
| `paperqa_manifest.csv` | 1.2 M | title, authors and DOI per paper | `AISCIENTIST_PAPERQA_MANIFEST` |
| `hf_cache/` | 412 M | the embedding model `NeuML/pubmedbert-base-embeddings` | `AISCIENTIST_PAPERQA_ROOT` (the job sets `HF_HOME=<that>/hf_cache`) |
| `mechanism_cache/`, `hgnc_complete_set.txt` | 395 M, 17 M | the corpus split into sentences with their embeddings, and the HGNC gene table, for the corpus scan | found beside `papers/`; `AISCIENTIST_MECHANISM_CACHE` and `AISCIENTIST_HGNC_TABLE` override |

None of these paths has a default in the code: with the `AISCIENTIST_PAPERQA_*` settings unset
the literature tools are unavailable and a run continues without them.
Rebuild: the image with `deploy/paperqa/build_and_stage.sh`
(`AISCIENTIST_PAPERQA_CONTAINER_DIR=<root>/retigene` stages it here), the index with
`deploy/paperqa/embed_corpus.slurm`, the corpus-scan cache with `scripts/build_mechanism_cache.py`.

## Annotation databases: `<reference>/` (241 G)

The shared reference directory follows the lab's rule: download each database once, mount it
read-only, reuse it across projects. Do not copy these under `<root>`.

| Path | Size | What |
|---|---|---|
| `vep_annotation/plugins/cadd/` | 158 G | CADD whole-genome SNV scores, one file per build: `whole_genome_SNVs.tsv.gz` (GRCh38) and `whole_genome_SNVs.grch37.tsv.gz` |
| `vep_annotation/GRCh38/`, `vep_annotation/GRCh37/` | 25 G, 17 G | the VEP release 112 offline caches |
| `vep_annotation/plugins/revel/` | 7.8 G | REVEL: `new_tabbed_revel_grch38.tsv.gz` and `new_tabbed_revel_grch37.tsv.gz`, and the 6 G source table they were built from |
| `vep_annotation/ref/` | 5.8 G | reference FASTA with `.fai`: `Homo_sapiens.GRCh38.dna.primary_assembly.fa` and `hg19.fa` |
| `vep_annotation/plugins/alphamissense/` | 602 M | `AlphaMissense_hg38.tsv.gz` |
| `vep_annotation/clinvar_GRCh38.vcf.gz`, `clinvar_GRCh37.vcf.gz` | 180 M each | ClinVar, read through VEP `--custom`. The `.tbi` beside each must be bound with it. |
| `vep_annotation/plugins/vep_plugins/` | small | the VEP plugin scripts (`--dir_plugins`) |
| `lirical/exomiser/2406_hg19/` | 26 G | the Exomiser 2406 hg19 variant database, for genotype-aware LIRICAL |
| `lirical/data/` | 346 M | LIRICAL's data: `hp.json`, `phenotype.hpoa`, `en_product6.xml`, `mim2gene_medgen`, `hgnc_complete_set.txt` and six transcript `.ser` files |
| `spliceai/OSAI-MANE-10000nt/` | 14 M | the five OpenSpliceAI MANE models |

Staged by `deploy/vep/build_and_stage.sh`, `deploy/vep/stage_annotation_dbs.sh` and
`deploy/lirical/build_and_stage.sh`. The settings that point at each file are listed in
[`vcf_pipeline_tools.md`](vcf_pipeline_tools.md) and in `configs/aiscientist.example.env`.

## Quota

All of this counts against the lab's group allocation on dfs3b (`ruic20_hpc`, 700 TiB), which
every lab member shares. Check it with `dfsquotas <UCInetID> dfs3b`; `df -h` shows the whole
filesystem, not the allocation.
