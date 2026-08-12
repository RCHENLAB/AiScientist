"""Reference template — build/query a scalable TileDB-VCF variant database (cohort/population scale).

ADAPT this via run_code for the OPTIONAL variant-database-management step (skip it for a single VCF).
TileDB-VCF stores many single-sample VCFs in one sparse-array dataset with incremental sample
addition (no expensive merges), compressed storage, and fast region/sample queries.

Requirements (heavy, optional — installed in the analysis image, NOT the gateway env):
  mamba install -c conda-forge -c bioconda -c tiledb tiledbvcf-py bcftools
Ingested VCFs must be **single-sample** and **indexed** (.tbi via tabix or .csi via bcftools):
  bgzip sample.vcf && tabix -p vcf sample.vcf.gz
Env conventions (see skills/README): BIOAGENT_WORK for the dataset dir, BIOAGENT_ARTIFACTS for exports.
"""

from __future__ import annotations

import os
from pathlib import Path

import tiledbvcf  # provided by the analysis image; do NOT import in the gateway env

WORK = Path(os.environ.get("BIOAGENT_WORK", "."))
ARTIFACTS = Path(os.environ.get("BIOAGENT_ARTIFACTS", "."))
DATASET_URI = str(WORK / "variant_db")           # a local dir (or an s3:// / az:// / gs:// URI)

# --- 1. Create the dataset once, then ingest samples incrementally --------------------------------
# Point this at YOUR indexed, single-sample VCFs (list them or glob a directory).
SAMPLE_VCFS = sorted(str(p) for p in WORK.glob("*.vcf.gz"))

if not Path(DATASET_URI).exists():
    ds = tiledbvcf.Dataset(uri=DATASET_URI, mode="w",
                           cfg=tiledbvcf.ReadConfig(memory_budget_mb=1024))
    ds.create_dataset()                          # attributes default to the common INFO/FORMAT fields
if SAMPLE_VCFS:
    tiledbvcf.Dataset(uri=DATASET_URI, mode="w").ingest_samples(SAMPLE_VCFS)
    # Incremental: re-run ingest_samples([new.vcf.gz]) later to add samples WITHOUT re-merging.

# --- 2. Query a genomic region across all (or selected) samples -----------------------------------
ds = tiledbvcf.Dataset(uri=DATASET_URI, mode="r")
all_samples = ds.samples()
df = ds.read(
    attrs=["sample_name", "contig", "pos_start", "pos_end", "alleles", "fmt_GT"],
    regions=["17:7668402-7687550", "13:32315474-32400266"],   # e.g. TP53, BRCA2 — adapt to your loci
    # samples=all_samples[:100],                               # optionally subset for population queries
)
print(f"[tiledbvcf] {len(all_samples)} sample(s); {len(df)} variant-records in the queried regions")

# --- 3. Export a subset / summary for the report --------------------------------------------------
ARTIFACTS.mkdir(parents=True, exist_ok=True)
out = ARTIFACTS / "tables" / "cohort_region_variants.tsv"
out.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(out, sep="\t", index=False)
# Per-sample variant counts in the queried regions (a simple cohort summary):
if len(df):
    counts = df.groupby("sample_name").size().sort_values(ascending=False)
    print("[tiledbvcf] variants per sample (top 10):")
    print(counts.head(10).to_string())
print(f"[tiledbvcf] wrote {out}")
