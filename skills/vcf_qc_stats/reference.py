"""Reference template — VCF callset QC metrics (Ti/Tv, Het/Hom, counts) with expected-range flags.

A trustworthiness check on the callset BEFORE interpreting individual variants: a low Ti/Tv means the
downstream consequence/pathogenicity counts describe a noisy callset full of false positives. Ported
from operon's variant-calling-vcf-statistics protocol (bcftools stats), adapted to our conventions
and made to degrade gracefully (bcftools -> cyvcf2 -> a stdlib Ti/Tv scan).

ADAPT: INPUT_VCF and SEQ_TYPE ("WGS" or "WES" — the expected Ti/Tv band differs). Writes
tables/vcf_qc_summary.csv + data/vcf_qc.json, with a `qc_flags` list of anything out of range.
"""
import csv
import json
import os
import shutil
import subprocess
from pathlib import Path

WORK = Path(os.environ.get("BIOAGENT_WORK", "."))
ART = Path(os.environ.get("BIOAGENT_ARTIFACTS", "."))
(ART / "tables").mkdir(parents=True, exist_ok=True)
(ART / "data").mkdir(parents=True, exist_ok=True)

# ADAPT: the VCF to QC, and whether it is whole-genome or exome (sets the expected Ti/Tv band).
INPUT_VCF = os.environ.get("BIOAGENT_DATASET") or str(WORK / "normalized.vcf.gz")
SEQ_TYPE = os.environ.get("BIOAGENT_SEQ_TYPE", "WGS").upper()   # "WGS" or "WES"

if not Path(INPUT_VCF).exists():
    raise SystemExit(f"input VCF not found: {INPUT_VCF}")

# Operon's expected ranges: (typical, flag-low, flag-high).
TITV_BAND = {"WGS": (2.0, 1.8, 2.5), "WES": (3.0, 2.5, 3.5)}.get(SEQ_TYPE, (2.0, 1.8, 2.5))
HETHOM_BAND = (1.75, 1.5, 2.0)     # cohort outlier if well outside
CALLRATE_MIN = 0.90                # flag < 90%

metrics: dict = {"input_vcf": INPUT_VCF, "seq_type": SEQ_TYPE, "method": None,
                 "ti_tv": None, "het_hom": None, "call_rate": None,
                 "n_snps": None, "n_indels": None, "n_multiallelic": None, "n_records": None,
                 "n_samples": None}


def _from_bcftools_stats() -> bool:
    """Populate ``metrics`` from ``bcftools stats``. Returns False if bcftools is unavailable."""
    if not shutil.which("bcftools"):
        return False
    out = subprocess.run(["bcftools", "stats", "-s", "-", INPUT_VCF],
                         capture_output=True, text=True, check=True).stdout
    het = hom = miss = 0
    for line in out.splitlines():
        f = line.split("\t")
        if line.startswith("SN\t"):
            key, val = f[2].strip(), f[3].strip()
            if key == "number of SNPs:": metrics["n_snps"] = int(val)
            elif key == "number of indels:": metrics["n_indels"] = int(val)
            elif key == "number of multiallelic sites:": metrics["n_multiallelic"] = int(val)
            elif key == "number of records:": metrics["n_records"] = int(val)
            elif key == "number of samples:": metrics["n_samples"] = int(val)
        elif line.startswith("TSTV\t"):
            metrics["ti_tv"] = round(float(f[4]), 3)                 # ts/tv column
        elif line.startswith("PSC\t"):
            # PSC: id sample nRefHom nNonRefHom nHets ... nMissing (last)
            hom += int(f[3]); het += int(f[4]); miss += int(f[-1])
    if hom:
        metrics["het_hom"] = round(het / hom, 3)
    total_gt = het + hom + miss
    if total_gt:
        metrics["call_rate"] = round((het + hom) / total_gt, 4)
    metrics["method"] = "bcftools stats"
    return True


def _from_cyvcf2() -> bool:
    """Fallback: compute Ti/Tv + Het/Hom by scanning with cyvcf2. False if not importable."""
    try:
        from cyvcf2 import VCF
    except ImportError:
        return False
    ts = tv = het = hom = miss = snps = indels = 0
    purines, pyrimidines = {"A", "G"}, {"C", "T"}
    vcf = VCF(INPUT_VCF)
    metrics["n_samples"] = len(vcf.samples)
    for v in vcf:
        if v.is_snp:
            snps += 1
            r, a = (v.REF or "").upper(), (v.ALT[0] if v.ALT else "").upper()
            if {r, a} <= purines or {r, a} <= pyrimidines:
                ts += 1
            else:
                tv += 1
        elif v.is_indel:
            indels += 1
        if v.gt_types is not None:
            het += int((v.gt_types == 1).sum())
            hom += int((v.gt_types == 3).sum())      # cyvcf2: 3 == HOM_ALT
            miss += int((v.gt_types == 2).sum())     # 2 == UNKNOWN/missing
    metrics.update(n_snps=snps, n_indels=indels, n_records=snps + indels)
    if tv:
        metrics["ti_tv"] = round(ts / tv, 3)
    if hom:
        metrics["het_hom"] = round(het / hom, 3)
    if het + hom + miss:
        metrics["call_rate"] = round((het + hom) / (het + hom + miss), 4)
    metrics["method"] = "cyvcf2"
    return True


def _from_stdlib() -> bool:
    """Last resort: a plain-text Ti/Tv scan (SNP substitution classes only, no genotypes)."""
    import gzip
    opn = gzip.open if INPUT_VCF.endswith(".gz") else open
    ts = tv = snps = indels = 0
    purines, pyrimidines = {"A", "G"}, {"C", "T"}
    with opn(INPUT_VCF, "rt") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            c = line.split("\t")
            if len(c) < 5:
                continue
            ref, alt = c[3].upper(), c[4].split(",")[0].upper()
            if len(ref) == 1 and len(alt) == 1:
                snps += 1
                if {ref, alt} <= purines or {ref, alt} <= pyrimidines:
                    ts += 1
                else:
                    tv += 1
            else:
                indels += 1
    metrics.update(n_snps=snps, n_indels=indels, n_records=snps + indels)
    if tv:
        metrics["ti_tv"] = round(ts / tv, 3)
    metrics["method"] = "stdlib scan (Ti/Tv only; no genotype metrics)"
    return True


_from_bcftools_stats() or _from_cyvcf2() or _from_stdlib()

# Flag anything outside operon's expected ranges.
flags: list[str] = []
titv = metrics["ti_tv"]
if titv is not None and (titv < TITV_BAND[1] or titv > TITV_BAND[2]):
    flags.append(f"Ti/Tv {titv} outside {SEQ_TYPE} range {TITV_BAND[1]}–{TITV_BAND[2]} "
                 f"(expect ~{TITV_BAND[0]}) — possible excess false positives / contamination.")
hh = metrics["het_hom"]
if hh is not None and (hh < HETHOM_BAND[1] or hh > HETHOM_BAND[2]):
    flags.append(f"Het/Hom {hh} outside {HETHOM_BAND[1]}–{HETHOM_BAND[2]} — sample outlier / "
                 "possible contamination or inbreeding.")
cr = metrics["call_rate"]
if cr is not None and cr < CALLRATE_MIN:
    flags.append(f"Call rate {cr:.1%} below {CALLRATE_MIN:.0%} — low-quality / poorly-covered callset.")
metrics["qc_flags"] = flags

with (ART / "tables" / "vcf_qc_summary.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["metric", "value"])
    for k in ("method", "n_records", "n_snps", "n_indels", "n_multiallelic", "n_samples",
              "ti_tv", "het_hom", "call_rate"):
        w.writerow([k, metrics[k]])
(ART / "data" / "vcf_qc.json").write_text(json.dumps(metrics, indent=2))
print(json.dumps(metrics, indent=2))
