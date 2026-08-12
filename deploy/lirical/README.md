# LIRICAL container — build & stage (phenotype → disease differential)

Build kit for `lirical.sif` + the LIRICAL data — the **phenotype-driven differential-diagnosis** line
BioAgent runs **when `BIOAGENT_PHENOTYPE_ON_HPC=1`**. It sits **downstream of the variant line**
(`vep.sif`): the variant pipeline produces the gene/variant shortlist, then LIRICAL fuses the patient's
HPO terms with those findings into a **per-disease post-test probability** ("RP 70% / LCA 20% / …") —
the calibrated confidence Rui Chen asked for.

The orchestration that *runs* the image is built + offline-tested (`src/bioagent/tools/phenotype_dx.py`
+ `phenotype_cli.py`, driven by `SlurmAnalysisExecutor` with an injected runner — no cluster, no
LIRICAL, no network in CI: `tests/test_phenotype_dx.py`). This folder is the **image + data build kit**.

> ⚠️ macOS cannot build `.sif`. Build on **HPC3** (a **login node** — the downloads need internet) or
> any Linux host with `singularity`.

## Two scoring modes

| Mode | Needs | Output |
|---|---|---|
| **Phenotype-only** (works now) | LIRICAL data only (`lirical download`, ~1–2 GB) | per-disease posterior from HPO terms, gene-restricted to the variant line's shortlist |
| **Genotype-aware** (full) | + an Exomiser **variant database** (~20 GB) | posterior that also weighs the patient's variants (the sharpened confidence) |

## ⚠️ The Exomiser-version gotcha (read before staging genotype-aware)

The email plan assumed we could **reuse the Exomiser already on HPC3**. We can't, for LIRICAL v2:

- What exists: `/dfs3b/ruic20_lab/{chen/pipeline_restructure/pipeline_restructure,bin/pipeline/pipeline_restructure}/exomiser`
  → **`1805_hg19`** variant DB + **`1807_phenotype`** + **`exomiser-cli-10.1.0`** — i.e. **2018-era,
  Exomiser 10.x schema, hg19 only** (~21 GB).
- What LIRICAL v2 needs: an Exomiser data release **≥ 2302** (the newer `.mv.db` format). The 10.x
  databases are **not compatible**.

So genotype-aware LIRICAL requires a **fresh** Exomiser DB download (`EXOMISER=1` below). Match the
assembly to the lab's eye VCFs — those are mostly **GRCh37/hg19** (same as the existing Exomiser data
and the VEP line's `GRCh37` default), so `EXOMISER_ASSEMBLY=hg19` is the sensible first stage.
Phenotype-only mode needs **no** Exomiser DB and is usable immediately.

## Build & stage

```bash
# on an HPC3 LOGIN node, from a checkout of this repo:
module load singularity            # HPC3 has apptainer/1.4.5, singularity/3.11.3
cd deploy/lirical

# 1. build the image (deps-only: JRE 17 + LIRICAL v2 CLI baked in) — pick a route the script prints
singularity build --fakeroot lirical.sif lirical.def       # if RCIC enables --fakeroot
#   ...or `singularity build --remote lirical.sif lirical.def`

# 2. stage the .sif + download the LIRICAL phenotype data (required); Exomiser DB is opt-in
BUILT=1 ./build_and_stage.sh                       # phenotype-only (no Exomiser)
BUILT=1 EXOMISER=1 EXOMISER_ASSEMBLY=hg19 ./build_and_stage.sh   # + genotype-aware (hg19)
#   -> lirical.sif   -> /dfs3b/ruic20_lab/software/AiScientist/containers/lirical.sif
#   -> data          -> /dfs3b/ruic20_lab/software/reference/lirical/data
#   -> exomiser      -> /dfs3b/ruic20_lab/software/reference/lirical/exomiser/<ver>_hg19  (if EXOMISER=1)
#   (idempotent: re-running skips anything already downloaded)

# 3. the script also PRINTS a ready-to-run phenotype-only smoke test (tiny retinal phenopacket) — run
#    it and confirm /tmp/smoke.tsv has a ranked differential.
```

The `prioritize` flags (verified against v2.4.1 on HPC3 — CLI-args mode: `-p` observed HPO / `-n` negated
/ `-d` data / `-o` outdir / `-x` prefix / `-f` format / `--vcf` / `--assembly` / `-ed19`/`-ed38` Exomiser
data DIR / `--sample-id`) are pinned to the LIRICAL version baked in `lirical.def` (`LIRICAL_VERSION`). If
a future LIRICAL renames one, run `lirical prioritize --help` in the image and tune
`tools/phenotype_dx.build_lirical_cmd` — a runtime arg, **no image rebuild needed**.

## Enable it

Set these in the gateway env (`.env` / `HPCSettings`) — until `BIOAGENT_PHENOTYPE_ON_HPC=1`, the
phenotype step reports `not_installed` and the run continues without the differential:

```
BIOAGENT_PHENOTYPE_ON_HPC=1
BIOAGENT_LIRICAL_IMAGE=/dfs3b/ruic20_lab/software/AiScientist/containers/lirical.sif
BIOAGENT_LIRICAL_DATA_DIR=/dfs3b/ruic20_lab/software/reference/lirical/data
# genotype-aware (optional — omit both for phenotype-only):
BIOAGENT_LIRICAL_EXOMISER_HG19=/dfs3b/ruic20_lab/software/reference/lirical/exomiser/2406_hg19
BIOAGENT_LIRICAL_EXOMISER_HG38=/dfs3b/ruic20_lab/software/reference/lirical/exomiser/2406_hg38
BIOAGENT_UPLOADS_ON_HPC=1
```

The VCF + the run's work/artifacts dirs must be reachable on the compute node (shared DFS — they are,
via `uploads_on_hpc`). The bioagent **tools** are synced to `<lab_storage>/<user>/pysrc` and
bind-mounted in, so a tool edit needs a code deploy, NOT an image rebuild.

## How it fits the two-track design

LIRICAL is the **PRIMARY (calibrated)** track. The **EVIDENCE (literature / PaperQA2)** track is
separate and never blended into LIRICAL's probability — see
[`docs/phenotype_gene_confidence_rag_spec.md`](../../docs/phenotype_gene_confidence_rag_spec.md) and
[`docs/paperqa2_evidence_layer_contract.md`](../../docs/paperqa2_evidence_layer_contract.md).
