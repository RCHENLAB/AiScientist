---
name: qc-snrna-with-cellqc-standalone
description: Run the CellQC pipeline (ambient correction, count/feature/mitochondrial filtering, doublet calling, nuclear fraction) over a cohort of 10x Cell Ranger libraries by hand, with bash plus conda or a container, from installing cellqc to validating result/metrics.csv. Use when setting up a cellqc environment or container outside an AiScientist run, writing a cellqc config.yaml (including mitochondrial/ribosomal/hemoglobin gene sets for mouse or non-human references), or diagnosing a failed or inert QC run. Inside an AiScientist analysis, QC of Cell Ranger output is the run_cellqc tool, not this skill.
license: MIT
compatibility: Requires bash, coreutils, tar, awk and conda or mamba; Slurm optional. Network access only to install packages.
metadata:
  category: single-cell
  version: "1.0.0"
  status: core
  author: "Jin Li"
  source: "lijinbio/aiscientist-skills@7fdab03 (MIT), adapted for AiScientist"
  adapted: "Python pinned below 3.14 in every install route; a container route added; when (not) to use"
  tags: "snrna-seq, scrna-seq, qc, 10x-genomics, cell-ranger, doublets, ambient-rna, snakemake"
  tested-with: "cellqc 0.3.6, Python 3.12.14, R 4.5.3, DoubletFinder 2.0.6, scDblFinder 1.24.10, SoupX 1.6.2, Seurat 5.5.1, Snakemake 9.22.0"
  compute: "16 CPUs, 80 GB requested (25.6 GB peak), 20 min for 4 libraries"
  image: "bioconda:cellqc=0.3.6 r-doubletfinder python=3.12"
  cpus: "16"
  mem_gb: "80"
  time_limit: "08:00:00"
  requires-gpu: "false"
  scheduler: "optional"
  requires-network: "install"
  requires-secrets: "none"
---

# QC snRNA-seq with CellQC (standalone)

[CellQC](https://github.com/lijinbio/cellqc) is a Snakemake pipeline. It takes 10x Cell Ranger
output and writes one QC'd `.h5ad` per library. A **cohort run** over a single sample file also
writes a joint `result/metrics.csv`, an HTML report and a slide deck. Separate single-sample runs
do not, so run the whole cohort together.

This skill needs only `bash`, coreutils, `tar`, `awk` and conda/mamba. Slurm is optional. The
commands were tested end to end, as written, on a 4-library mouse snRNA-seq cohort (10x 3' v4 /
GEM-X, GRCm39 reference) with `cellqc 0.3.6`: 40 Snakemake steps, all completed, in 19.5 min on
16 CPUs, with a 25.6 GB memory peak. Every value in `metrics.csv` matched an earlier 0.3.5 run of
the same data. Replace every `/path/to/...` with your own paths.

Keep two trees separate:

| Tree | Holds | Example |
| --- | --- | --- |
| project dir | `samples.tsv` metadata, `config.yaml`, run scripts | `~/proj/` |
| data dir | staged Cell Ranger outputs, CellQC output | `/path/to/data/` |

Never write into the Cell Ranger delivery itself. Treat it as read-only.

## When to use

- Running CellQC by hand on HPC3 or a workstation: setting up the environment or container, staging
  deliveries, writing the config, running, validating.
- Diagnosing a CellQC run that failed, fell back, or whose filter was inert — including one that the
  `run_cellqc` tool ran (its config, sample file and `cellqc.log` are under `work/cellqc/`).

## When NOT to use

- Inside an AiScientist analysis of Cell Ranger output, by default: call the `run_cellqc` tool. It
  runs this same pipeline in a pinned image on HPC3, stages the libraries, applies this checklist,
  and writes the checkpoint the later tools read. Re-doing it by hand with `run_code` loses all of
  that. Follow this skill instead (through `run_in_environment`, below) only when the user asks for
  it, or needs something `run_cellqc` does not expose: a cohort config, custom gene sets, a step
  re-run with changed settings.
- On a single matrix or an `.h5ad` with no raw matrix: CellQC needs Cell Ranger's raw and filtered
  matrices; use `run_scanpy_qc`.

## On AiScientist

This skill declares its environment (`image` in its metadata), so inside an AiScientist run its
commands run with `run_in_environment(skill="qc-snrna-with-cellqc-standalone", command=...)`. Each
call is a 16-CPU / 80 GB Slurm job on HPC3 in that image. What changes from the steps below:

- **Skip §1.** The image already has CellQC 0.3.6, DoubletFinder and Python 3.12; install nothing.
- **Paths.** The delivery is `$AISCIENTIST_DATASET_ROOT`, read-only: one library, or a folder of
  libraries (one per sub-folder). Use it as `/path/to/delivery`. Use `$AISCIENTIST_ENV_DIR` (the
  working directory, which persists across calls in the run) as the project and data dir, e.g.
  `stage=$AISCIENTIST_ENV_DIR/lnfiles` and `outdir=$AISCIENTIST_ENV_DIR/cellqc`.
- **Threads.** `-t "$AISCIENTIST_JOB_CPUS"`.
- **No sbatch.** The call already is a Slurm job: run `cellqc` in the foreground (§5's first form).
  A full run fits one call (8 h limit), and the dry run can be its own call.
- **Species.** One run takes one gene set. Run libraries of different species separately (§4).
- **Deliverables.** Copy `result/metrics.csv`, `qc_status.csv`, `manifest.tsv`, `report.html` and
  `report_slides.pdf` into `$AISCIENTIST_ARTIFACTS/cellqc/`, so they reach the report.
- **Later analysis steps.** AiScientist's analysis tools read one merged AnnData,
  `$AISCIENTIST_WORK/adata_qc.h5ad`, with a `sampleid` column. This image writes `.h5ad` with
  anndata 0.13, and the analysis image's anndata 0.12 cannot write back pandas' nullable string
  columns. So cast the `obs`/`var` text columns to `object` before writing the merged file.
  `run_cellqc` does this merge for you.

## 1. Environment setup

Install CellQC from bioconda into an env named after its version, so every result can be traced
back to the exact code that made it:

```bash
mamba create -y -n cellqc_v0.3.6 -c conda-forge -c bioconda cellqc=0.3.6 r-doubletfinder "python>=3.12,<3.14"

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate cellqc_v0.3.6

cellqc --version                     # cellqc, version 0.3.6
python --version                     # Python 3.12.x
Rscript -e "library(DoubletFinder)"  # must load without error
```

Both extra packages are required:

- **`r-doubletfinder`**: DoubletFinder is the default doublet caller. The bioconda `cellqc` 0.3.6
  package depends on it from build 1 (2026-10-02) on; naming it keeps older builds working.
- **`python>=3.12,<3.14`** (AiScientist pins it below 3.14 in every route; the reference run used
  3.12, and the image AiScientist builds pins `python=3.12`): without it the solver picks Python 3.14. On 3.14 the `nuclear_fraction`
  step fails for every library with `NameError: name 'snakemake' is not defined`, because 3.14
  changed how multiprocessing starts worker processes.

`conda create` with the same arguments works if you don't have mamba.

### Or a container (no conda on the host)

The BioContainers image `quay.io/biocontainers/cellqc:0.3.6--pyhdfd78af_1` has everything, but was
solved with **Python 3.14.7** (checked 2026-10-02), so `nuclear_fraction` fails in it whenever a BAM
is present. Build a pinned image from the same bioconda packages instead. This needs no root, so it
works on HPC3, where `--fakeroot` is unavailable. Run it inside a Slurm job: RCIC bans downloads on
login nodes.

```bash
module load singularity/3.11.3
W=${TMPDIR:-/tmp}/cellqc-build; mkdir -p "$W/pkgs" "$W/tmp"
export SINGULARITY_TMPDIR=$W/tmp SINGULARITY_CACHEDIR=$W/cache
singularity build --sandbox "$W/box" docker://mambaorg/micromamba:2.9.0-debian12-slim
mkdir -p "$W/box/aisci-pkgs" "$W/box/data" "$W/box/dfs3b" "$W/box/pub"
# --no-mount bind-paths: HPC3 binds /data etc. into every container, which a writable sandbox
# cannot create mount points for.
singularity exec --writable --containall --no-mount bind-paths -B "$W/pkgs:/aisci-pkgs" "$W/box" \
  bash -c 'export CONDA_PKGS_DIRS=/aisci-pkgs MAMBA_ROOT_PREFIX=/opt/conda;
           micromamba install -y -n base -c conda-forge -c bioconda \
             cellqc=0.3.6 r-doubletfinder "python>=3.12,<3.14" && micromamba clean -a -y'
mkdir -p "$W/box/.singularity.d/env" "$W/box/etc/profile.d"
echo 'export PATH=/opt/conda/bin:$PATH' > "$W/box/.singularity.d/env/90-conda.sh"
echo 'export PATH=/opt/conda/bin:$PATH' > "$W/box/etc/profile.d/90-conda.sh"   # for bash -l
singularity build cellqc_0.3.6_py312.sif "$W/box"

singularity exec cellqc_0.3.6_py312.sif python --version   # Python 3.12.x
```

Then put `singularity exec --containall -B <data> -B <outdir> cellqc_0.3.6_py312.sif` in front of
every `cellqc` command below. AiScientist's `run_cellqc` tool declares exactly this recipe
(`image: "bioconda:cellqc=0.3.6 r-doubletfinder python=3.12"`) and the platform builds it once, on
first use, into the shared containers directory.

The tested solve resolved to Python 3.12.14, R 4.5.3, r-doubletfinder 2.0.6, r-seurat 5.5.1,
bioconductor-scdblfinder 1.24.10, r-soupx 1.6.2 and snakemake-minimal 9.22.0. The command
above leaves DoubletFinder unpinned, so if your solve picks a different DoubletFinder or Seurat,
record the versions you got (`conda list`) in the report.

## 2. Stage the Cell Ranger outputs

CellQC reads a Cell Ranger `outs/`-style directory per library: `filtered_feature_bc_matrix/`,
`raw_feature_bc_matrix/` (each with `barcodes.tsv.gz`, `features.tsv.gz`, `matrix.mtx.gz`), the two
`.h5` files and `metrics_summary.csv`. `possorted_genome_bam.bam` and its `.bai` are optional.
If the plain `outs/` directory is already there, point the sample file straight at it and skip to
stage 3.

Deliveries from 10x Cloud (and some cores) arrive read-only, with the matrix directories packed as
`.tar.gz` archives of bare files. The script below links each delivery into a writable directory
and untars **each archive into a directory named after the archive**. That recreates the `outs/`
layout. If you untar everything into the library root instead, the raw and filtered matrices
overwrite each other and CellQC can't read them.

The metadata table is tab-separated, with one row per library and at least a `sampleid` column
and a `cellranger` column (the delivery directory):

```tsv
sampleid	genotype	cellranger
WT_1	WT	/path/to/delivery/WT_1
KO_1	KO	/path/to/delivery/KO_1
```

```bash
#!/usr/bin/env bash
# stage.sh: link each delivery into the data tree, then untar archives in place.
set -euo pipefail
shopt -s nullglob

meta=samples.tsv              # project metadata (sampleid, cellranger, ...)
stage=/path/to/data/lnfiles   # writable staging directory

# Columns are selected by header name, so reordering the table cannot shift them.
awk 'BEGIN {FS=OFS="\t"}
	NR==1 {for (i=1; i<=NF; i++) c[$i]=i
		if (!("sampleid" in c) || !("cellranger" in c)) {print "missing sampleid/cellranger column" > "/dev/stderr"; exit 1}
		next}
	{print $c["sampleid"], $c["cellranger"]}' "$meta" |
while IFS=$'\t' read -r sample delivery
do
	mkdir -p "$stage/$sample"
	for f in "$delivery"/*
	do
		link=$stage/$sample/$(basename "$f")
		[[ -e $link || -L $link ]] || ln -s "$(realpath -s "$f")" "$link"
	done
	for f in "$stage/$sample"/*.tar.gz
	do
		d=${f%.tar.gz}
		mkdir -p "$d"
		tar --skip-old-files -C "$d" -xzf "$f"
	done
done
```

The script is safe to rerun: existing links are left alone, and `--skip-old-files` never
overwrites files it already extracted. Large files (BAM, `.h5`) stay as links.

Check every library before going on:

```bash
stage=/path/to/data/lnfiles
for s in "$stage"/*/
do
	for m in filtered_feature_bc_matrix raw_feature_bc_matrix
	do
		for f in barcodes.tsv.gz features.tsv.gz matrix.mtx.gz
		do
			[[ -s $s$m/$f ]] || echo "MISSING: $s$m/$f"
		done
		[[ -s $s$m.h5 ]] || echo "MISSING: $s$m.h5"
	done
	[[ -s ${s}metrics_summary.csv ]] || echo "MISSING: ${s}metrics_summary.csv (no cellranger_* columns in metrics.csv)"
	[[ -s ${s}possorted_genome_bam.bam && -s ${s}possorted_genome_bam.bam.bai ]] \
		|| echo "NO BAM: $s (nuclear_fraction will be skipped for this library)"
done
```

No output means everything is in place. The BAM is what turns on the `nuclear_fraction` step.
Without it, that library still goes through every other step.

## 3. Build the cohort sample file

The sample file is a TSV with the header `sample<TAB>cellranger[<TAB>nreaction]`. Generate it from
the metadata table instead of typing it, so the run and the metadata always list the same
libraries:

```bash
meta=samples.tsv
stage=/path/to/data/lnfiles
outdir=/path/to/data/cellqc
mkdir -p "$outdir"

awk -v d="$stage" 'BEGIN {FS=OFS="\t"}
	NR==1 {for (i=1; i<=NF; i++) c[$i]=i; print "sample", "cellranger", "nreaction"; next}
	{print $c["sampleid"], d"/"$c["sampleid"], 1}' "$meta" > "$outdir/samples.txt"

cat "$outdir/samples.txt"
```

`nreaction` is the number of 10x reactions (GEM wells) pooled into a library. It only changes the
expected doublet rate, `rate * ncell / (nreaction * capacity)`. It describes how the libraries were
prepared and is not measured from the data, so halving it doubles the expected rate. **Confirm it
with whoever made the libraries.** If it varies by library, add an `nreaction` column to the
metadata and print `$c["nreaction"]` instead of `1`.

## 4. Configure

The config only needs the values that differ from the defaults (defaults merge in one level deep).
This is the config the reference run used, for a **mouse GRCm39 (GENCODE)** reference and 3' v4
nuclei:

```yaml
seed: 42

ambient:
  method: soupx      # applied to the counts
  compare: [decontx] # estimated and reported only

filterbycount:
  mincount: 500
  minfeature: 300
  mito: 5            # % mitochondrial cut-off; the default of 10 is permissive for nuclei

geneset:             # patterns are case-insensitive
  mt:
    label: '% mitochondrial'
    patterns: ['^mt-']
    symbols: []
    exclude: []
  ribo:
    label: '% ribosomal'
    patterns: ['^Rp[sl]\d', '^Rplp\d', '^Rpsa$']
    symbols: []
    exclude: ['^Rps6k', '^Rps19bp']
  hb:
    label: '% hemoglobin'
    patterns: ['^Hb[ab]-']
    symbols: []
    exclude: []

doublet:
  run: [doubletfinder, scdblfinder]
  decider: doubletfinder
  findpK: false
  pK: 0.01
  numthreads: 5
  # 3' v4 (GEM-X) has about half the Next GEM multiplet rate; 0.052 at capacity 13000
  # encodes that. Use the default rate (0.1) for Next GEM chemistry.
  rate: 0.052
  capacity: 13000
  nreaction: 1

nuclear_fraction:    # defaults; runs per library only if an indexed possorted_genome_bam.bam exists
  numthreads: 12     # worker processes reading the BAM
  cbtag: CB          # BAM tag holding the corrected cell barcode
  retag: RE          # BAM tag holding the region type of each alignment
  exontag: E         # RE value for exonic reads
  introntag: N       # RE value for intronic reads
```

The `nuclear_fraction` values are the defaults, written out so they are visible. The tags match
Cell Ranger's BAM, so only change them for a BAM from another aligner. Nuclear fraction (intronic
share of reads per cell) is reported, never filtered on.

### Gene sets for other references

Whether the mitochondrial filter does anything depends on `geneset`. If no gene matches, every
cell gets `pct_counts_mt = 0` and `filterbycount.mito` removes no cells, even though it is set in
the config. Specifying a set replaces its default definition entirely, so write out each set in
full.

| Reference | `mt` definition |
| --- | --- |
| Human GRCh38 | `patterns: ['^MT-']` |
| Mouse GRCm39/mm10 | `patterns: ['^mt-']` |
| Macaque Ensembl Mmul_10 (bare mtDNA names) | `patterns: ['^MT-']`, `symbols: [ND1, ND2, ND3, ND4, ND4L, ND5, ND6, COX1, COX2, COX3, ATP6, ATP8, CYTB]` |

The `symbols` list is a fallback. It is used only when no pattern matches any gene, so on a
reference that does use the `MT-` prefix, a bare `COX1` (also an old alias of nuclear `PTGS1`) is
never pulled in. For human or macaque, use uppercase `ribo`/`hb` patterns:
`['^RP[SL]\d', '^RPLP\d', '^RPSA$']` excluding `['^RPS6K', '^RPS19BP']`, and
`['^HB[ABDEGMQZ]([0-9][AB]?)?$', '^HB[AB]-[A-Z0-9]+$']`.

Other notes:

- `ribo` and `hb` are only reported, never used to filter. Keep `hb` even if it is zero
  everywhere: `filter_n_hb_genes > 0` with zero counts is a real result, whereas zero matched genes
  means the definition is wrong.
- `ambient.compare` estimates a second method without applying it, so you can see where the
  methods disagree without changing the counts.
- Both doublet callers score every cell. Only `decider` removes cells, and the two are compared
  with Cohen's kappa.
- `seed` makes the stochastic steps reproducible.

## 5. Run

**Dry run first.** It checks the config and sample file, and writes the fully resolved config,
defaults included, to `$outdir/config_<timestamp>.yaml`. Read that file: it shows exactly what will
run.

```bash
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate cellqc_v0.3.6

outdir=/path/to/data/cellqc
cellqc -d "$outdir" -t 16 -n -c config.yaml -- "$outdir/samples.txt"
```

**Run in the foreground** (workstation, or an interactive allocation). Set `-t` to the number of
cores you have:

```bash
cellqc -d "$outdir" -t 16 -c config.yaml -- "$outdir/samples.txt"
```

**Or submit it to Slurm** with a plain sbatch script. Fill in partition and account for your
cluster, and run `sbatch run_cellqc.sbatch` from the directory that contains `config.yaml`:

```bash
#!/bin/bash
#SBATCH --job-name=cellqc
#SBATCH --partition=<partition>
#SBATCH --account=<account>
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=16
#SBATCH --mem=80G
#SBATCH --time=1-00:00:00
#SBATCH -o cellqc-%j.out
#SBATCH -e cellqc-%j.err

# No `set -u`: conda's activation scripts reference unset variables.
set -eo pipefail
echo "start $(date) on $(hostname), job $SLURM_JOB_ID"

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate cellqc_v0.3.6

outdir=/path/to/data/cellqc
cellqc -d "$outdir" -t 16 -c config.yaml -- "$outdir/samples.txt"

echo "end $(date)"
```

Keep the `.sbatch` file and its `.out`/`.err` logs. Together they record exactly what ran.

**Sizing.** The reference run (4 libraries, about 42k Cell Ranger-called nuclei, biggest library
18.7k) used 16 CPUs and 80G requested, peaked at 25.6 GB, took 19.5 min wall time and 1.8 CPU-hours.
An earlier 11-library cohort (201k called cells, one library at 123k) took 44 min on 32 CPUs and
200G. Each library runs barcoderank, ambient, nuclear_fraction, filterbycount, doubletfinder,
scdblfinder, filterdoublet and postproc, and the cohort report and slides run once at the end.

**If `publish` fails** with "`result/<sample>.h5ad` has older modification time than input ...
clock skew": it is not a clock problem but an intermittent cellqc bug (hard-linked outputs keep
their source's timestamp). Delete the `outdir` and run again.

Write to a **new `outdir`** whenever you change the CellQC version or a threshold, so results that
may be compared against each other are kept separately.

## 6. Validate before using the output

Under `-d|--outdir`:

| Path | Contents |
| --- | --- |
| `result/{sample}.h5ad` | final QC'd matrix, ready for integration |
| `result/{sample}_obs.txt.gz` / `_var.txt.gz` | `.obs` / `.var` as TSV |
| `result/{sample}_doublet_summary.txt`, `_doublet_concordance.txt` | per-caller counts and kappa |
| `result/metrics.csv` | one row per sample, every number the run produced |
| `result/qc_status.csv` | `ok` / `fallback` / `failed` / `skipped` for each step of each sample |
| `result/manifest.tsv` | which samples reached `result/`, their cell count, and why not |
| `result/report.html`, `report_slides.pdf` | cohort QC report and slide deck |

Check these, in this order:

1. **`qc_status.csv`**: every step should be `ok`. Read the message on any `fallback` or
   `skipped` (e.g. `nuclear_fraction` skipped means no BAM). **`manifest.tsv`**: every sample
   should be `included = True`.
2. **Gene sets matched**: in `metrics.csv`, `filter_n_mt_genes` should be > 0 (13 for the
   protein-coding mtDNA genes) and `filter_mt_matched_by` should be the same for every library.
   If `filter_fail_mito = 0` across the whole cohort, the threshold isn't doing anything. Don't
   read it as clean data.
3. **Retention**: `filter_ncell_before` → `filter_ncell_after` → `doublet_ncell_after`, and
   `frac_retained`. A library far from the rest of the cohort needs looking into.
4. **Which threshold removed cells**: `filter_fail_mincount_only` / `_minfeature_only` /
   `_mito_only` versus `filter_fail_multiple`. The `_only` columns show what each threshold costs
   on its own.
5. **Ambient load**: `ambient_soupx_contamination_mean`, compared with
   `ambient_decontx_counts_removed_frac`. Write a note for any library above about 25%. If ambient
   load differs between conditions, carry it into integration as a covariate or diagnostic.
6. **Doublet agreement**: `concordance_doubletfinder_vs_scdblfinder_kappa`. A low kappa means the
   choice of `decider` is driving which cells get removed.
7. **Nuclear fraction**: `nf_median`, `nf_q25`, `nf_q75`, `nf_n_missing`. Reported only, not
   used to filter.

Quick per-library summary:

```bash
python - <<'EOF'
import csv
rows = list(csv.DictReader(open("/path/to/data/cellqc/result/metrics.csv")))
cols = ["filter_ncell_before", "filter_ncell_after", "doublet_ncell_after", "frac_retained",
        "filter_n_mt_genes", "filter_mt_matched_by", "ambient_soupx_contamination_mean",
        "concordance_doubletfinder_vs_scdblfinder_kappa"]
key = next(iter(rows[0]))  # first column is the sample id
print("\t".join([key] + cols))
for r in rows:
    print("\t".join([r[key]] + [r.get(c, "NA") for c in cols]))
EOF
```

What the reference run looked like: 13 mitochondrial genes matched by pattern in every library.
Retention was 85–89% for three libraries and 62% for the fourth, which also had about 25% mean
SoupX contamination against 2–9% for the others. Kappa was 0.05–0.14. A library like that fourth
one should be flagged before integration. Don't average it away: high ambient load fits both real
cell loss in the tissue and a technical failure.

CellQC does not re-call cells. Cell Ranger's cell calls are what enter the pipeline, so watch for
over-calling in the filtering numbers, because this pipeline won't correct it.

## Report

Include:

- the exact `cellqc` command, plus the job ID if run under Slurm;
- the env name and `cellqc --version`;
- the sample file: libraries and `nreaction`, and where the `nreaction` values came from;
- each config deviation from the defaults, with the reason;
- gene sets as *matched* (`filter_n_<set>_genes`, `filter_<set>_matched_by`), not just as
  configured;
- cells in → cells out per library, with what removed them;
- ambient and doublet summaries;
- every library flagged as a concern.

Keep "the run completed" separate from "the result can be trusted". Every step finishing doesn't
show that a threshold actually removed anything.
