# What AiScientist owns on HPC3

**Inventory date: 2026-08-08.** Most of this is **not in the repo** — it is 350+ GB of container
images, model weights and annotation databases that took days of build/download time. If it is
lost, the repo alone cannot bring the product back. This file is the record of what exists, who
built it, and how to rebuild each piece.

Keep it current: **anything you stage, build, or download on HPC3 gets a row here in the same
change-set.** The rebuild column is the point — a size and a path are not a handoff.

## Where everything lives

```
/dfs3b/ruic20_lab/software/AiScientist/      <- our root (BIOAGENT_HPC_SHARED_ROOT)
/dfs3b/ruic20_lab/software/bioagent          -> symlink to AiScientist  (back-compat, see below)
/dfs3b/ruic20_lab/software/reference/        <- lab-shared reference dir (owner <ucinetid>), three of
                                                its subdirs are ours
```

### The 2026-08-08 rename

The root was `software/bioagent` until 2026-08-08 and was renamed to `software/AiScientist` to
match the product name. **`software/bioagent` is now a symlink to it**, deliberately and
permanently: prod's `/data/BioAgent/app/.env` and any out-of-repo script may still carry the old
absolute paths, and both spellings must keep resolving. Same zero-downtime pattern as the
`BIOAGENT_*` / `AISCIENTIST_*` env aliases (CLAUDE.md). **Do not delete the symlink** until every
`.env` on eyeserver has been checked. Verified after the rename: all 7 `.sif` files resolve through
both names.

Note `/dfs3b/ruic20_lab/AiScientist` (top level) does **not** exist and cannot be created by us —
`/dfs3b/ruic20_lab` is `drwxr-s--- ruic20 ruic20_hpc`, no group write. `newgrp`/`sg` do not help
(supplementary groups already count for the access check). `software/` is group-writable (2775),
which is why our root lives there.

## Containers — `<root>/containers/` (17 G)

Built with `singularity build --remote` (Sylabs cloud): **HPC3 has no fakeroot** — the user has no
mapping in `/etc/subuid` — so a local `singularity build` cannot work. Each has a
`deploy/<name>/build_and_stage.sh` in the repo.

| file | size | built | purpose | rebuild |
|---|---|---|---|---|
| `vllm.sif` | 8.3 G | 2026-06-11 | the LLM server (Qwen3.6-35B-A3B-AWQ, `/v1`) | `deploy/` — vLLM upstream image |
| `scgpt.sif` | 4.1 G | 2026-06-20 | scGPT per-cell annotation (GPU) | `deploy/scgpt/build_and_stage.sh` |
| `vlreview.sif` | 3.8 G | 2026-07-07 | VL report review (Qwen2.5-VL-7B) | `deploy/vlreview/` |
| `report.sif` | 466 M | 2026-07-02 | pandoc + xelatex report render | `deploy/report/build_and_stage.sh` |
| `analysis.sif` | 451 M | 2026-07-11 | run_code + scanpy line; **bcftools 1.21**, samtools/tabix/bgzip/bedtools, pysam/cyvcf2 | `deploy/analysis/build_and_stage.sh` (`analysis.def` sits next to it) |
| `analysis.sif.bak-20260711` | 411 M | 2026-07-02 | the pre-toolkit image, kept as the rollback | — |
| `vep.sif` | 241 M | 2026-07-07 | offline Ensembl VEP (+ OpenSpliceAI runs inside it) | `deploy/vep/build_and_stage.sh` |
| `lirical.sif` | 135 M | 2026-07-14 | LIRICAL v2.4.1 phenotype→disease | `deploy/lirical/build_and_stage.sh` |

**`paperqa.sif` is NOT in this directory** — it was built, but it lives in Ziyao's personal dir
(see the warning below), so `deploy/paperqa/build_and_stage.sh` has never staged a copy here.

## Model weights & caches — `<root>/` (~66 G)

| path | size | what | rebuild |
|---|---|---|---|
| `ollama/` | 24 G | `qwen3.6:35b-a3b` weights, moved off `$HOME` to shared DFS | re-pull |
| `hf/` | 24 G | HuggingFace cache (`HF_HOME`), holds the served AWQ model. `hub/` 24 G + `xet/` | re-download from HF |
| `vlreview_model/` | 16 G | `Qwen/Qwen2.5-VL-7B-Instruct`, 5 safetensors shards | `hf download` (was truncated once on a login node — resume the same command) |
| `scgpt_model/` | 198 M | scGPT checkpoint: `best_model.pt`, `vocab.json`, `id2type.json`, `dev_train_args.yml` | from the scGPT release |
| `envs/openspliceai` | 5.1 G | conda env for OpenSpliceAI, executed **inside** `vep.sif` | `deploy/vep/stage_annotation_dbs.sh` |

Build caches, safe to delete and regenerate: `.sing-cache/` 16 G, `.pip-cache/` 2.7 G,
`.conda-pkgs/` 657 M, `.sing-tmp/`, `.tmp/`, `.smoketest/`, and two stray `vlreview-*.out` job logs.
**These are the first place to look when dfs3b is tight — ~19 G with no research value.**

## Annotation databases — `software/reference/` (241 G of ours)

Owner of the parent is `<ucinetid>`; these three subdirs are ours (owner `<ucinetid>`), placed there on
purpose following Jin Li's convention: download each DB once, mount read-only, reuse across
projects. **Do not duplicate them under our own root.**

| path | size | what |
|---|---|---|
| `vep_annotation/plugins/cadd` | **158 G** | CADD — by far the largest single asset we own |
| `vep_annotation/GRCh38` | 25 G | VEP offline cache, GRCh38 |
| `vep_annotation/GRCh37` | 17 G | VEP offline cache, GRCh37 (most eye/IRD data is hg19) |
| `vep_annotation/plugins/revel` | 7.8 G | REVEL |
| `vep_annotation/ref` | 5.8 G | reference FASTAs |
| `vep_annotation/plugins/alphamissense` | 602 M | AlphaMissense |
| `vep_annotation/clinvar_GRCh3{7,8}.vcf.gz(+.tbi)` | 180 M each | ClinVar via VEP `--custom`; the `.tbi` **must** be bound alongside |
| `lirical/exomiser` | 26 G | Exomiser 2406_hg19 (the lab's old 1805_hg19 is too old for LIRICAL v2) |
| `lirical/data` | 346 M | `hp.json`, `en_product6.xml`, `mim2gene_medgen`, `hgnc_complete_set.txt`, 6 × `.ser` |
| `spliceai/OSAI-MANE-10000nt` | 14 M | OpenSpliceAI MANE models (JHU FTP) |

Staged by `deploy/vep/build_and_stage.sh` and `deploy/vep/stage_annotation_dbs.sh`; the run logs
are kept on the cluster at `vep_annotation/_stage.log` (75 M) and `_stage.sh`.

## The literature line lives in a personal dir — the one real fragility

`BIOAGENT_PAPERQA_*` in prod's `.env` points at **five paths inside `/dfs3b/ruic20_lab/<ucinetid>/`**,
totalling ~6.6 GB:

| path | size | what |
|---|---|---|
| `<ucinetid>/BioAgentPrototype/deploy/paperqa/paperqa.sif` | 2.8 G | the PaperQA container |
| `<ucinetid>/retigene/papers` | 3.6 G | the PDF corpus |
| `<ucinetid>/retigene/index_pubmedbert` | 215 M | the PubMedBERT index |
| `<ucinetid>/retigene/paperqa_manifest.csv` | 1.2 M | the manifest |
| `<ucinetid>/retigene` | (root) | `BIOAGENT_PAPERQA_ROOT`, also holds `hf_cache` |

They are readable today (`<ucinetid>/` is `drwxr-s---`, group `r-x`), so prod works. But if that
account reorganises or leaves, `deep_literature` silently drops to its `dependency_missing`
fallback — the run continues and simply has no literature. **Nothing here is ours to move**:
it is the literature line's work sitting in its owner's directory, and moving another member's
files is exactly what we do not do. Raise it with Ziyao; the destination if he agrees is
`<shared_root>/` alongside the other assets. Flagged in prod's `.env` next to the keys.

## Working dirs — per-user, under our root

| path | swept? |
|---|---|
| `Temp/<ucinetid>/` | **yes — 3 days**, see `docs/hpc3_storage_layout.md` |
| `uploads/<ucinetid>/` | never (raw research data) |
| `pysrc/<ucinetid>/` | never (rewritten each connect) |
| `bin/<ucinetid>/temp_gc.sh` | never (re-staged each connect) |

## Per-user session state — `$HOME/.bioagent/` (2.4 M)

`serve.sbatch` / `serve.gpu.sbatch` / `serve.free_gpu32.sbatch`, `vllm.<jobid>.port` + `vllm.port`
(how the gateway finds a running vLLM), and stale `analysis/ jobs/ ollama/ report/ runcode/
variant/` dirs left over from before scratch moved to dfs3b. Small; not swept. Note `$HOME` is a
separate, much smaller quota than dfs3b.

## Legacy per-member dirs — `/dfs3b/ruic20_lab/<ucinetid>/`

Where run process files went before 2026-08-08: `analysis/ variant/ phenotype/ scgpt/ reports/
.bioagent/`, plus `uploads/`. **Never auto-deleted** — browse and delete by hand from the storage
panel. As of the inventory, `<ucinetid>` alone had 11 G of `analysis` + 17 G of `variant` there.

## Quota

`ruic20_hpc` on dfs3b: **595.26 TiB / 600 TiB (99.2 %)** as of 2026-08-07 — about 4.7 TiB left for
the whole lab, down from ~16 TiB a month earlier. Check with `dfsquotas <UCInetID> dfs3b`; `df -h`
shows the whole filesystem, not the allocation.
