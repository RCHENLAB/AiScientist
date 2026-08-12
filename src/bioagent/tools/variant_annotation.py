"""Variant annotation via Ensembl VEP (REST) + ClinVar clinical significance.

Annotate a VCF's variants with functional consequence, the affected gene, predicted impact, and
ClinVar pathogenicity, using the public Ensembl VEP REST API (no key). ClinVar significance is taken
from VEP's ``colocated_variants[].clin_sig`` — the same ClinVar data, in one round-trip. Stdlib-only
(``urllib``/``gzip``) so it runs in a minimal sandbox; the HTTP call is injectable so the
parse/merge/summarise logic is unit-testable offline (no network in CI).

Endpoint: ``POST https://rest.ensembl.org/vep/{species}/region`` with
``{"variants": ["CHROM POS ID REF ALT . . .", …]}`` (≤200 variants/request). GRCh37 lives on
``https://grch37.rest.ensembl.org``. Adapted from the k-dense-ai/scientific-agent-skills references.

NETWORK: VEP/ClinVar are REST calls — this must run where the network is reachable (the eyeserver
local sandbox), NOT inside the network-off HPC3 analysis container.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

# GRCh38 is the default assembly; GRCh37/hg19 has its own REST host.
VEP_HOST = {"GRCh38": "https://rest.ensembl.org", "GRCh37": "https://grch37.rest.ensembl.org"}
_VEP_BATCH = 200   # the VEP REST region-POST caps at 200 variants per request

# ClinVar significance ranked most→least clinically actionable, for collapsing a clin_sig LIST
# (VEP returns e.g. ["uncertain_significance", "benign", "pathogenic"]) to a single headline label.
_CLIN_RANK = (
    "pathogenic", "likely_pathogenic", "risk_factor", "drug_response", "association",
    "conflicting_interpretations_of_pathogenicity", "uncertain_significance",
    "likely_benign", "benign", "protective", "not_provided",
)


def _existing_path(source: str) -> "Path | None":
    """``Path(source)`` iff it is a plausible, existing file path — else ``None``. Guards against
    calling ``Path(...).exists()`` on raw multi-line VCF TEXT (newline / too-long / OSError → text)."""
    if "\n" in source or len(source) >= 4096:
        return None
    try:
        p = Path(source)
        return p if p.exists() else None
    except OSError:
        return None


def _read_source(source: str) -> str:
    """Return VCF text from a path (optionally ``.gz``) or treat ``source`` as raw VCF text already."""
    p = _existing_path(source)
    if p is None:
        return source
    if source.endswith(".gz"):
        with gzip.open(p, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return p.read_text(encoding="utf-8", errors="replace")


def parse_vcf_variants(source: str, *, max_variants: int = 500) -> list[dict[str, Any]]:
    """Parse a VCF (path, ``.gz`` path, or raw text) into variant dicts
    ``{chrom, pos, id, ref, alt}``. Splits a multi-allelic ALT into one variant each; skips headers
    (``#``) and malformed rows; strips a leading ``chr``; caps at ``max_variants``."""
    out: list[dict[str, Any]] = []
    for line in _read_source(source).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t") if "\t" in line else line.split()
        if len(cols) < 5 or not cols[1].isdigit():
            continue
        chrom, pos, vid, ref = cols[0], int(cols[1]), cols[2], cols[3]
        for alt in cols[4].split(","):
            alt = alt.strip()
            if not alt or alt == ".":
                continue
            out.append({"chrom": chrom[3:] if chrom.lower().startswith("chr") else chrom,
                        "pos": pos, "id": vid if (vid and vid != ".") else ".",
                        "ref": ref, "alt": alt})
            if len(out) >= max_variants:
                return out
    return out


# A VCF FILTER cell counts as "passing" QC when it is PASS or the missing-value "." (per VCF spec,
# "." means no filter applied ⇒ not failed). Anything else (snp_filter, indel_filter, q10, …) FAILED.
_PASS_OK = {"PASS", ".", ""}


def read_vcf_for_annotation(
    source: str, *, max_variants: int = 500, pass_only: bool = True,
) -> dict[str, Any]:
    """Scan a VCF ONCE and return both the (capped) variants to annotate AND honest FILTER
    accounting — so the report can state real PASS/non-PASS counts instead of an unconditional
    "ALL PASS". Honours "first filter by PASS": when ``pass_only`` (the default, matching the
    offline line), only FILTER ∈ {PASS, .} records are annotated; non-PASS records are COUNTED but
    skipped. ``n_pass``/``n_nonpass`` are over the WHOLE file (record-level, before the cap);
    ``truncated`` is True iff the kept set was cut off at ``max_variants`` (so more would qualify)."""
    variants: list[dict[str, Any]] = []
    n_pass = n_nonpass = 0
    truncated = False
    for line in _read_source(source).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t") if "\t" in line else line.split()
        if len(cols) < 5 or not cols[1].isdigit():
            continue
        filt = (cols[6].strip() if len(cols) > 6 else ".") or "."
        is_pass = filt.upper() in _PASS_OK
        n_pass += int(is_pass)
        n_nonpass += int(not is_pass)
        if pass_only and not is_pass:
            continue            # dropped by the PASS filter — counted above, not annotated
        if len(variants) >= max_variants:
            truncated = True
            continue            # cap reached: keep scanning ONLY to finish the pass/non-pass tally
        chrom, pos, vid, ref = cols[0], int(cols[1]), cols[2], cols[3]
        for alt in cols[4].split(","):
            alt = alt.strip()
            if not alt or alt == ".":
                continue
            variants.append({"chrom": chrom[3:] if chrom.lower().startswith("chr") else chrom,
                             "pos": pos, "id": vid if (vid and vid != ".") else ".",
                             "ref": ref, "alt": alt})
            if len(variants) >= max_variants:
                truncated = True
                break
    return {"variants": variants, "n_pass": n_pass, "n_nonpass": n_nonpass,
            "n_records": n_pass + n_nonpass, "truncated": truncated}


def to_vep_region(variant: dict[str, Any]) -> str:
    """A variant dict → the VEP region-POST string ``"CHROM POS ID REF ALT . . ."``."""
    return (f"{variant['chrom']} {variant['pos']} {variant.get('id') or '.'} "
            f"{variant['ref']} {variant['alt']} . . .")


def classify_significance(clin_sig: "list[str] | None") -> str:
    """Collapse a ClinVar ``clin_sig`` list to the single most clinically actionable term
    (``''`` when the variant is not in ClinVar)."""
    if not clin_sig:
        return ""
    terms = {str(s).lower().replace(" ", "_") for s in clin_sig}
    for rank in _CLIN_RANK:
        if rank in terms:
            return rank
    return sorted(terms)[0]


def _max_allele_freq(item: dict[str, Any], alt: str) -> "float | None":
    """The maximum population allele frequency (gnomAD / 1000G) across the colocated variants — a
    rarity signal (rare/novel variants are likelier pathogenic). Prefers the ALT allele's frequencies,
    falls back to the global max; ``None`` when the variant has no frequency record (treat as rare)."""
    best: "float | None" = None
    for c in (item.get("colocated_variants") or []):
        freqs = c.get("frequencies") or {}
        pop_dicts = [freqs[alt]] if alt and alt in freqs else list(freqs.values())
        for pops in pop_dicts:
            for val in (pops or {}).values():
                try:
                    f = float(val)
                except (TypeError, ValueError):
                    continue
                if best is None or f > best:
                    best = f
    return best


def _first(d: dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among ``keys`` in ``d`` (VEP plugins vary the key spelling
    between JSON/VCF output); ``""`` when none is set — so a plugin-less run just leaves the field blank."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return ""


def _alphamissense_score(tc: dict[str, Any]) -> Any:
    """AlphaMissense pathogenicity from a transcript consequence. VEP's JSON output nests it as
    ``alphamissense: {am_pathogenicity, am_class}`` (a DICT), while the VCF/plugin form uses a flat key —
    so a plain ``_first(tc, "am_pathogenicity")`` silently misses the common JSON case. Unwrap both."""
    am = tc.get("alphamissense")
    if isinstance(am, dict):
        return am.get("am_pathogenicity", "")
    return _first(tc, "am_pathogenicity", "alphamissense", "AlphaMissense_score")


def _predictor_across_tcs(tcs: "list[dict[str, Any]]", picked: dict[str, Any], extract) -> Any:
    """A predictor value from the PICKED transcript, else the MANE / canonical one, else any transcript.
    VEP annotates CADD / REVEL / AlphaMissense on DIFFERENT transcript sets, so the most-severe
    transcript we report the consequence from often lacks a score that another (typically the clinically
    canonical MANE) transcript carries — without this, those scores are silently dropped."""
    seen: list[dict[str, Any]] = []
    for cand in (picked,
                 next((t for t in tcs if t.get("mane_select") or t.get("mane")), None),
                 next((t for t in tcs if t.get("canonical")), None)):
        if cand is not None and cand not in seen:
            seen.append(cand)
            v = extract(cand)
            if v not in (None, "", [], {}):
                return v
    for t in tcs:                                  # last resort: first transcript that carries it
        v = extract(t)
        if v not in (None, "", [], {}):
            return v
    return ""


def parse_vep_result(item: dict[str, Any]) -> dict[str, Any]:
    """One VEP response element → a flat annotation row. Every field is optional/defensive: picks the
    transcript consequence matching ``most_severe_consequence`` (else the first); the ClinVar
    significance + rsID from the colocated variants; in-silico deleteriousness (SIFT/PolyPhen, plus
    CADD/REVEL/AlphaMissense when the offline plugins are staged) from the transcript; HGVS + MANE
    naming; and the max gnomAD/1000G population allele frequency (rarity). Plugin/HGVS/MANE fields stay
    blank on the REST path and on a plugin-less cache."""
    tcs = item.get("transcript_consequences") or []
    msc = item.get("most_severe_consequence") or ""
    tc = next((t for t in tcs if msc in (t.get("consequence_terms") or [])), tcs[0] if tcs else {})
    clin: list[str] = []
    rsid = ""
    for c in (item.get("colocated_variants") or []):
        if c.get("clin_sig"):
            clin = list(c.get("clin_sig"))
        if not rsid and str(c.get("id", "")).startswith("rs"):
            rsid = str(c.get("id"))
    alt = (item.get("allele_string") or "").split("/")[-1]
    return {
        "input": item.get("input", ""),
        "location": f"{item.get('seq_region_name', '')}:{item.get('start', '')}",
        "allele": item.get("allele_string", ""),
        "gene_symbol": tc.get("gene_symbol", ""),
        "gene_id": tc.get("gene_id", ""),
        "consequence": msc,
        "impact": tc.get("impact", ""),
        "amino_acids": tc.get("amino_acids", ""),
        "sift": tc.get("sift_prediction", ""),
        "polyphen": tc.get("polyphen_prediction", ""),
        "max_af": _max_allele_freq(item, alt),
        "rsid": rsid,
        "clin_sig": clin,
        "clinical_significance": classify_significance(clin),
        # HGVS + MANE-Select naming (from --hgvs / --mane_select) and predictor plugins
        # (CADD / REVEL / AlphaMissense) — blank unless the offline cache has them staged.
        "hgvsc": _first(tc, "hgvsc"),
        "hgvsp": _first(tc, "hgvsp"),
        # MANE + the predictors are sourced across transcripts (prefer the MANE one) — VEP puts them on
        # different transcript sets than the most-severe one we pick for the consequence.
        "mane_select": _predictor_across_tcs(tcs, tc, lambda t: _first(t, "mane_select", "mane")),
        "cadd_phred": _predictor_across_tcs(tcs, tc, lambda t: _first(t, "cadd_phred", "CADD_PHRED")),
        "revel": _predictor_across_tcs(tcs, tc, lambda t: _first(t, "revel", "REVEL", "revel_score")),
        "alphamissense": _predictor_across_tcs(tcs, tc, _alphamissense_score),
        "clinvar_review_status": "",   # set by the offline path from ClinVar CLNREVSTAT (star rating)
        # SpliceAI (OpenSpliceAI) splice-disruption score — max of the 4 delta scores + which event;
        # filled by the offline path's SpliceAI stage (blank on REST / when the stage is off).
        "spliceai_max_ds": "",
        "spliceai_site": "",
    }


HttpPost = Callable[[str, bytes, "dict[str, str]"], bytes]


def _urlopen_post(url: str, body: bytes, headers: "dict[str, str]") -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:   # noqa: S310 - fixed Ensembl REST host
        return resp.read()


def vep_annotate(
    variants: "list[dict[str, Any]]",
    *,
    assembly: str = "GRCh38",
    species: str = "human",
    http_post: "HttpPost | None" = None,
    sleep_s: float = 0.2,
) -> list[dict[str, Any]]:
    """Annotate variants via the Ensembl VEP REST region-POST, batched (≤200/request), returning
    annotation rows. ``http_post(url, body, headers) -> bytes`` is injectable so the batching/parsing
    is exercised offline in tests; the default hits the live REST host."""
    if not variants:
        return []
    host = VEP_HOST.get(assembly, VEP_HOST["GRCh38"])
    url = f"{host}/vep/{species}/region"
    post = http_post or _urlopen_post
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    rows: list[dict[str, Any]] = []
    for i in range(0, len(variants), _VEP_BATCH):
        batch = variants[i:i + _VEP_BATCH]
        body = json.dumps({"variants": [to_vep_region(v) for v in batch]}).encode("utf-8")
        raw = post(url, body, headers)
        results = json.loads(raw.decode("utf-8")) if raw else []
        rows.extend(parse_vep_result(r) for r in (results or []))
        if i + _VEP_BATCH < len(variants) and sleep_s:
            time.sleep(sleep_s)
    return rows


def _is_rare(af: "float | None", cutoff: float = 0.01) -> bool:
    """A variant is rare/novel (clinically likelier deleterious) if it has no population-frequency
    record or its max allele frequency is below ``cutoff`` (default 1%)."""
    return af is None or af < cutoff


def _fnum(v: Any) -> "float | None":
    """``v`` as a float, or None if it is blank / non-numeric (a plugin-less field)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_damaging(row: dict[str, Any]) -> bool:
    """Damaging by any available line of evidence: VEP impact / SIFT / PolyPhen, plus the
    CADD/REVEL/AlphaMissense predictors WHEN staged (operon cutoffs: CADD≥20, REVEL>0.5,
    AlphaMissense>0.564). Blank predictor fields (REST path / plugin-less cache) are simply ignored,
    so this is backward-compatible with a SIFT/PolyPhen-only run."""
    cadd, revel, am = _fnum(row.get("cadd_phred")), _fnum(row.get("revel")), _fnum(row.get("alphamissense"))
    spliceai = _fnum(row.get("spliceai_max_ds"))
    return (row.get("impact") == "HIGH"
            or str(row.get("sift", "")).startswith("deleterious")
            or str(row.get("polyphen", "")) in ("probably_damaging", "possibly_damaging")
            or (cadd is not None and cadd >= 20)
            or (revel is not None and revel > 0.5)
            or (am is not None and am > 0.564)
            or (spliceai is not None and spliceai >= 0.5))   # SpliceAI high-precision splice-altering


def apply_variant_filters(
    rows: "list[dict[str, Any]]", *, max_pop_af: float = 0.0, genes: "list[str] | None" = None,
) -> "tuple[list[dict[str, Any]], dict[str, Any]]":
    """Post-annotation reduction for a rare-disease / known-gene workflow (Rui Chen's IRD direction):

    - ``max_pop_af`` > 0 drops COMMON variants (gnomAD max allele frequency ABOVE the cutoff — e.g.
      0.01 removes everything >1%, since a rare-disease causal variant is rare). Variants with no
      frequency record are novel ⇒ kept.
    - ``genes`` restricts to a known disease-gene panel (match on ``gene_symbol``) — focus on the known
      IRD genes first; expand to the whole genome only if that search is negative.

    Both default to no-op, so a general run is unchanged. Returns ``(kept_rows, stats)``."""
    panel = {str(g).upper() for g in genes if str(g).strip()} if genes else None
    kept: list[dict[str, Any]] = []
    n_common = n_offpanel = 0
    for r in rows:
        af = r.get("max_af")
        if max_pop_af and af is not None and af > max_pop_af:
            n_common += 1
            continue
        if panel is not None and str(r.get("gene_symbol", "")).upper() not in panel:
            n_offpanel += 1
            continue
        kept.append(r)
    stats = {"n_input": len(rows), "n_kept": len(kept),
             "n_dropped_common_af": n_common, "n_dropped_off_panel": n_offpanel,
             "max_pop_af": max_pop_af or None, "gene_panel_size": len(panel) if panel else 0}
    return kept, stats


def _ird_flagged(row: "dict[str, Any]") -> bool:
    """True when the IRD annotation layers gave this variant independent inclusion evidence — an HGMD
    exact match or a dbscSNV splice prediction (the ``reason_for_inclusion`` cascade). Absent/blank
    when the IRD layers did not run, so this only ADDS to the generic high-impact/deleterious selection."""
    if row.get("hgmd_match"):
        return True
    return str(row.get("reason_for_inclusion") or "").startswith("splice_prediction")


def summarize_annotations(rows: "list[dict[str, Any]]") -> dict[str, Any]:
    """Counts by consequence / impact / clinical significance / rarity, the ClinVar pathogenic list,
    and the high-priority SHORTLIST of NOVEL candidates: rare (below the AF cutoff) + high-impact or
    predicted-deleterious variants that are NOT yet in ClinVar. Pathogenic/likely-pathogenic ClinVar
    calls are their OWN list (``pathogenic_variants``); the shortlist is the unclassified candidates to
    validate — the two lists are disjoint and match the standard deliverable tables + the study question."""
    from collections import Counter

    from .ird_prioritize import annotate_disease_model

    # Tag each variant with the inheritance model(s) its GENE qualifies for (dominant ≤1e-4 /
    # recessive ≥2 alleles ≤5e-3 / X) — computed over ALL rows so compound-het counting sees both
    # alleles. This is the lab's disease-model prioritization (docs/ird_filter_spec.md); it ranks the
    # shortlist (model-fitting candidates lead), it does not drop anything. On a panel-restricted IRD
    # run this is what pulls real disease genes (CRB1/USH2A) above off-target noise (mito on chrM has
    # no autosome/X model, so it sinks).
    rows = annotate_disease_model(rows)

    def _pv(r: dict[str, Any]) -> dict[str, Any]:
        return {"gene": r.get("gene_symbol"), "location": r.get("location"),
                "consequence": r.get("consequence"), "impact": r.get("impact"),
                "significance": r.get("clinical_significance"), "sift": r.get("sift"),
                "polyphen": r.get("polyphen"), "max_af": r.get("max_af"), "rsid": r.get("rsid"),
                "spliceai_max_ds": r.get("spliceai_max_ds"),
                "disease_model": r.get("disease_model") or [],
                "reason_for_inclusion": r.get("reason_for_inclusion") or "",
                "hgmd_match": bool(r.get("hgmd_match")),
                "retina_specific_exon": r.get("retina_specific_exon") or ""}

    by_cons = Counter(r.get("consequence") or "unknown" for r in rows)
    by_impact = Counter(r.get("impact") or "unknown" for r in rows)
    by_clin = Counter(r.get("clinical_significance") or "not_in_clinvar" for r in rows)
    pathogenic = [r for r in rows
                  if r.get("clinical_significance") in ("pathogenic", "likely_pathogenic")]
    high_priority = [
        r for r in rows
        if not (r.get("clinical_significance") or "")            # NOT in ClinVar (novel/unclassified)
        and _is_rare(r.get("max_af"))                            # rare, AND either:
        and (_is_damaging(r) or _ird_flagged(r))                 #   high-impact/deleterious OR IRD-flagged (HGMD/splice)
    ]
    # Rank: HGMD-matched first (strongest IRD evidence), then disease-model-fitting, then impact severity.
    _impact_rank = {"HIGH": 0, "MODERATE": 1, "LOW": 2, "MODIFIER": 3}
    high_priority.sort(key=lambda r: (not r.get("hgmd_match"),
                                      not r.get("fits_disease_model"),
                                      _impact_rank.get(r.get("impact"), 9)))
    return {
        "n_variants": len(rows),
        "by_consequence": dict(by_cons.most_common()),
        "by_impact": dict(by_impact.most_common()),
        "by_clinical_significance": dict(by_clin.most_common()),
        "n_rare": sum(1 for r in rows if _is_rare(r.get("max_af"))),
        "n_pathogenic": len(pathogenic),
        "pathogenic_variants": [_pv(r) for r in pathogenic],
        "n_high_priority": len(high_priority),
        "n_high_priority_disease_model": sum(1 for r in high_priority
                                             if r.get("fits_disease_model")),
        "high_priority_variants": [_pv(r) for r in high_priority[:50]],
    }


# The COMPLETE per-variant annotation schema. Every annotated table (REST here + offline in
# vcf_offline.py) writes exactly these columns, so the "annotated results table" the user asks for
# always carries gene / position / rsID / consequence / impact / ClinVar / gnomAD AF / SIFT / PolyPhen
# — never a degenerate one-column (e.g. FILTER-only) export.
ANNOTATION_COLUMNS = ["location", "allele", "gene_symbol", "gene_id", "consequence", "impact",
                      "amino_acids", "sift", "polyphen", "max_af", "rsid", "clinical_significance",
                      # added when the offline predictor plugins / HGVS / MANE are staged (blank otherwise):
                      "hgvsc", "hgvsp", "mane_select", "cadd_phred", "revel", "alphamissense",
                      "clinvar_review_status",
                      # added when the offline SpliceAI stage runs (blank otherwise):
                      "spliceai_max_ds", "spliceai_site",
                      # added when the IRD annotation layers run (blank otherwise):
                      "reason_for_inclusion", "hgmd_match", "hgmd_mutations_nearby",
                      "retina_specific_exon", "atac_peak", "ada_score", "rf_score"]


def _artifacts_tables_dir() -> "Path | None":
    """The bundle ``tables/`` dir: ``$BIOAGENT_ARTIFACTS/tables`` if set, else ``$BIOAGENT_WORK/tables``,
    else ``None``. The earlier code returned '' the moment ``BIOAGENT_ARTIFACTS`` was unset — which is
    exactly how run 09a48f3cf62f shipped an empty ``annotated_table`` and the model hand-rolled a broken
    FILTER-only CSV. The BIOAGENT_WORK fallback makes the table land even when only that env is set."""
    for env in ("BIOAGENT_ARTIFACTS", "BIOAGENT_WORK"):
        base = os.environ.get(env)
        if base:
            return Path(base) / "tables"
    return None


def _write_table(rows: "list[dict[str, Any]]", dest: "Path | str | None" = None) -> str:
    """Persist the FULL per-variant annotation table to ``<tables>/variant_annotation.tsv`` and VERIFY
    it landed: after writing, assert the file exists (``os.path.exists``) and its header carries EVERY
    :data:`ANNOTATION_COLUMNS` column. Returns the path, or ``''`` if there is no writable tables dir or
    the verification fails — so the caller can flag a persistence failure instead of silently shipping a
    missing/degenerate table."""
    import csv

    if not rows:
        return ""
    out_dir = Path(dest) if dest is not None else _artifacts_tables_dir()
    if out_dir is None:
        return ""
    path = out_dir / "variant_annotation.tsv"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=ANNOTATION_COLUMNS, delimiter="\t", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    except OSError:
        return ""
    # Persistence assertions: the table must exist on disk and expose the complete column schema
    # (guards against the empty / one-column export that made the annotated table useless).
    if not os.path.exists(path):
        return ""
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
    if header != ANNOTATION_COLUMNS:
        return ""
    return str(path)


# The five standard deliverable tables `annotate_variants` writes DETERMINISTICALLY from a
# summarize_annotations() result — so the orchestrator never hand-writes (and botches) this CSV /
# summary-dict code. (The skills/variant_output_tables/ skill stays for CUSTOM thresholds/columns.)
STANDARD_TABLES = (
    "variant_consequence_distribution.csv", "variant_impact_distribution.csv",
    "variant_clinical_significance.csv", "clinvar_pathogenic_variants.csv",
    "high_priority_variants.csv",
)


def write_standard_tables(summary: dict[str, Any], tables_dir: "Path | str | None" = None) -> list[str]:
    """Write the five standard variant deliverable tables (+ a data/ summary JSON) from a
    :func:`summarize_annotations` result, best-effort. Returns the paths written. Distributions come
    from the ``by_*`` counts; the ClinVar pathogenic list and the novel high-priority shortlist from
    the two disjoint variant lists — the SAME content the model used to build by hand."""
    import csv
    import json

    out_dir = Path(tables_dir) if tables_dir is not None else _artifacts_tables_dir()
    if out_dir is None:
        return []
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    written: list[str] = []

    def _dist(mapping: dict, name: str, label: str) -> None:
        total = sum(mapping.values()) or 1
        p = out_dir / name
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([label, "Count", "Percentage"])
            for value, count in mapping.items():
                w.writerow([value, count, f"{100 * count / total:.2f}%"])
        written.append(str(p))

    _dist(summary.get("by_consequence") or {}, "variant_consequence_distribution.csv", "Consequence")
    _dist(summary.get("by_impact") or {}, "variant_impact_distribution.csv", "Impact")
    _dist(summary.get("by_clinical_significance") or {}, "variant_clinical_significance.csv",
          "Clinical Significance")

    pcsv = out_dir / "clinvar_pathogenic_variants.csv"
    with pcsv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Gene", "Position", "rsID", "Disease"])
        for r in summary.get("pathogenic_variants") or []:
            w.writerow([r.get("gene") or "", r.get("location") or "", r.get("rsid") or "",
                        r.get("significance") or ""])
    written.append(str(pcsv))

    hcsv = out_dir / "high_priority_variants.csv"
    with hcsv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Gene", "Position", "rsID", "Consequence", "Impact",
                    "gnomAD_AF", "SIFT", "PolyPhen", "Disease_Model",
                    "Reason_for_Inclusion", "HGMD", "Retina_Exon", "ClinVar_Status"])
        for r in summary.get("high_priority_variants") or []:
            w.writerow([r.get("gene") or "", r.get("location") or "", r.get("rsid") or "",
                        r.get("consequence") or "", r.get("impact") or "",
                        "" if r.get("max_af") is None else r.get("max_af"),
                        r.get("sift") or "", r.get("polyphen") or "",
                        ";".join(r.get("disease_model") or []) or ".",
                        r.get("reason_for_inclusion") or "",
                        "yes" if r.get("hgmd_match") else "",
                        r.get("retina_specific_exon") or "", "not_in_clinvar"])
    written.append(str(hcsv))

    # A machine-readable summary alongside the tables (matches the skill's output; also lets the
    # report's task-kind detector recognise a variant run even if no tables/ glob matched).
    data_dir = out_dir.parent / "data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "annotated_results_summary.json").write_text(json.dumps({
            "total_variants": summary.get("n_variants"),
            "consequence_distribution": summary.get("by_consequence") or {},
            "impact_distribution": summary.get("by_impact") or {},
            "clinical_significance_distribution": summary.get("by_clinical_significance") or {},
            "n_pathogenic_clinvar": summary.get("n_pathogenic"),
            "n_rare_variants_af_lt_0.1": summary.get("n_rare"),
            "n_high_priority_rare_deleterious": summary.get("n_high_priority"),
            "tables_produced": list(STANDARD_TABLES),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass
    return written


def annotate_variants_rest(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The REST annotation path (Ensembl VEP REST + ClinVar). Suits SMALL VCFs and hosts WITHOUT the
    offline stack; for WGS-scale VCFs use the offline line (:mod:`bioagent.tools.vcf_offline`), which
    this also serves as the in-process fallback when the HPC3 job can't run. Caps at ``max_variants``
    (default 500) because the public REST API is rate-limited — the offline path removes that cap."""
    src = str(args.get("vcf_path")
              or (ctx.decisions.get("dataset_path") if ctx and getattr(ctx, "decisions", None) else "")
              or "").strip()
    if not src:
        return {"status": "error", "error": "no VCF given (pass vcf_path or load a .vcf dataset)"}
    looks_like_text = ("\n" in src) or src.lstrip().startswith("#")
    if not looks_like_text and _existing_path(src) is None:
        return {"status": "error", "error": f"VCF not found: {src}"}
    cap = int(args.get("max_variants", 500) or 500)
    pass_only = bool(args.get("pass_only", True))   # honour "first filter by PASS" (offline default too)
    try:
        scan = read_vcf_for_annotation(src, max_variants=cap, pass_only=pass_only)
    except Exception as exc:  # noqa: BLE001 - surface a clean tool error, never crash the loop
        return {"status": "error", "error": f"VCF parse failed: {type(exc).__name__}: {exc}"}
    variants = scan["variants"]
    if not variants:
        note = "no variants parsed from the VCF"
        if pass_only and scan["n_nonpass"] and not scan["n_pass"]:
            note = f"no PASS variants: all {scan['n_nonpass']} record(s) failed the VCF FILTER column"
        return {"status": "empty", "note": note,
                "n_pass": scan["n_pass"], "n_nonpass": scan["n_nonpass"]}
    assembly = "GRCh37" if str(args.get("assembly", "")).lower() in ("grch37", "hg19") else "GRCh38"
    try:
        rows = vep_annotate(variants, assembly=assembly, species=str(args.get("species", "human")))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"VEP REST failed: {type(exc).__name__}: {exc}",
                "hint": "VEP REST needs network to reach the Ensembl API (available on-demand)."}
    # Rare-disease / known-gene reduction (Rui Chen's IRD workflow): drop common variants (gnomAD
    # AF > cutoff) and, when a panel is given, keep only known disease-gene variants. Both default off.
    rows, filter_stats = apply_variant_filters(
        rows, max_pop_af=float(args.get("max_pop_af", 0) or 0), genes=args.get("genes") or None)
    summary = summarize_annotations(rows)
    table = _write_table(rows)
    standard = write_standard_tables(summary)   # the 5 deliverable tables — no run_code needed
    # REAL FILTER accounting — never an unconditional "ALL PASS". PASS filtering runs BEFORE the cap:
    # only FILTER∈{PASS,.} records are annotated (when pass_only), and n_pass/n_nonpass count the whole
    # file at the record level, so the report can state e.g. "172 PASS / 328 non-PASS" honestly.
    out = {"status": "ok", "tool": "annotate_variants", "execution_mode": "rest", "assembly": assembly,
           "n_input_variants": len(variants),
           "filter": "PASS-only" if pass_only else "all-records (no PASS filter)",
           "variant_filters": filter_stats,
           "n_pass": scan["n_pass"], "n_nonpass": scan["n_nonpass"],
           "n_records_scanned": scan["n_records"],
           "annotated_table": table, "annotated_table_columns": list(ANNOTATION_COLUMNS),
           "standard_tables": [Path(p).name for p in standard],
           **summary}
    if not table:
        out["warning_table"] = (
            "could NOT persist the per-variant annotation table (no writable BIOAGENT_ARTIFACTS / "
            "BIOAGENT_WORK dir) — do NOT hand-roll a CSV from the FILTER column; fix the artifacts dir.")
    # LOUD, never silent: if we filled the cap, the VCF was truncated and every downstream count /
    # distribution describes only this first-N slice — NOT the whole file. A WGS-scale VCF hitting this
    # via the REST path means the offline VEP line (BIOAGENT_VARIANT_ON_HPC=1, no cap) should be used.
    if scan["truncated"]:
        out["truncated"] = True
        out["n_annotated"] = len(variants)
        out["warning"] = (
            f"TRUNCATED: only the first {cap} {'PASS ' if pass_only else ''}variants were annotated "
            f"(REST cap reached). Further qualifying variants were NOT included, so all counts and "
            f"distributions describe a {cap}-variant slice, not the whole file. For a WGS-scale VCF "
            f"enable the offline VEP line (set BIOAGENT_VARIANT_ON_HPC=1) to annotate every variant.")
    return out


def make_variant_annotation_tool() -> Any:
    """The ``annotate_variants`` tool: VEP + ClinVar annotation of a VCF. Imported lazily so this
    module has no import-time dependency on the agents package. Defaults to the REST path; the
    gateway swaps in the offline HPC3 line via the tool router when ``variant_on_hpc`` is set."""
    from ..agents.research_harness import HarnessTool

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return annotate_variants_rest(args, ctx)

    return HarnessTool(
        "annotate_variants",
        "Annotate a VCF's variants with functional consequence, the affected gene, predicted impact, "
        "and ClinVar clinical significance (pathogenicity), via the Ensembl VEP REST API. Pass "
        "`vcf_path` (a .vcf or .vcf.gz on the host, or the run's loaded dataset). Filters to PASS "
        "variants by default (pass_only) and returns REAL FILTER counts (n_pass / n_nonpass) — never "
        "assume 'all PASS'. Writes the COMPLETE per-variant annotated table to "
        "`tables/variant_annotation.tsv` (gene/position/rsID/consequence/impact/ClinVar/gnomAD AF/"
        "SIFT/PolyPhen) AND the five standard deliverable tables (consequence/impact/clinical-"
        "significance distributions, the ClinVar pathogenic list, the rare-unclassified high-priority "
        "shortlist) — so you do NOT need ANY run_code to post-process: report directly from these "
        "files. Returns counts by consequence / impact / clinical significance and the list of "
        "pathogenic + likely-pathogenic variants (gene, location, rsID). For a rare-disease study, set "
        "`max_pop_af` (e.g. 0.01) to drop common variants and/or `genes` to focus on a known "
        "disease-gene panel first. Reports ONLY what VEP/ClinVar return; never invents a gene, "
        "consequence, or significance.",
        {"type": "object", "properties": {
            "vcf_path": {"type": "string",
                         "description": "path to a .vcf/.vcf.gz (defaults to the run's dataset)"},
            "assembly": {"type": "string", "description": "GRCh38 (default) or GRCh37/hg19"},
            "species": {"type": "string", "description": "Ensembl species (default human)"},
            "pass_only": {"type": "boolean",
                          "description": "annotate only FILTER=PASS/'.' variants (default true)"},
            "max_variants": {"type": "integer", "description": "cap variants annotated (default 500)"},
            "max_pop_af": {"type": "number",
                           "description": "drop variants with gnomAD population AF above this (e.g. 0.01 "
                                          "for a rare-disease study); 0 = keep all (default)"},
            "genes": {"type": "array", "items": {"type": "string"},
                      "description": "restrict to this known disease-gene panel (gene symbols); empty = all"},
            "regions_bed": {"type": "string",
                            "description": "offline line only: a BED to restrict annotation to a "
                                           "panel's regions BEFORE VEP (big compute saving on a WGS VCF)"}}},
        _exec,
        reads_private_data=True, category="annotation",
    )
