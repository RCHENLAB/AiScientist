"""Reference template — normalize a VCF (atomize MNPs, split multiallelic, left-align indels).

Run this BEFORE annotation / ClinVar+gnomAD matching / caller comparison. dbSNP, ClinVar and gnomAD
store variants in canonical **left-aligned, parsimonious** representation; a non-normalized indel or
multiallelic site fails to match its database record, so a real pathogenic variant is silently
reported as "not_in_clinvar". Ported from operon's variant-calling-variant-normalization protocol
(bcftools norm), adapted to our BIOAGENT_WORK/BIOAGENT_ARTIFACTS conventions.

Pipeline (order matters): (1) --atomize splits MNPs into SNPs, (2) -m-any splits multiallelic into
biallelic records, (3) -f REF left-aligns + trims indels against the reference FASTA.

ADAPT: INPUT_VCF and REF_FASTA (a reference genome matching the VCF's assembly — GRCh38 or GRCh37,
with its .fai index). Needs bcftools on PATH (present in the analysis image, NOT the gateway env).
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

WORK = Path(os.environ.get("BIOAGENT_WORK", "."))
ART = Path(os.environ.get("BIOAGENT_ARTIFACTS", "."))
(ART / "data").mkdir(parents=True, exist_ok=True)

# ADAPT: the input VCF and the reference FASTA (must match the VCF's genome build).
INPUT_VCF = os.environ.get("BIOAGENT_DATASET") or str(WORK / "input.vcf.gz")
REF_FASTA = os.environ.get("BIOAGENT_REF_FASTA", "")   # e.g. .../GRCh38.primary_assembly.genome.fa
OUTPUT_VCF = str(WORK / "normalized.vcf.gz")

if not shutil.which("bcftools"):
    raise SystemExit("bcftools not found on PATH — normalization needs bcftools (analysis image only).")
if not Path(INPUT_VCF).exists():
    raise SystemExit(f"input VCF not found: {INPUT_VCF}")
if not REF_FASTA or not Path(REF_FASTA).exists():
    raise SystemExit("REF_FASTA not set / not found — left-alignment REQUIRES the reference genome "
                     "FASTA (+ .fai) for the VCF's assembly. Set BIOAGENT_REF_FASTA.")


def _count(vcf: str) -> int:
    """Number of variant records in ``vcf`` (via bcftools; -1 if it cannot be counted)."""
    try:
        out = subprocess.run(["bcftools", "view", "-H", vcf], capture_output=True, text=True, check=True)
        return sum(1 for _ in out.stdout.splitlines())
    except (subprocess.CalledProcessError, OSError):
        return -1


n_before = _count(INPUT_VCF)

# The 3-stage norm as one shell pipeline (operon's canonical order). shell=True so the pipe is legible;
# every argument here is a controlled path, not model/user free text.
pipeline = (
    f"bcftools norm --atomize {INPUT_VCF!r} 2>/dev/null "
    f"| bcftools norm -m-any 2>/dev/null "
    f"| bcftools norm -f {REF_FASTA!r} -Oz -o {OUTPUT_VCF!r}"
)
proc = subprocess.run(["bash", "-lc", pipeline], capture_output=True, text=True)
if proc.returncode != 0:
    raise SystemExit(f"bcftools norm failed (rc={proc.returncode}):\n{proc.stderr[-2000:]}")

# Index the normalized output so downstream VEP/region queries can use it.
subprocess.run(["bcftools", "index", "-t", OUTPUT_VCF], check=False)
n_after = _count(OUTPUT_VCF)

summary = {
    "input_vcf": INPUT_VCF,
    "reference_fasta": REF_FASTA,
    "normalized_vcf": OUTPUT_VCF,
    "records_before": n_before,
    "records_after": n_after,           # > before when multiallelic sites were split into biallelic
    "note": "atomize MNPs -> split multiallelic -> left-align+trim against the reference; feed "
            "normalized.vcf.gz to annotate_variants / ClinVar matching.",
}
(ART / "data" / "normalization_summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
