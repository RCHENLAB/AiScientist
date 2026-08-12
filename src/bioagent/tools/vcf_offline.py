"""Offline, WGS-scale VCF variant annotation via Ensembl VEP ``--offline`` + a local cache.

The REST tool (:mod:`bioagent.tools.variant_annotation`) can't scale to a large VCF: it reads the
WHOLE file into a Python string, caps at 500 variants, and is throttled by the public VEP REST API,
so it cannot annotate a WGS VCF at all. This module is the offline counterpart:

* **bcftools** streams a PASS-filter (a C tool — never materialises the VCF in Python), then an
  optional left-align/split ``norm`` when a reference FASTA is staged,
* **VEP** annotates the (whole) filtered VCF against a bind-mounted local cache with ``--fork``
  parallelism — no network needed (the cache is local), so it runs on an HPC3 CPU node inside
  ``vep.sif`` in ~30–60 min; predictor plugins (CADD/AlphaMissense/REVEL) + HGVS/MANE are added when
  their data is staged (env-gated),
* **Python** only parses VEP's JSONL output line-by-line — so peak memory is BOUNDED regardless of
  how large the VCF is.

Runs INSIDE ``vep.sif`` on an HPC3 CPU node, driven by :mod:`bioagent.tools.variant_cli` +
:class:`bioagent.gateway.slurm_analysis.SlurmAnalysisExecutor` (the same Phase-4 offload pattern as
the scanpy line). The parse/summarise REUSE the REST tool's helpers
(``parse_vep_result`` / ``classify_significance`` / ``summarize_annotations``) so both paths emit the
SAME annotation schema; only the ClinVar SOURCE differs — offline pulls it from a ``--custom`` ClinVar
VCF (surfaced under ``custom_annotations``), REST from the colocated variants' ``clin_sig``.

Privacy boundary preserved: takes a VCF PATH, returns only derived metrics / a table path / gene +
significance lists — never raw genotypes.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterator

from .variant_annotation import (
    ANNOTATION_COLUMNS, apply_variant_filters, classify_significance, parse_vep_result,
    summarize_annotations, write_standard_tables)

# VEP annotation flags we always request so the JSON carries the fields the REST parser reads
# (gene symbol, canonical transcript, SIFT/PolyPhen, and the population frequencies
# ``parse_vep_result`` collapses into ``max_af``). Kept as a module constant so the HPC smoke test
# can tune it in one place if a cache release renames a frequency flag.
_VEP_ANNOT_FLAGS: tuple[str, ...] = (
    "--symbol", "--canonical", "--biotype",
    "--sift", "b", "--polyphen", "b",
    "--af", "--af_gnomade", "--af_gnomadg",
)

# Falsy values for the plugin master switch (BIOAGENT_VEP_PLUGINS).
_OFF = {"", "0", "false", "off", "no"}


def vep_plugin_flags(env: "dict[str, str] | None" = None, *, enabled: "bool | None" = None,
                     cadd_snv: str = "", cadd_indels: str = "", alphamissense: str = "",
                     revel: str = "") -> tuple[str, ...]:
    """VEP ``--plugin`` (+ ``--mane_select``) flags for whichever predictor data is STAGED.

    Paths come from the explicit keyword args when given (the gateway injects them so they resolve
    INSIDE the vep.sif container), else from the environment (the local fallback path). OFF unless
    ``enabled`` / ``BIOAGENT_VEP_PLUGINS`` is truthy, and each predictor is added only when its file
    EXISTS — so a partially-staged cache still runs:
      - CADD (``BIOAGENT_VEP_CADD_SNV`` + optional ``BIOAGENT_VEP_CADD_INDELS``) → ``--plugin CADD,snv=…``
      - AlphaMissense (``BIOAGENT_VEP_ALPHAMISSENSE``) → ``--plugin AlphaMissense,file=…``
      - REVEL (``BIOAGENT_VEP_REVEL``) → ``--plugin REVEL,file=…``
    """
    e = os.environ if env is None else env
    if enabled is None:
        enabled = str(e.get("BIOAGENT_VEP_PLUGINS", "")).strip().lower() not in _OFF
    if not enabled:
        return ()
    cadd_snv = cadd_snv or e.get("BIOAGENT_VEP_CADD_SNV", "")
    cadd_indels = cadd_indels or e.get("BIOAGENT_VEP_CADD_INDELS", "")
    alphamissense = alphamissense or e.get("BIOAGENT_VEP_ALPHAMISSENSE", "")
    revel = revel or e.get("BIOAGENT_VEP_REVEL", "")
    flags: list[str] = []
    if cadd_snv and os.path.exists(cadd_snv):
        spec = f"CADD,snv={cadd_snv}" + (f",indels={cadd_indels}"
                                         if cadd_indels and os.path.exists(cadd_indels) else "")
        flags += ["--plugin", spec]
    if alphamissense and os.path.exists(alphamissense):
        flags += ["--plugin", f"AlphaMissense,file={alphamissense}"]
    if revel and os.path.exists(revel):
        flags += ["--plugin", f"REVEL,file={revel}"]
    if flags:
        flags.append("--mane_select")   # clinically-standard transcript per gene (cache carries MANE)
    return tuple(flags)


def build_norm_cmd(input_vcf: str, output_vcf: str, ref_fasta: str) -> list[str]:
    """A ``bcftools norm`` argv: split multiallelic sites AND left-align/trim indels against the
    reference FASTA (``-m-any -f``). Run BEFORE VEP so a non-canonical indel matches its ClinVar/gnomAD
    record instead of looking falsely absent (the operon variant-normalization step, inlined)."""
    return ["bcftools", "norm", "-m-any", "-f", ref_fasta, "-Oz", "-o", output_vcf, input_vcf]


# --- SpliceAI (OpenSpliceAI) splice-disruption scoring -----------------------
# SpliceAI predicts whether a variant creates or destroys an RNA splice site — the class of pathogenic
# variant that VEP's protein-level predictors (SIFT/PolyPhen/CADD/AlphaMissense) miss. OpenSpliceAI is
# the PyTorch reimplementation (Jin's chosen path); it runs in its OWN conda env on HPC3 (NOT inside
# vep.sif — it needs torch), so this stage calls the env's ``openspliceai variant`` binary DIRECTLY on
# the compute node. Each variant gets four delta scores (Acceptor/Donor × Gain/Loss); we keep the MAX
# (the splice-disruption signal — ≥0.5 = likely splice-altering, SpliceAI's high-precision cutoff) +
# which event, and merge them into the VEP rows by locus. It is ~50 s/variant on CPU, so it is a
# PANEL-STAGE tool: run it on the handful of variants left AFTER the known-gene / AF reduction, never on
# a whole WGS VCF (a hard variant-count cap enforces this).
_SPLICEAI_SITES: tuple[str, ...] = ("acceptor_gain", "acceptor_loss", "donor_gain", "donor_loss")
_SPLICEAI_ASSEMBLY: dict[str, str] = {"GRCh38": "grch38", "GRCh37": "grch37"}


def spliceai_is_enabled(env: "dict[str, str] | None" = None, *, enabled: "bool | None" = None) -> bool:
    """Whether the SpliceAI stage is ON — explicit ``enabled`` wins, else ``BIOAGENT_SPLICEAI`` (OFF by
    default, same falsy set as the VEP plugins)."""
    if enabled is not None:
        return bool(enabled)
    e = os.environ if env is None else env
    return str(e.get("BIOAGENT_SPLICEAI", "")).strip().lower() not in _OFF


def build_spliceai_cmd(input_vcf: str, output_vcf: str, *, bin_path: str, ref_fasta: str,
                       models_dir: str, annotation: str = "grch38", flank: int = 10000,
                       distance: int = 50, precision: int = 3, threads: int = 8,
                       home_dir: str = "") -> list[str]:
    """An ``openspliceai variant`` argv, run directly from the conda env (NOT via singularity — VEP's
    container has no torch). ``TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`` lets torch>=2.6 load the released
    full-model ``.pt`` files (trusted JHU CCB weights); ``OMP_NUM_THREADS`` bounds CPU inference.
    ``home_dir`` (a writable per-run dir) is pointed at by ``HOME``/``TORCH_HOME``/``XDG_CACHE_HOME`` so
    torch/pyfaidx have somewhere to write even under ``--containall`` (where the real HOME is unbound);
    the ref FASTA's ``.fai`` must already exist next to it (bound read-only) so pyfaidx never rebuilds."""
    env_vars = ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1", f"OMP_NUM_THREADS={threads}"]
    if home_dir:
        env_vars += [f"HOME={home_dir}", f"TORCH_HOME={home_dir}", f"XDG_CACHE_HOME={home_dir}"]
    return [
        "env", *env_vars,
        bin_path, "variant",
        "-R", ref_fasta, "-A", annotation,
        "-m", models_dir, "-t", "pytorch", "-f", str(flank),
        "-I", input_vcf, "-O", output_vcf, "-D", str(distance), "-p", str(precision),
    ]


def _row_locus(row: dict[str, Any]) -> "tuple[str, str, str, str] | None":
    """``(chrom, pos, ref, alt)`` for a VEP annotation row, from its echoed ``input`` line (falls back to
    ``location`` + ``allele``). The join key between the VEP rows and the SpliceAI output — both derive
    from the same normalized VCF, so the raw locus matches exactly."""
    parts = str(row.get("input", "")).split()
    if len(parts) >= 5:
        return (parts[0], parts[1], parts[3], parts[4])
    loc, allele = str(row.get("location", "")), str(row.get("allele", ""))
    if ":" in loc and "/" in allele:
        chrom, pos = loc.split(":", 1)
        ref, *alt = allele.split("/")
        if alt:
            return (chrom, pos, ref, alt[-1])
    return None


def write_spliceai_vcf(rows: list[dict[str, Any]], path: str | Path) -> int:
    """Write the (already panel/AF-reduced) annotation rows back to a minimal VCF for SpliceAI scoring —
    one ``CHROM POS . REF ALT . . .`` line per resolvable locus. A ``##contig`` header is emitted for
    EVERY chromosome present: OpenSpliceAI writes its output through pysam/htslib, which refuses to write
    a record whose contig is not declared in the header (``Invalid BCF, CONTIG id=0 not present``) — so a
    contig-less minimal VCF makes the whole SpliceAI stage fail. Returns how many loci were written."""
    loci = [locus for r in rows if (locus := _row_locus(r)) is not None]
    chroms = list(dict.fromkeys(locus[0] for locus in loci))   # distinct chroms, input order
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("##fileformat=VCFv4.2\n")
        for chrom in chroms:
            fh.write(f"##contig=<ID={chrom}>\n")
        fh.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
        for chrom, pos, ref, alt in loci:
            fh.write(f"{chrom}\t{pos}\t.\t{ref}\t{alt}\t.\t.\t.\n")
    return len(loci)


def parse_spliceai_vcf(path: str | Path) -> "dict[tuple[str, str, str, str], dict[str, Any]]":
    """Parse an OpenSpliceAI output VCF's ``OpenSpliceAI=ALT|SYMBOL|DS_AG|DS_AL|DS_DG|DS_DL|…`` INFO into
    ``{(chrom, pos, ref, alt): {spliceai_max_ds, spliceai_site}}`` — the MAX of the four delta scores and
    which splice event it is (acceptor/donor × gain/loss). Multiple gene annotations (comma-separated) →
    the max across them. Missing / malformed entries are skipped."""
    out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    p = Path(path)
    if not p.exists():
        return out
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 8:
                continue
            chrom, pos, _id, ref, alt = cols[0], cols[1], cols[2], cols[3], cols[4]
            info = cols[7]
            payload = ""
            for field in info.split(";"):
                if field.startswith("OpenSpliceAI=") or field.startswith("SpliceAI="):
                    payload = field.split("=", 1)[1]
                    break
            if not payload:
                continue
            best_ds, best_site = -1.0, ""
            for entry in payload.split(","):
                p_ = entry.split("|")
                if len(p_) < 6:
                    continue
                for idx, site in enumerate(_SPLICEAI_SITES):
                    ds = _to_float(p_[2 + idx])
                    if ds is not None and ds > best_ds:
                        best_ds, best_site = ds, site
            if best_ds >= 0.0:
                out[(chrom, pos, ref, alt)] = {"spliceai_max_ds": round(best_ds, 3),
                                               "spliceai_site": best_site}
    return out


def _to_float(v: Any) -> "float | None":
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def merge_spliceai(rows: list[dict[str, Any]],
                   scores: "dict[tuple[str, str, str, str], dict[str, Any]]") -> int:
    """Fill each row's ``spliceai_max_ds`` / ``spliceai_site`` from the parsed SpliceAI scores (joined on
    the raw locus). Returns how many rows got a score."""
    n = 0
    for r in rows:
        locus = _row_locus(r)
        s = scores.get(locus) if locus else None
        if s:
            r["spliceai_max_ds"] = s["spliceai_max_ds"]
            r["spliceai_site"] = s["spliceai_site"]
            n += 1
    return n


# Injectable subprocess runner (so command construction is unit-testable without bcftools/VEP).
Runner = Callable[[list[str]], Any]

# chr1 reference length is the single most reliable build signal in a VCF header — GRCh37/hg19 and
# GRCh38/hg38 differ here, so it disambiguates even when the ``##reference``/``assembly=`` tags are
# absent or wrong. Used to pick the matching VEP cache: annotating hg19 coordinates against a GRCh38
# cache runs cleanly but mis-assigns the gene at ~90% of sites (empirically verified on a real WGS VCF).
_CHR1_LEN_TO_BUILD: dict[str, str] = {"249250621": "GRCh37", "248956422": "GRCh38"}


def detect_assembly(header_text: str) -> str:
    """Best-effort genome build (``'GRCh37'`` | ``'GRCh38'`` | ``''``) from a VCF header. Prefers the
    chr1 contig length (unambiguous across builds), then falls back to explicit ``assembly=`` /
    ``##reference`` tags. Returns ``''`` when unknown so the caller keeps its configured default."""
    m = re.search(r"##contig=<ID=(?:chr)?1,length=(\d+)", header_text)
    if m and m.group(1) in _CHR1_LEN_TO_BUILD:
        return _CHR1_LEN_TO_BUILD[m.group(1)]
    low = header_text.lower()
    if any(tag in low for tag in ("grch37", "hg19", "b37")):
        return "GRCh37"
    if any(tag in low for tag in ("grch38", "hg38", "b38")):
        return "GRCh38"
    return ""


def _default_run(argv: list[str]) -> Any:
    import subprocess
    return subprocess.run(argv, capture_output=True, text=True)  # noqa: S603 - fixed tool argv


def _tail(proc: Any, n: int = 1500) -> str:
    return ((getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "")[-n:]).strip()


# --- command builders (pure) -------------------------------------------------


def build_filter_cmd(input_vcf: str, output_vcf: str, *, pass_only: bool = True,
                     regions_bed: str = "", sample: str = "") -> list[str]:
    """A ``bcftools view`` argv that streams the VCF to ``output_vcf`` (bgzipped), keeping only the
    wanted records. ``-f PASS,.`` keeps both hard-PASS sites AND ``FILTER='.'`` (no filter applied) —
    dropping ``.`` would silently discard every record in the many single-sample VCFs that never set
    a PASS. ``regions_bed`` restricts to a gene panel / region set; ``sample`` subsets one sample."""
    cmd = ["bcftools", "view"]
    if pass_only:
        cmd += ["-f", "PASS,."]
    if sample:
        cmd += ["-s", sample]
    if regions_bed:
        cmd += ["-R", regions_bed]
    cmd += ["-Oz", "-o", output_vcf, input_vcf]
    return cmd


def build_vep_cmd(input_vcf: str, output_json: str, cache_dir: str, *, assembly: str = "GRCh38",
                  fork: int = 8, clinvar_vcf: str = "",
                  annot_flags: tuple[str, ...] = _VEP_ANNOT_FLAGS,
                  plugins: tuple[str, ...] = (), ref_fasta: str = "", plugins_dir: str = "") -> list[str]:
    """An offline ``vep`` argv: annotate ``input_vcf`` against the local ``cache_dir`` (no network),
    forked ``fork`` ways, emitting one JSON object per line to ``output_json``. When ``clinvar_vcf``
    is set, ClinVar significance + condition + review-status stars (``CLNSIG``/``CLNDN``/``CLNREVSTAT``)
    are added via ``--custom``. ``plugins`` (see :func:`vep_plugin_flags`) add predictor scores; a
    ``ref_fasta`` enables HGVS ``c.``/``p.`` naming (``--hgvs``). All extras are additive — an empty
    ``plugins``/``ref_fasta`` reproduces the baseline SIFT/PolyPhen annotation exactly."""
    cmd = ["vep", "--offline", "--cache", "--dir_cache", cache_dir,
           "--assembly", assembly, "--species", "homo_sapiens",
           "--fork", str(int(fork)),
           "--input_file", input_vcf, "--format", "vcf",
           "--json", "--output_file", output_json,
           "--no_stats", "--force_overwrite"]
    cmd += list(annot_flags)
    if ref_fasta:
        cmd += ["--hgvs", "--fasta", ref_fasta]
    if plugins_dir:
        cmd += ["--dir_plugins", plugins_dir]   # load the plugin .pm scripts from a bind-mounted dir
    cmd += list(plugins)
    if clinvar_vcf:
        cmd += ["--custom",
                f"file={clinvar_vcf},short_name=ClinVar,format=vcf,type=exact,coords=0,"
                "fields=CLNSIG%CLNDN%CLNREVSTAT"]
    return cmd


# --- JSONL parsing (streamed, memory-bounded) --------------------------------


def _clinvar_from_custom(item: dict[str, Any]) -> list[str]:
    """ClinVar significance terms from a VEP ``--custom`` annotation. VEP surfaces a custom VCF's
    INFO fields under ``custom_annotations.<short_name>[].fields``; ClinVar's ``CLNSIG`` packs
    multiple terms with ``|`` / ``/`` separators (e.g. ``Pathogenic/Likely_pathogenic``) which we
    split into a flat list for :func:`classify_significance`."""
    out: list[str] = []
    for entry in ((item.get("custom_annotations") or {}).get("ClinVar") or []):
        raw = (entry.get("fields") or {}).get("CLNSIG") or (entry.get("fields") or {}).get("clnsig")
        if not raw:
            continue
        for part in str(raw).replace("|", ",").replace("/", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _clinvar_review_from_custom(item: dict[str, Any]) -> str:
    """ClinVar review status (``CLNREVSTAT``) from the ``--custom`` annotation — the confidence/"star"
    rating (e.g. ``criteria_provided,_multiple_submitters,_no_conflicts`` = 2 stars). Empty when the
    variant is not in ClinVar. Lets a report weight an expert-panel call above a single-submitter one."""
    for entry in ((item.get("custom_annotations") or {}).get("ClinVar") or []):
        raw = (entry.get("fields") or {}).get("CLNREVSTAT") or (entry.get("fields") or {}).get("clnrevstat")
        if raw:
            return str(raw)
    return ""


def parse_offline_vep_line(item: dict[str, Any]) -> dict[str, Any]:
    """One VEP ``--json`` element → the same flat annotation row the REST tool emits, then augment
    the ClinVar significance + review status from the ``--custom`` annotation when the cache's
    colocated variants carried none (the usual offline case)."""
    row = parse_vep_result(item)
    if not row.get("clin_sig"):
        clin = _clinvar_from_custom(item)
        if clin:
            row["clin_sig"] = clin
            row["clinical_significance"] = classify_significance(clin)
    review = _clinvar_review_from_custom(item)
    if review:
        row["clinvar_review_status"] = review
    return row


def iter_vep_json(json_path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield VEP JSON elements from a JSONL file one line at a time (skipping blank / malformed
    lines) — never loads the whole output into memory."""
    with open(json_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def annotate_from_vep_json(json_path: str | Path, *, max_variants: int = 0) -> list[dict[str, Any]]:
    """Stream-parse a VEP JSONL file into annotation rows. ``max_variants`` > 0 caps the count
    (0 = annotate every variant — the default for the offline WGS path)."""
    rows: list[dict[str, Any]] = []
    for item in iter_vep_json(json_path):
        rows.append(parse_offline_vep_line(item))
        if max_variants and len(rows) >= max_variants:
            break
    return rows


def _write_table(rows: list[dict[str, Any]], tables_dir: str | Path) -> str:
    """Write the FULL per-variant annotated table to ``<tables_dir>/variant_annotation.tsv`` (the
    same complete :data:`ANNOTATION_COLUMNS` schema the REST tool writes) and VERIFY it landed: the
    file must exist and its header must carry every column, else return '' so the caller flags a
    persistence failure instead of shipping a missing/degenerate table."""
    import csv
    import os

    if not rows:
        return ""
    path = Path(tables_dir) / "variant_annotation.tsv"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=ANNOTATION_COLUMNS, delimiter="\t", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    except OSError:
        return ""
    if not os.path.exists(path):
        return ""
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
    if header != list(ANNOTATION_COLUMNS):
        return ""
    return str(path)


# --- orchestration -----------------------------------------------------------


def run_offline_annotation(
    dataset_path: str | Path,
    workspace: str | Path,
    *,
    cache_dir: str,
    assembly: str = "GRCh38",
    fork: int = 8,
    clinvar_vcf: str = "",
    pass_only: bool = True,
    regions_bed: str = "",
    sample: str = "",
    max_variants: int = 0,
    max_pop_af: float = 0.0,
    genes: "list[str] | None" = None,
    # Predictor-plugin config — injected by the gateway so paths resolve inside vep.sif; each falls
    # back to the matching BIOAGENT_VEP_* / BIOAGENT_REF_FASTA env for the local path.
    plugins_enabled: "bool | None" = None,
    cadd_snv: str = "", cadd_indels: str = "", alphamissense: str = "", revel: str = "",
    plugins_dir: str = "", ref_fasta: str = "",
    # SpliceAI (OpenSpliceAI) splice-disruption scoring — a separate conda-env binary; injected by the
    # gateway or read from BIOAGENT_SPLICEAI_* env. OFF by default; runs only on the reduced variant set.
    spliceai_enabled: "bool | None" = None,
    spliceai_bin: str = "", spliceai_models: str = "", spliceai_max_variants: int = 0,
    # IRD annotation layers (HGMD / retina-specific exon / retina ATAC / dbscSNV splice) — tabix-based,
    # run on the reduced (post panel/AF) set; gated OFF by default. Paths injected by the gateway or read
    # from BIOAGENT_IRD_* env; each layer is skipped if its path is empty. See docs/ird_filter_spec.md.
    ird_annotate: "bool | None" = None,
    hgmd_path: str = "", retina_bed: str = "", atac_path: str = "", dbscsnv_template: str = "",
    run: Runner | None = None,
) -> dict[str, Any]:
    """Annotate a whole VCF offline: (optional) bcftools PASS-filter → offline VEP → stream-parse →
    summarise + write a table. Returns derived metrics only (never raw genotypes). ``run`` is the
    injectable subprocess runner (defaults to :func:`subprocess.run`)."""
    runner = run or _default_run
    ds = Path(dataset_path)
    if not ds.exists():
        return {"status": "error", "error": f"VCF not found: {ds}"}
    cache = Path(cache_dir)
    if not cache_dir or not cache.exists():
        return {"status": "error",
                "error": f"VEP cache dir not found: {cache_dir!r} — run deploy/vep/build_and_stage.sh"}

    ws = Path(workspace)
    work = ws / "work"
    tables = ws / "artifacts" / "tables"
    work.mkdir(parents=True, exist_ok=True)
    tables.mkdir(parents=True, exist_ok=True)

    # 1. Optional bcftools pre-filter (PASS / region panel / sample) — streamed, memory-bounded.
    vep_input: Path = ds
    if pass_only or regions_bed or sample:
        filtered = work / "filtered.vcf.gz"
        fcmd = build_filter_cmd(str(ds), str(filtered), pass_only=pass_only,
                                regions_bed=regions_bed, sample=sample)
        proc = runner(fcmd)
        if getattr(proc, "returncode", 1) != 0:
            return {"status": "error", "error": f"bcftools filter failed: {_tail(proc)}",
                    "cmd": " ".join(fcmd)}
        vep_input = filtered

    # 1b. Optional bcftools norm (split multiallelic + left-align indels) when a reference FASTA is
    #     staged — so non-canonical indels match ClinVar/gnomAD. Skipped gracefully otherwise.
    ref_fasta = ref_fasta or os.environ.get("BIOAGENT_REF_FASTA", "")
    ref_fasta = ref_fasta if (ref_fasta and os.path.exists(ref_fasta)) else ""
    normalized = False
    if ref_fasta:
        normed = work / "normalized.vcf.gz"
        ncmd = build_norm_cmd(str(vep_input), str(normed), ref_fasta)
        proc = runner(ncmd)
        if getattr(proc, "returncode", 1) != 0:
            return {"status": "error", "error": f"bcftools norm failed: {_tail(proc)}",
                    "cmd": " ".join(ncmd)}
        vep_input = normed
        normalized = True

    # 2. Offline VEP → JSONL (no network; forked over the local cache). Predictor plugins + HGVS are
    #    added only when their data is staged (config injected by the gateway, else env) — otherwise
    #    this is the baseline annotation.
    vep_json = work / "vep.jsonl"
    plugins = vep_plugin_flags(enabled=plugins_enabled, cadd_snv=cadd_snv, cadd_indels=cadd_indels,
                               alphamissense=alphamissense, revel=revel)
    plugins_dir = plugins_dir or os.environ.get("BIOAGENT_VEP_PLUGINS_DIR", "")
    plugins_dir = plugins_dir if (plugins_dir and os.path.isdir(plugins_dir)) else ""
    vcmd = build_vep_cmd(str(vep_input), str(vep_json), str(cache), assembly=assembly,
                         fork=fork, clinvar_vcf=clinvar_vcf, plugins=plugins, ref_fasta=ref_fasta,
                         plugins_dir=plugins_dir)
    proc = runner(vcmd)
    if getattr(proc, "returncode", 1) != 0:
        return {"status": "error", "error": f"VEP failed: {_tail(proc)}", "cmd": " ".join(vcmd)}
    if not vep_json.exists():
        return {"status": "error", "error": "VEP produced no output file", "cmd": " ".join(vcmd)}

    # 3. Stream-parse the JSONL (memory-bounded) → annotation rows.
    rows = annotate_from_vep_json(vep_json, max_variants=max_variants)
    if not rows:
        return {"status": "empty", "note": "VEP produced no annotations", "vep_json": str(vep_json)}

    # 3b. Rare-disease / known-gene reduction (Rui Chen's IRD workflow): drop common variants (gnomAD
    #     AF > cutoff) and/or restrict to a known disease-gene panel. Both default off (no-op).
    rows, filter_stats = apply_variant_filters(rows, max_pop_af=max_pop_af, genes=genes)
    if not rows:
        return {"status": "empty", "note": "no variants left after the AF / gene-panel filter",
                "variant_filters": filter_stats}

    # 3c. SpliceAI (OpenSpliceAI) — splice-disruption scoring on the (reduced) variant set. Gated (OFF
    #     by default). ~50 s/variant on CPU, so it is meant for the post-panel/AF set; there is NO cap by
    #     default (spliceai_max_variants=0), but a >0 value is an optional safety valve that skips it with
    #     a loud note if the set is still huge (else a whole-WGS run would take ~forever).
    spliceai_bin = spliceai_bin or os.environ.get("BIOAGENT_SPLICEAI_BIN", "")
    spliceai_models = spliceai_models or os.environ.get("BIOAGENT_SPLICEAI_MODELS", "")
    spliceai: dict[str, Any] = {"ran": False}
    if (spliceai_is_enabled(enabled=spliceai_enabled) and spliceai_bin and os.path.exists(spliceai_bin)
            and spliceai_models and os.path.isdir(spliceai_models) and ref_fasta):
        if spliceai_max_variants and len(rows) > spliceai_max_variants:
            spliceai["note"] = (f"skipped: {len(rows)} variants > cap {spliceai_max_variants} — apply a "
                                f"gene panel / max_pop_af first (SpliceAI is ~50 s/variant on CPU)")
        else:
            sa_in, sa_out = work / "spliceai_in.vcf", work / "spliceai_out.vcf"
            sa_home = work / "spliceai_home"
            sa_home.mkdir(exist_ok=True)
            n_written = write_spliceai_vcf(rows, sa_in)
            scmd = build_spliceai_cmd(str(sa_in), str(sa_out), bin_path=spliceai_bin,
                                      ref_fasta=ref_fasta, models_dir=spliceai_models,
                                      annotation=_SPLICEAI_ASSEMBLY.get(assembly, "grch38"),
                                      threads=fork, home_dir=str(sa_home))
            proc = runner(scmd)
            if getattr(proc, "returncode", 1) == 0 and sa_out.exists():
                n_scored = merge_spliceai(rows, parse_spliceai_vcf(sa_out))
                spliceai = {"ran": True, "n_input": n_written, "n_scored": n_scored}
            else:
                spliceai["note"] = f"spliceai failed: {_tail(proc)}"

    # 3d. IRD annotation layers (HGMD / retina-specific exon / retina ATAC / dbscSNV splice) + the
    #     reason_for_inclusion cascade — tabix lookups against the lab's staged reference files, on the
    #     reduced set. Gated OFF; a tabix miss on any file just skips that layer (never fatal).
    ird_layers: dict[str, Any] = {"ran": False}
    _ird_on = (ird_annotate if ird_annotate is not None
               else os.environ.get("BIOAGENT_IRD_ANNOTATE", "").strip().lower()
               in ("1", "true", "yes", "on"))
    hgmd_path = hgmd_path or os.environ.get("BIOAGENT_IRD_HGMD", "")
    retina_bed = retina_bed or os.environ.get("BIOAGENT_IRD_RETINA_EXONS", "")
    atac_path = atac_path or os.environ.get("BIOAGENT_IRD_ATAC", "")
    dbscsnv_template = dbscsnv_template or os.environ.get("BIOAGENT_IRD_DBSCSNV", "")
    if _ird_on and (hgmd_path or retina_bed or atac_path or dbscsnv_template):
        def _tabix(path: str, region: str) -> list[str]:
            try:
                proc = runner(["tabix", path, region])
                if getattr(proc, "returncode", 1) == 0:
                    return (getattr(proc, "stdout", "") or "").splitlines()
            except Exception:  # noqa: BLE001 - a tabix hiccup must not abort annotation
                pass
            return []
        from .ird_annotate import annotate_ird_layers
        annotate_ird_layers(rows, _tabix, hgmd_path=hgmd_path, retina_bed=retina_bed,
                            atac_path=atac_path, dbscsnv_template=dbscsnv_template)
        ird_layers = {"ran": True, "n_variants": len(rows),
                      "layers": [n for n, p in (("hgmd", hgmd_path), ("retina_exon", retina_bed),
                                                ("atac", atac_path), ("dbscsnv", dbscsnv_template)) if p]}

    # 4. Summarise (reuse the REST helper — identical schema) + write the per-variant table AND the
    #    five standard deliverable tables (same as the REST path — no run_code post-processing needed).
    summary = summarize_annotations(rows)
    table = _write_table(rows, tables)
    standard = write_standard_tables(summary, tables)
    # Which optional stages actually ran (surfaced so the report can state the annotation depth
    # honestly — e.g. "normalized: no" means indel ClinVar matches may be understated).
    predictors = [p for p in plugins if p not in ("--plugin", "--mane_select")]
    return {"status": "ok", "tool": "annotate_variants", "execution_mode": "offline_vep",
            "assembly": assembly, "n_input_variants": len(rows),
            "normalized": normalized, "predictors": predictors, "variant_filters": filter_stats,
            "spliceai": spliceai, "ird_layers": ird_layers,
            "annotated_table": table, "standard_tables": [Path(p).name for p in standard],
            "raw_data_to_llm": False, **summary}
