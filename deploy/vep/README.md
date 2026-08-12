# Offline VEP container — build & stage (variant annotation)

Build kit for `vep.sif` + the VEP caches — the OFFLINE variant-annotation line AiScientist runs
**when `BIOAGENT_VARIANT_ON_HPC=1`**. The orchestration that *runs* the image is already built +
offline-tested (`src/bioagent/tools/vcf_offline.py` + `variant_cli.py`, driven by
`SlurmAnalysisExecutor` with an injected runner — no cluster, no VEP, no network in CI:
`tests/test_vcf_offline.py`). This folder is the **image + cache build kit**.

> ⚠️ macOS cannot build `.sif`. Build on **HPC3** (a **login node** — the cache download needs
> internet; compute nodes don't) or any Linux host with `singularity`.

## Why this exists

The REST tool (`tools/variant_annotation.py`) can't scale to a WGS-size VCF: it reads the whole file
into memory, caps at 500 variants, and is throttled by the public Ensembl VEP REST API. (HPC3 compute
nodes *do* have outbound egress — verified 2026-07-08 — so REST *can* run there, but bulk REST over
millions of variants is rate-limited, non-reproducible, and against Ensembl's guidance either way.)

This line fixes it: **bcftools** streams a PASS-filter, **VEP** annotates the whole VCF against a
**bind-mounted local cache** with `--fork` parallelism (no network needed — the cache is local), and
Python only stream-parses VEP's JSONL, so peak memory is BOUNDED.
A full WGS VCF forks through in ~30–60 min under `#SBATCH --mem` (a real cgroup cap).

## Build & stage

```bash
# on an HPC3 LOGIN node, from a checkout of this repo:
module load singularity            # HPC3 has apptainer/1.4.5, singularity/3.11.3
cd deploy/vep

# 1. build the image (deps-only: vep + bcftools + python3) — pick a route the script prints
singularity build --fakeroot vep.sif vep.def       # if RCIC enables --fakeroot
#   ...or `singularity build --remote vep.sif vep.def`

# 2. stage the .sif + download BOTH caches (GRCh38 + GRCh37) and the ClinVar VCFs to dfs3b
BUILT=1 ./build_and_stage.sh
#   -> vep.sif           -> /dfs3b/ruic20_lab/software/AiScientist/containers/vep.sif
#   -> GRCh38 / GRCh37   -> /dfs3b/ruic20_lab/software/reference/vep_annotation/<asm>/homo_sapiens/112_<asm>
#   -> clinvar_<asm>.vcf.gz(.tbi) in the same vep_cache/ dir
#   (idempotent: re-running skips anything already downloaded)

# 3. the script also PRINTS a ready-to-run offline smoke test (tiny TP53 VCF) — run it and
#    confirm /tmp/smoke.jsonl has one JSON line with a ClinVar significance.
```

The **release must match**: `vep.def`'s `release_112.0` tag ↔ the `release-112` cache the stage
script downloads ↔ the `112_GRCh38` dir VEP looks for. `VEP_RELEASE=113 ./build_and_stage.sh` bumps
all three; bump the def tag too.

## Enable it

Set these in the gateway env (`.env` / `HPCSettings`) — until `BIOAGENT_VARIANT_ON_HPC=1`,
`annotate_variants` stays on the REST path and this image is unused:

```
BIOAGENT_VARIANT_ON_HPC=1
BIOAGENT_VEP_IMAGE=/dfs3b/ruic20_lab/software/AiScientist/containers/vep.sif
BIOAGENT_VEP_CACHE_DIR_GRCH38=/dfs3b/ruic20_lab/software/reference/vep_annotation/GRCh38
BIOAGENT_VEP_CACHE_DIR_GRCH37=/dfs3b/ruic20_lab/software/reference/vep_annotation/GRCh37
BIOAGENT_VEP_CLINVAR_GRCH38=/dfs3b/ruic20_lab/software/reference/vep_annotation/clinvar_GRCh38.vcf.gz
BIOAGENT_VEP_CLINVAR_GRCH37=/dfs3b/ruic20_lab/software/reference/vep_annotation/clinvar_GRCh37.vcf.gz
BIOAGENT_VEP_ASSEMBLY=GRCh38          # default; LLM can override per call
BIOAGENT_VEP_FORK=8
BIOAGENT_UPLOADS_ON_HPC=1             # so the VCF lands on dfs3b and is annotated in place
```

The VCF + the run's work/artifacts dirs must be reachable on the compute node (shared DFS — they are,
via `uploads_on_hpc`). The bioagent **tools** are synced to `<lab_storage>/<user>/pysrc` by the
gateway and bind-mounted in, so a tool edit needs a code deploy, NOT an image rebuild. If HPC is
unreachable at run time, `annotate_variants` falls back to the REST path in-process (fine for small
VCFs).

## Keep in sync

`vcf_offline.build_vep_cmd` requests `--symbol --sift b --polyphen b --af --af_gnomade --af_gnomadg`
(so the JSON carries the fields `parse_vep_result` reads) plus `--custom` ClinVar. If a future cache
release renames a frequency flag, tune `_VEP_ANNOT_FLAGS` in `tools/vcf_offline.py` and re-run the
smoke test — no image rebuild needed (VEP flags are runtime args).
