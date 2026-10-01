"""IRD annotation layers — HGMD / retina-specific exon / retina ATAC / dbscSNV splice — plus the lab's
``reason_for_inclusion`` cascade. Ports ``annotate_filter/annotationTools.py`` (docs/ird_filter_spec.md).

Each annotator is PURE: it takes the *already-fetched* reference lines for a locus (the HPC3 side does
the ``tabix``/bedtools lookup and passes the output here) plus the variant's coordinates, and returns
the annotation. This keeps the file-format parsing testable offline; the thin HPC3 runner in
``vcf_offline`` supplies the lines. Reference file formats (verified on HPC3):

* HGMD  ``…parsedforVCFannotationandindexingfixed.txt.gz`` — TSV, cols
  ``CHROM POS REF ALT GENE ISOFORM HGVS_cDNA DISEASE_MUTATION PHENOTYPE REFERENCE PUBMED …`` (CHROM has
  NO ``chr`` prefix). Exact CHROM/POS/REF/ALT ⇒ a MATCH.
* Retina exons — BED ``chrom start end [gene …]`` (0-based half-open).
* ATAC — narrowPeak ``chrom start end name …`` (cols 1-3 are the interval).
* dbscSNV ``dbscSNV1.1.chr*`` — TSV; key = ``(chr, pos, ref, alt)`` (cols 0-3, no ``chr`` prefix),
  ``ada_score`` = col 16, ``rf_score`` = col 17 (per the lab's annotationTools).
"""
from __future__ import annotations

from typing import Iterable

from .ird_prioritize import parse_af

# VEP consequences that count as "protein-altering" (coding non-synonymous or splice-region) — the VEP
# equivalent of the lab's ANNOVAR "ExonicFunc != synonymous / Func startswith splicing" test.
PROTEIN_ALTERING = {
    "missense_variant", "stop_gained", "stop_lost", "start_lost", "frameshift_variant",
    "inframe_insertion", "inframe_deletion", "protein_altering_variant", "coding_sequence_variant",
    "transcript_ablation", "transcript_amplification", "splice_acceptor_variant",
    "splice_donor_variant", "splice_region_variant", "splice_donor_5th_base_variant",
    "splice_donor_region_variant", "splice_polypyrimidine_tract_variant",
}


def _norm_chrom(c: str) -> str:
    c = str(c or "").strip()
    return c[3:] if c.lower().startswith("chr") else c


def _f(v) -> "float | None":
    try:
        s = str(v).strip()
        return float(s) if s and s != "." else None
    except (TypeError, ValueError):
        return None


def interval_overlap(lines: Iterable[str], pos: int) -> bool:
    """True if the 1-based ``pos`` falls in any interval of these BED/narrowPeak lines (cols 1-3 =
    chrom, start[0-based], end). A 1-based ``pos`` is inside ``[start, end)`` iff ``start < pos <= end``."""
    for ln in lines:
        f = ln.rstrip("\n").split("\t")
        if len(f) < 3:
            continue
        try:
            start, end = int(f[1]), int(f[2])
        except ValueError:
            continue
        if start < pos <= end:
            return True
    return False


def retina_exon_genes(lines: Iterable[str], pos: int) -> "list[str]":
    """Gene labels (BED cols ≥4) of any retina-specific exon covering ``pos`` — empty if none."""
    genes: list[str] = []
    for ln in lines:
        f = ln.rstrip("\n").split("\t")
        if len(f) < 3:
            continue
        try:
            start, end = int(f[1]), int(f[2])
        except ValueError:
            continue
        if start < pos <= end:
            genes.extend(g for g in f[3:] if g and g not in genes)
    return genes


def hgmd_nearby(lines: Iterable[str], chrom: str, pos: int, ref: str, alt: str) -> "tuple[str, bool]":
    """Return ``(annotation, is_match)`` for HGMD entries in the fetched window (the caller tabixes a
    ±15 bp window). Each entry ⇒ ``chrom:pos:ref>alt:gene:disease:pubmed``; an EXACT chrom/pos/ref/alt
    entry is prefixed ``MATCH-`` and placed first. ``is_match`` is True iff any exact match exists."""
    qchrom = _norm_chrom(chrom)
    items: list[str] = []
    is_match = False
    for ln in lines:
        f = ln.rstrip("\n").split("\t")
        if len(f) < 5 or f[0].startswith("#"):
            continue
        h_chrom, h_pos, h_ref, h_alt, gene = f[0], f[1], f[2], f[3], f[4]
        disease = f[7] if len(f) > 7 else ""
        pubmed = f[10] if len(f) > 10 else ""
        info = f"{h_chrom}:{h_pos}:{h_ref}>{h_alt}:{gene}:{disease}:{pubmed}"
        if (_norm_chrom(h_chrom), h_pos, h_ref, h_alt) == (qchrom, str(pos), ref, alt):
            items.insert(0, "MATCH-" + info)
            is_match = True
        else:
            items.append(info)
    return ("&".join(items), is_match)


def dbscsnv_scores(lines: Iterable[str], chrom: str, pos: int, ref: str, alt: str) -> "tuple[float | None, float | None]":
    """``(ada_score, rf_score)`` for the (chrom,pos,ref,alt) row in the fetched dbscSNV lines, else
    ``(None, None)``. ada = col 16, rf = col 17 (0-based), key = cols 0-3 (chrom has no ``chr``)."""
    qchrom = _norm_chrom(chrom)
    for ln in lines:
        f = ln.rstrip("\n").split("\t")
        if len(f) < 18:
            continue
        if (_norm_chrom(f[0]), f[1], f[2], f[3]) == (qchrom, str(pos), ref, alt):
            return _f(f[16]), _f(f[17])
    return None, None


def is_protein_altering(consequence: str) -> bool:
    """VEP most-severe consequence ⇒ protein-altering / splice-region (the lab's inclusion class)."""
    if not consequence:
        return False
    # A VEP consequence field can be '&'-joined; any protein-altering term qualifies.
    return any(c.strip() in PROTEIN_ALTERING for c in str(consequence).split("&"))


def inclusion_reason(variant: dict, *, cutoff: float = 0.005, splice: float = 0.6) -> str:
    """The lab's ``reason_for_inclusion`` cascade (spec §2), first match wins:

    HGMD exact match (any freq) → too-common drop (``none``) → dbscSNV splice ≥ ``splice`` →
    protein-altering/splice-region → ``none``. (The lab's ``variant_exceptions_list`` keep-list is not
    reproduced — we have no such list.) Reads ``hgmd_match``, ``max_af``, ``ada_score``/``rf_score``,
    ``consequence`` off the (already-annotated) variant dict.
    """
    if variant.get("hgmd_match"):
        return "HGMD_match"
    if parse_af(variant.get("max_af")) >= cutoff:
        return "none"
    ada, rf = _f(variant.get("ada_score")), _f(variant.get("rf_score"))
    if (ada is not None and ada >= splice) or (rf is not None and rf >= splice):
        return f"splice_prediction>{splice}"
    if is_protein_altering(variant.get("consequence", "")):
        return "protein-altering"
    return "none"


# --- batch runner (wired into the offline annotation on HPC3) -------------------------------------

def _split_location(loc: str) -> "tuple[str, int | None]":
    s = str(loc or "")
    if ":" not in s:
        return s, None
    c, _, p = s.partition(":")
    p = p.split("-", 1)[0]                       # a range 'chr1:10-12' → start
    try:
        return c, int(p)
    except ValueError:
        return c, None


def _split_allele(allele: str) -> "tuple[str, str]":
    ref, _, alt = str(allele or "").partition("/")
    return ref, alt.split("/", 1)[0]            # first ALT if multiple


def annotate_ird_layers(
    rows: list[dict],
    tabix,
    *,
    hgmd_path: str = "",
    retina_bed: str = "",
    atac_path: str = "",
    dbscsnv_template: str = "",
    window: int = 15,
    set_reason: bool = True,
) -> list[dict]:
    """Add the IRD annotation fields to each row IN PLACE (and return ``rows``). ``tabix`` is a callable
    ``(file_path, region) -> list[str]`` — the HPC3 runner shells out to ``tabix``; tests pass a fake.
    A layer whose path is empty is skipped. Chromosome conventions per the verified file formats: the
    BED/narrowPeak files are ``chr``-prefixed; HGMD + dbscSNV are not (region uses the bare contig). The
    dbscSNV file is per-chromosome — ``dbscsnv_template`` is formatted with ``chrom`` (e.g.
    ``".../dbscSNV1.1.{chrom}"``). Sets ``reason_for_inclusion`` last (unless ``set_reason`` is False).
    """
    for r in rows:
        chrom, pos = _split_location(r.get("location"))
        if pos is None:
            continue
        ref, alt = _split_allele(r.get("allele"))
        bare = _norm_chrom(chrom)                                   # '1'   (HGMD / dbscSNV)
        chr_pref = chrom if chrom.lower().startswith("chr") else "chr" + chrom  # 'chr1' (BED/peak)
        if retina_bed:
            hits = retina_exon_genes(tabix(retina_bed, f"{chr_pref}:{pos}-{pos}"), pos)
            r["retina_specific_exon"] = ";".join(hits) if hits else ""
        if atac_path:
            r["atac_peak"] = interval_overlap(tabix(atac_path, f"{chr_pref}:{pos}-{pos}"), pos)
        if hgmd_path:
            ann, m = hgmd_nearby(tabix(hgmd_path, f"{bare}:{max(1, pos - window)}-{pos + window}"),
                                 chrom, pos, ref, alt)
            r["hgmd_mutations_nearby"], r["hgmd_match"] = ann, m
        if dbscsnv_template:
            path = dbscsnv_template.format(chrom=chr_pref)
            ada, rf = dbscsnv_scores(tabix(path, f"{bare}:{pos}-{pos}"), chrom, pos, ref, alt)
            if ada is not None:
                r["ada_score"] = ada
            if rf is not None:
                r["rf_score"] = rf
        if set_reason:
            r["reason_for_inclusion"] = inclusion_reason(r)
    return rows
