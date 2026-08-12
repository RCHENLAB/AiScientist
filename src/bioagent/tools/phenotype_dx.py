"""Phenotype-driven differential diagnosis for IRD cases.

Answers the question Rui Chen raised: IRD symptoms overlap heavily, so a phenotype alone cannot pin the
diagnosis — but phenotype + the VCF genetic findings can yield a CONFIDENCE-ranked list of candidate
DISEASES ("RP 70% / LCA 20% / ..."). Two tracks, deliberately NOT blended into one number:

  * PRIMARY (calibrated) — LIRICAL: fuses the patient's HPO terms with the variant shortlist into a
    per-disease POST-TEST PROBABILITY via a likelihood-ratio model over the curated HPO/OMIM
    annotations. This is the trusted confidence, and it is what handles symptom overlap rigorously
    (rare/specific symptoms carry more information than common ones).
  * EVIDENCE (long tail) — a literature track (PaperQA2; placeholder here): for gene-disease
    associations LIRICAL CANNOT reach (not yet in OMIM/HPOA — newly reported genes, phenotype
    expansions), it returns a ClinGen-style gene-disease VALIDITY tier + citations. A tier is an
    evidence GRADE, NOT a probability, so it is never averaged into LIRICAL's number — it can only
    ATTACH support to a LIRICAL call, or RESCUE a candidate LIRICAL missed, for human review.

Status: SCAFFOLD, not wired into the gateway. LIRICAL is not yet staged on HPC3 (``run_lirical`` is
gated), and the PaperQA2 track is a placeholder matching the interface contract in
``docs/paperqa2_evidence_layer_contract.md``. What IS implemented + tested here — the data model, the
LIRICAL TSV parser, and the two-track reconciliation — does not depend on either external system.
See ``docs/phenotype_gene_confidence_rag_spec.md``.
"""
from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ClinGen gene-disease validity tiers — the currency of the LITERATURE track, strongest first. Disputed/
# Refuted are contradictory evidence: they rank below the positive tiers so they never pass a positive
# threshold (they can only argue AGAINST a call, never rescue one).
CLINGEN_TIERS = ("DEFINITIVE", "STRONG", "MODERATE", "LIMITED", "DISPUTED", "REFUTED", "NONE")
_TIER_RANK = {t: i for i, t in enumerate(CLINGEN_TIERS)}


def tier_at_least(tier: str, threshold: str) -> bool:
    """True if ``tier`` is at least as strong as ``threshold`` (DEFINITIVE strongest). Unknown → NONE."""
    return _TIER_RANK.get((tier or "NONE").upper(), _TIER_RANK["NONE"]) <= \
        _TIER_RANK.get((threshold or "NONE").upper(), _TIER_RANK["NONE"])


def _f(v: Any) -> "float | None":
    """Parse a LIRICAL numeric cell; accepts a fraction ('0.967') or a percentage ('96.7%')."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    try:
        x = float(s)
    except ValueError:
        return None
    return x / 100.0 if pct else x


@dataclass
class DiseaseCandidate:
    """One candidate disease in the differential. ``posttest_prob`` is LIRICAL's calibrated posterior
    (None when the candidate came only from the literature track — we never invent a probability). The
    ``evidence_*`` fields carry the literature track's ClinGen grade + citations, kept SEPARATE from the
    probability on purpose."""

    disease_name: str
    disease_id: str = ""                 # OMIM/ORPHA curie, e.g. "OMIM:613835"
    gene: str = ""                       # gene SYMBOL — LIRICAL's TSV does NOT emit this (see below)
    entrez_gene_id: str = ""             # e.g. "NCBIGene:24" — what genotype-aware LIRICAL DOES emit;
    #                                      reconcile keys on the symbol, so entrez->symbol must be mapped
    #                                      (LIRICAL's staged hgnc_complete_set.txt) before the merge.
    variants: str = ""                   # genotype-aware only: the scored variant(s) + pathogenicity + GT
    posttest_prob: "float | None" = None
    pretest_prob: "float | None" = None
    composite_lr: "float | None" = None
    matched_hpo: list[str] = field(default_factory=list)
    evidence_tier: str = ""              # ClinGen tier (literature) — NOT a probability
    evidence_pmids: list[str] = field(default_factory=list)
    evidence_status: str = ""            # graded | contradicted | unsupported | ungraded | "" (not asked)
    #                                      distinguishes "the corpus said nothing" (ungraded — must not
    #                                      count against a candidate) from "papers came back and none
    #                                      support it" (unsupported). See phenotype_evidence.grade_evidence.
    sources: set[str] = field(default_factory=set)     # {"lirical", "literature"}
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["sources"] = sorted(self.sources)
        return d


def parse_lirical_tsv(text: str) -> list[DiseaseCandidate]:
    """Parse LIRICAL's TSV output into ranked :class:`DiseaseCandidate` rows. LIRICAL prefixes metadata
    lines with ``!``; the first non-``!``/``#`` line is the header. Columns are mapped BY NAME
    (case-insensitive) so this survives minor version differences — verify the header names against your
    LIRICAL version (``diseaseName``/``diseaseCurie``/``posttestprob``/``pretestprob``/``compositeLR``)."""
    header: "list[str] | None" = None
    out: list[DiseaseCandidate] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("!") or s.startswith("#"):
            continue
        cells = line.rstrip("\n").split("\t")
        if header is None:
            header = [c.strip().lower() for c in cells]
            continue
        rec = dict(zip(header, cells))
        out.append(DiseaseCandidate(
            disease_name=rec.get("diseasename") or rec.get("disease") or "",
            disease_id=rec.get("diseasecurie") or rec.get("diseaseid") or "",
            # LIRICAL v2.4 TSV emits ``entrezGeneId`` (+ ``variants``) in genotype-aware mode and NO gene
            # column in phenotype-only mode — verified on HPC3. Keep the ``gene`` symbol lookup as a
            # fallback for other formats/versions, but expect the identity to arrive as entrez.
            gene=(rec.get("gene") or rec.get("genesymbol") or "").strip(),
            entrez_gene_id=(rec.get("entrezgeneid") or "").strip(),
            variants=(rec.get("variants") or "").strip(),
            posttest_prob=_f(rec.get("posttestprob") or rec.get("posttestprobability")),
            pretest_prob=_f(rec.get("pretestprob")),
            composite_lr=_f(rec.get("compositelr")),
            sources={"lirical"},
        ))
    return out


def paperqa2_evidence(gene: str, disease: str, hpo_terms: Iterable[str] = (), *,
                      runner: "Callable[..., dict] | None" = None) -> dict[str, Any]:
    """The literature EVIDENCE track (PaperQA2). Placeholder until the corpus/QA is wired: returns
    ``status='not_enabled'`` unless a ``runner`` implementing the contract is injected. Contract (see
    ``docs/paperqa2_evidence_layer_contract.md``): given (gene, candidate disease, HPO terms) it returns
    ``{association: bool, clingen_tier: <CLINGEN_TIERS>, evidence: [{pmid, quote, study_type}]}``,
    GROUNDED in retrieved passages — never an ungrounded model number, never a probability."""
    if runner is None:
        return {"status": "not_enabled", "gene": gene, "disease": disease,
                "association": False, "clingen_tier": "NONE", "evidence": []}
    out = runner(gene=gene, disease=disease, hpo_terms=list(hpo_terms)) or {}
    tier = str(out.get("clingen_tier") or "NONE").upper()
    if tier not in _TIER_RANK:
        tier = "NONE"
    # ``evidence_status``/``notes`` are grading PROVENANCE, not part of the original contract: they
    # say WHY a record is negative — nothing retrieved vs. retrieved-and-unsupportive vs. refuted —
    # which is the distinction ``adjudicate`` needs and a bare ``association=False`` cannot carry.
    # Absent (a runner written to the plain contract) they default to "", which adjudicate reads as
    # "not asked" and scores as LIRICAL-only, so an older runner behaves exactly as before.
    return {"status": "ok", "gene": gene, "disease": out.get("disease") or disease,
            "association": bool(out.get("association")),
            "clingen_tier": tier, "evidence": list(out.get("evidence") or []),
            "evidence_status": str(out.get("evidence_status") or ""),
            "notes": list(out.get("notes") or [])}


@dataclass
class DifferentialResult:
    ranked: list[DiseaseCandidate]              # LIRICAL-ranked primary differential (posttest_prob desc)
    literature_rescued: list[DiseaseCandidate]  # LIRICAL missed, literature-supported → for human review

    def as_dict(self) -> dict[str, Any]:
        return {"ranked": [c.as_dict() for c in self.ranked],
                "literature_rescued": [c.as_dict() for c in self.literature_rescued]}


def reconcile(lirical: list[DiseaseCandidate], literature: "list[dict[str, Any]]",
              *, rescue_threshold: str = "STRONG") -> DifferentialResult:
    """Two-track merge that does NOT blend the currencies (a calibrated probability and an evidence grade
    are different things):

    * ATTACH — literature evidence (ClinGen tier + PMIDs) is attached to the matching LIRICAL candidate
      as SUPPORT; ``posttest_prob`` is never modified.
    * RESCUE — a literature-supported association LIRICAL did NOT surface, whose ClinGen tier is at least
      ``rescue_threshold``, is added to a SEPARATE list flagged for human review, with ``posttest_prob``
      left None (we do not fabricate a probability the model cannot calibrate).

    ``literature`` is a list of :func:`paperqa2_evidence` dicts (each carries gene/disease/tier/evidence)."""
    # Index the literature by gene. CONTRADICTORY records (DISPUTED/REFUTED) carry ``association=False``
    # but MUST be indexed too — they are the only channel by which the literature can argue against a
    # curated LIRICAL call, and dropping them (as this did originally) made a conflict invisible. They
    # still cannot RESCUE anything: ``tier_at_least`` puts them below every positive threshold.
    def _informative(ev: dict[str, Any]) -> bool:
        if ev.get("status") not in (None, "ok") or not ev.get("gene"):
            return False
        if ev.get("association") or ev.get("clingen_tier", "").upper() in ("DISPUTED", "REFUTED"):
            return True
        # A searched-but-unsupportive record is negative AND tier NONE, yet it is not the same as
        # never asking: passages came back and none supported the link. It must reach the candidate
        # so ``adjudicate`` can apply its (small) haircut. It can still never RESCUE anything —
        # ``tier_at_least("NONE", …)`` fails every positive threshold.
        return str(ev.get("evidence_status") or "") == "unsupported"

    by_gene: dict[str, dict[str, Any]] = {}
    for ev in literature:
        if _informative(ev):
            by_gene.setdefault(ev["gene"], ev)          # first informative record per gene wins

    ranked = sorted(lirical, key=lambda c: (c.posttest_prob is None, -(c.posttest_prob or 0.0)))
    seen_genes: set[str] = set()
    for c in ranked:
        seen_genes.add(c.gene)
        ev = by_gene.get(c.gene)
        if ev:
            c.evidence_tier = ev.get("clingen_tier", "")
            c.evidence_pmids = [e.get("pmid") for e in ev.get("evidence", []) if e.get("pmid")]
            c.evidence_status = str(ev.get("evidence_status") or "")
            c.sources.add("literature")

    rescued: list[DiseaseCandidate] = []
    for gene, ev in by_gene.items():
        if gene in seen_genes:
            continue
        if tier_at_least(ev.get("clingen_tier", ""), rescue_threshold):
            rescued.append(DiseaseCandidate(
                disease_name=ev.get("disease") or "",
                gene=gene,
                evidence_tier=ev.get("clingen_tier", ""),
                evidence_pmids=[e.get("pmid") for e in ev.get("evidence", []) if e.get("pmid")],
                evidence_status=str(ev.get("evidence_status") or ""),
                sources={"literature"},
                flags=["lirical_missed_literature_supported"],
            ))
    return DifferentialResult(ranked=ranked, literature_rescued=rescued)


# --- adjudication: ONE ranked differential, with the literature weighted above LIRICAL --------------
#
# :func:`reconcile` above is the PROVENANCE layer — it keeps the two tracks side by side and never
# moves ``posttest_prob``, so what LIRICAL actually said stays auditable forever. This is the
# DECISION layer stacked on top of it: it answers "so what is the diagnosis?", and here the two
# tracks are deliberately weighed against each other, because a clinician needs one ordered list.
#
# The weighting is asymmetric ON PURPOSE — the literature outranks LIRICAL when they disagree:
# LIRICAL's posterior is only as current as the OMIM/HPOA curation it reads, and that curation lags
# the literature by months to years. When a retrieved, cited body of work contradicts a curated call,
# the curated call is the stale one. So a REFUTED tier sinks a 0.96 LIRICAL candidate, and a STRONG
# literature-only candidate outranks a mid-probability LIRICAL one.
#
# What is NOT done: ``posttest_prob`` is never overwritten, and no probability is invented for a
# literature-only candidate. ``final_score`` is a RANKING score on its own named axis, not a
# calibrated probability, and every candidate carries the ``agreement`` branch that produced it.

# Positive tiers map onto 0..1; contradictory tiers go NEGATIVE, which is what lets them sink a
# candidate rather than merely fail to lift it.
TIER_STRENGTH: dict[str, float] = {
    "DEFINITIVE": 1.00, "STRONG": 0.80, "MODERATE": 0.50, "LIMITED": 0.25,
    "DISPUTED": -0.60, "REFUTED": -1.00, "NONE": 0.0,
}

LITERATURE_WEIGHT = 0.65     # > LIRICAL_WEIGHT: the literature wins a disagreement
LIRICAL_WEIGHT = 0.35
# A candidate the corpus actively failed to support keeps LIRICAL's ranking, minus a haircut. Small
# on purpose: the corpus is BOUNDED (~the IRD papers), so "no support here" is weak evidence, not the
# same thing as the refutation branch above.
UNSUPPORTED_PENALTY = 0.85


def tier_strength(tier: str) -> float:
    """A ClinGen tier on the ranking axis: positive = support, negative = contradiction, 0 = no signal."""
    return TIER_STRENGTH.get((tier or "NONE").upper(), 0.0)


def adjudicate(result: DifferentialResult, *,
               literature_weight: float = LITERATURE_WEIGHT,
               lirical_weight: float = LIRICAL_WEIGHT) -> list[dict[str, Any]]:
    """Merge the two reconciled tracks into ONE ranked differential, literature-weighted.

    Each candidate lands in exactly one ``agreement`` branch, and the branch — not a single blended
    formula — decides how it is scored:

    ``concordant``
        both tracks positive → ``literature_weight·tier + lirical_weight·posttest_prob``. The
        literature's larger share is what makes it dominate on disagreement.
    ``conflict``
        the literature DISPUTES/REFUTES a LIRICAL candidate → the same weighted sum, but the tier
        is negative, so a high LIRICAL probability is dragged below every uncontradicted candidate.
        This is the "literature wins" case, and it is flagged for the reviewer.
    ``literature_only``
        LIRICAL never surfaced it (not curated, or LIRICAL could not run) → scored on the tier
        alone. This is the gap-fill: without it these candidates are simply absent from the answer.
    ``lirical_only``
        the literature was not asked, or the corpus returned nothing (``ungraded``) → scored on
        ``posttest_prob`` alone. NO penalty: silence from a bounded corpus is absence of data.
    ``unsupported``
        passages came back and none supported the link → ``posttest_prob`` minus a small haircut.

    Returns plain dicts sorted by ``final_score`` (desc), each carrying both raw tracks so a reader
    can always see what each one contributed.
    """
    out: list[dict[str, Any]] = []

    for c in result.ranked:
        prob = c.posttest_prob or 0.0
        tier = (c.evidence_tier or "").upper()
        status = (c.evidence_status or "").lower()
        strength = tier_strength(tier)

        if status == "contradicted" or tier in ("DISPUTED", "REFUTED"):
            agreement = "conflict"
            score = literature_weight * strength + lirical_weight * prob
            note = (f"the literature {tier.lower()}s this association — it outranks LIRICAL's "
                    f"{prob:.0%} here; review before reporting")
        elif status == "unsupported":
            agreement = "unsupported"
            score = prob * UNSUPPORTED_PENALTY
            note = ("LIRICAL's call; the corpus returned related passages but none supporting this "
                    "gene–disease link (the corpus is bounded — treat as weak, not as refutation)")
        elif "literature" in c.sources and strength > 0:
            agreement = "concordant"
            score = literature_weight * strength + lirical_weight * prob
            note = f"both tracks agree: LIRICAL {prob:.0%}, literature {tier}"
        else:
            agreement = "lirical_only"
            score = prob
            note = ("LIRICAL only — the literature track was not asked or the corpus returned "
                    "nothing (absence of data, not evidence of absence)")

        d = c.as_dict()
        d.update({"final_score": round(score, 4), "agreement": agreement, "decision_note": note})
        out.append(d)

    # The gap-fill: candidates LIRICAL never produced enter the SAME ranked list rather than a side
    # list, because a differential a clinician has to cross-reference against a second list is one
    # they will read as "LIRICAL's answer, plus footnotes".
    for c in result.literature_rescued:
        strength = tier_strength(c.evidence_tier)
        d = c.as_dict()
        d.update({
            "final_score": round(strength, 4),
            "agreement": "literature_only",
            "decision_note": (f"LIRICAL did not surface this (not curated, or LIRICAL could not "
                              f"run); the literature grades it {(c.evidence_tier or 'NONE').upper()}. "
                              f"No post-test probability exists for it — ranked on evidence alone."),
        })
        out.append(d)

    out.sort(key=lambda d: -d["final_score"])
    for i, d in enumerate(out, 1):
        d["rank"] = i
    return out


def hpo_release_drift(data_dir: str, lexicon_version: str) -> list[str]:
    """Do WE and LIRICAL speak the same HPO release? -> a note if not (``[]`` when they agree or either
    side is unknown).

    Our bundled lexicon (which resolves the free text) and LIRICAL's staged ``hp.json`` (which resolves
    the IDs into disease likelihoods) are updated by DIFFERENT hands: ours by re-running
    ``scripts/build_hpo_lexicon.py`` and committing, LIRICAL's by whoever next runs ``lirical download``.
    They match today (both 2026-06-23, byte-identical hp.json — verified on HPC3 2026-07-15), and nothing
    keeps them matched. Drift is SILENT and one-sided: a term we still map to may be obsolete in
    LIRICAL's newer ontology, and it would simply stop matching — no error, just a quieter differential.
    Cheap enough to check every run (the release stamp is in hp.json's header)."""
    if not data_dir:
        return []
    from .hpo_terms.index import hp_json_release, release_date

    theirs = hp_json_release(str(Path(data_dir) / "hp.json"))
    ours = release_date(lexicon_version)
    if not theirs or not ours or theirs == ours:
        return []
    return [f"HPO release mismatch: the free-text→HPO mapper uses {ours}, LIRICAL's data dir has "
            f"{theirs}. Terms retired between the two will silently stop matching — regenerate the "
            f"lexicon with `python scripts/build_hpo_lexicon.py --hp-json {data_dir}/hp.json`."]


# --- LIRICAL input (Phenopacket) + CLI (pure, testable) ----------------------
# LIRICAL's assembly flag speaks hg19/hg38; the rest of the codebase (VEP) speaks GRCh37/GRCh38. Map so
# a caller can pass either. The Exomiser variant DB is chosen by this same axis (-e19 vs -e38).
_ASSEMBLY_ALIASES: dict[str, str] = {
    "grch37": "hg19", "hg19": "hg19", "b37": "hg19", "37": "hg19",
    "grch38": "hg38", "hg38": "hg38", "b38": "hg38", "38": "hg38",
}


def normalize_assembly(assembly: str) -> str:
    """A VCF/VEP assembly name → LIRICAL's ``hg19``/``hg38``. Unknown → ``hg38`` (LIRICAL's default)."""
    return _ASSEMBLY_ALIASES.get(str(assembly or "").strip().lower(), "hg38")


# --- chr-prefix normalization (Exomiser expects bare contig names: 1, X, MT — not chr1/chrX) ---------
# The Exomiser variant DBs use Ensembl-style bare chromosome names, so a chr-prefixed VCF (UCSC/GATK
# hg19, e.g. the lab's WGS output) silently fails to match at the genotype step. The lab's own Exomiser
# pipeline ships a remove_chr.py for exactly this. We DETECT first and strip only when needed (a VCF
# that is already bare is passed through untouched), and — unlike the lab's v10 workaround — we do NOT
# touch genotypes (LIRICAL v2.4 splits multiallelic 1/2 calls correctly on its own).


def vcf_uses_chr_prefix(vcf_path: str) -> bool:
    """True if the VCF's contigs / records use a ``chr`` prefix (``chr1``) rather than bare Ensembl names
    (``1``). Reads only the header + the first data record (never the whole WGS file): the first
    ``##contig`` decides, else the first record's CHROM. False on any read error (treat as bare)."""
    opener = gzip.open if str(vcf_path).endswith(".gz") else open
    try:
        with opener(vcf_path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("##contig="):
                    m = re.match(r"##contig=<ID=([^,>]+)", line)
                    if m:
                        return m.group(1).lower().startswith("chr")
                elif not line.startswith("#"):
                    return line[:1].isalnum() and line.lower().startswith("chr")
    except OSError:
        return False
    return False


def strip_chr_prefix(in_path: str, out_path: str) -> int:
    """Stream-rewrite a VCF removing a leading ``chr`` from BOTH the ``##contig`` header IDs and the CHROM
    column of each record (so header and records stay consistent for htsjdk). Memory-bounded (line by
    line — safe on a WGS-size VCF); returns the number of records rewritten. ``chrM`` → ``M`` (Exomiser
    hg19 mito is ``MT``; an unmatched ``M`` just drops the handful of mito calls, never IRD drivers)."""
    n = 0
    in_open = gzip.open if str(in_path).endswith(".gz") else open
    with in_open(in_path, "rt", encoding="utf-8", errors="replace") as fin, \
            open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if line.startswith("##contig=<ID=chr"):
                fout.write("##contig=<ID=" + line[len("##contig=<ID=chr"):])
            elif line.startswith("#"):
                fout.write(line)
            elif line.startswith("chr"):
                fout.write(line[3:])
                n += 1
            else:
                fout.write(line)
    return n


def build_phenopacket(hpo_terms: Iterable[str], *, sample_id: str = "sample-1",
                      excluded: Iterable[str] = (), labels: "dict[str, str] | None" = None,
                      case_id: str = "bioagent-case", created: str = "2024-01-01T00:00:00Z",
                      hpo_version: str = "2023-10-09") -> dict[str, Any]:
    """A minimal GA4GH **Phenopacket v2** carrying the patient's observed (and negated) HPO terms — the
    stable, self-describing input we feed LIRICAL (concentrating the version-fragile surface into a few
    CLI flags instead of the phenotype list). ``labels`` optionally names each term (cosmetic; LIRICAL
    resolves labels from ``hp.json`` regardless). Excluded terms carry ``excluded: true`` so LIRICAL
    treats them as *absent* findings (which lower a disease's likelihood), not missing data."""
    labels = labels or {}
    feats: list[dict[str, Any]] = []
    for hp in hpo_terms:
        hp = str(hp).strip()
        if hp:
            feats.append({"type": {"id": hp, "label": labels.get(hp, "")}})
    for hp in excluded:
        hp = str(hp).strip()
        if hp:
            feats.append({"type": {"id": hp, "label": labels.get(hp, "")}, "excluded": True})
    return {
        "id": case_id,
        "subject": {"id": sample_id},
        "phenotypicFeatures": feats,
        "metaData": {
            "created": created,
            "createdBy": "bioagent",
            "resources": [{
                "id": "hp", "name": "human phenotype ontology", "namespacePrefix": "HP",
                "url": "http://purl.obolibrary.org/obo/hp.owl", "version": hpo_version,
                "iriPrefix": "http://purl.obolibrary.org/obo/HP_",
            }],
            "phenopacketSchemaVersion": "2.0",
        },
    }


def build_lirical_cmd(*, observed: Iterable[str], data_dir: str, output_dir: str,
                      prefix: str = "lirical", negated: Iterable[str] = (), assembly: str = "hg38",
                      vcf_path: str = "", exomiser_dir: str = "", sample_id: str = "",
                      output_format: str = "tsv", base_cmd: tuple[str, ...] = ("lirical",)) -> list[str]:
    """A LIRICAL v2.4 ``prioritize`` argv (CLI-args mode). ``observed`` / ``negated`` are the patient's
    HPO term IDs, passed comma-separated (``-p`` / ``-n``). Runs PHENOTYPE-ONLY (posterior from symptoms
    alone) unless BOTH a ``vcf_path`` AND the matching Exomiser ``exomiser_dir`` are given, in which case
    the variants are scored too (the genotype-aware posterior). LIRICAL runs phenotype-only when
    ``--assembly`` is unset, so we withhold ``--vcf``/``--assembly``/``-ed*`` together when the Exomiser
    data is absent rather than let it error.

    Flags are pinned to the LIRICAL version baked in ``deploy/lirical/lirical.def`` and were VERIFIED
    against ``lirical prioritize --help`` on HPC3 (2026-07-14, v2.4.1): ``-p`` observed / ``-n`` negated /
    ``-d`` data / ``-o`` outdir / ``-x`` prefix / ``-f`` format / ``--vcf`` / ``--assembly`` / ``-ed19``
    / ``-ed38`` Exomiser data DIRECTORY / ``--sample-id``. This is the SINGLE place to tune them if a
    future LIRICAL renames one (a runtime arg; no image rebuild). Kept pure so it is unit-tested without
    a live LIRICAL, exactly like ``vcf_offline.build_vep_cmd``."""
    obs = ",".join(h for h in (str(x).strip() for x in observed) if h)
    cmd = [*base_cmd, "prioritize",
           "-p", obs,
           "-d", data_dir,
           "-o", output_dir,
           "-x", prefix,
           "-f", output_format]
    neg = ",".join(h for h in (str(x).strip() for x in negated) if h)
    if neg:
        cmd += ["-n", neg]
    if vcf_path and exomiser_dir:
        asm = normalize_assembly(assembly)
        cmd += ["--vcf", vcf_path, "--assembly", asm,
                ("-ed19" if asm == "hg19" else "-ed38"), exomiser_dir]
        if sample_id:
            cmd += ["--sample-id", sample_id]
    return cmd


def run_lirical(*, hpo_terms: Iterable[str], vcf_path: str = "", data_dir: str = "",
                workspace: str = "", assembly: str = "hg38", excluded_hpo: Iterable[str] = (),
                sample_id: str = "sample-1", exomiser_hg19: str = "", exomiser_hg38: str = "",
                output_prefix: str = "lirical", base_cmd: tuple[str, ...] = ("lirical",),
                labels: "dict[str, str] | None" = None,
                exec_fn: "Callable[[list[str]], Any] | None" = None) -> dict[str, Any]:
    """Run LIRICAL (the PRIMARY track): write a phenopacket from the patient's HPO terms, invoke
    ``prioritize``, and parse the ranked differential. Injectable (``exec_fn``) so the build/parse are
    testable without a live LIRICAL — the real ``exec_fn`` runs inside ``lirical.sif`` on HPC3
    (:mod:`bioagent.tools.phenotype_cli`), the fallback is ``subprocess.run``.

    GATED: returns ``status='not_installed'`` until both ``data_dir`` (the staged LIRICAL data) and an
    ``exec_fn`` are supplied — i.e. until ``deploy/lirical/build_and_stage.sh`` has run. Scores
    GENOTYPE-AWARE when a ``vcf_path`` + the assembly-matched Exomiser DB are present, else
    PHENOTYPE-ONLY (still a valid per-disease posterior from the symptoms alone)."""
    hpo_terms = [str(h).strip() for h in hpo_terms if str(h).strip()]
    if not hpo_terms:
        return {"status": "error", "error": "LIRICAL needs at least one observed HPO term.",
                "candidates": []}

    # GATE: every incoming ID is checked against the real HPO release before it can define a patient's
    # phenotype. The IDs usually arrive from map_phenotype_to_hpo (already grounded), but nothing stops
    # the model from typing them itself — and a fabricated-but-well-formed ID (HP:0000622 for HP:0000662)
    # would silently score the WRONG phenotype and yield a confident, wrong differential. Unknown/
    # malformed IDs are dropped, obsolete ones forwarded to their replacement, and both are reported.
    phenotype_notes: list[str] = []
    try:
        from .hpo_terms.mapper import validate_hpo_ids

        checked = validate_hpo_ids(hpo_terms)
        checked_excl = validate_hpo_ids([str(h).strip() for h in excluded_hpo if str(h).strip()])
    except Exception:                                   # noqa: BLE001 - a lexicon problem must not block a run
        labels = labels or {}
    else:
        for r in checked["rejected"] + checked_excl["rejected"]:
            phenotype_notes.append(f"dropped {r['hpo_id']}: {r['note']}")
        for r in checked["remapped"] + checked_excl["remapped"]:
            phenotype_notes.append(f"{r['from']} is obsolete → used {r['to']} ({r['name']})")
        if not checked["valid"]:
            return {"status": "error", "candidates": [],
                    "error": ("none of the supplied HPO terms exist in "
                              f"{checked['hpo_version'] or 'the HPO release'}: "
                              + "; ".join(phenotype_notes)
                              + " — use map_phenotype_to_hpo on the clinical text instead of writing IDs."),
                    "phenotype_notes": phenotype_notes}
        hpo_terms = checked["valid"]
        excluded_hpo = checked_excl["valid"]
        # Label every term from the ONTOLOGY (it overrides the caller's wording) so the phenopacket
        # records what the ID actually means, not what the model thought it meant.
        labels = {**(labels or {}), **checked["labels"], **checked_excl["labels"]}
        phenotype_notes += hpo_release_drift(data_dir, checked["hpo_version"])

    if not data_dir or exec_fn is None:
        return {"status": "not_installed",
                "note": "LIRICAL not staged on HPC3 yet — run deploy/lirical/build_and_stage.sh.",
                "phenotype_notes": phenotype_notes, "candidates": []}

    asm = normalize_assembly(assembly)
    exomiser_dir = exomiser_hg19 if asm == "hg19" else exomiser_hg38
    genotype_aware = bool(vcf_path and exomiser_dir)

    ws = Path(workspace) if workspace else Path(".")
    out_dir = ws / "artifacts" / "phenotype"
    work = ws / "work"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"status": "error", "error": f"cannot create workspace dirs: {exc}", "candidates": []}

    # Genotype-aware only: DETECT a chr contig prefix and strip it just for LIRICAL when present (a bare
    # VCF is used as-is). Exomiser matches on bare Ensembl names, so a chr-prefixed WGS VCF would score
    # nothing without this. A strip failure is non-fatal — fall back to the original VCF.
    vcf_for_lirical = vcf_path if genotype_aware else ""
    chr_stripped = False
    if genotype_aware:
        try:
            if vcf_uses_chr_prefix(vcf_path):
                nochr = work / "nochr.vcf"
                strip_chr_prefix(vcf_path, str(nochr))
                vcf_for_lirical = str(nochr)
                chr_stripped = True
        except OSError:
            vcf_for_lirical = vcf_path

    # Write the case's HPO terms as a GA4GH Phenopacket for PROVENANCE (a standard, self-describing record
    # of exactly what defined the case) — LIRICAL v2.4 itself is driven by the -p/-n CLI args below, not
    # this file. Best-effort: a write failure must not abort the analysis.
    excluded_list = [str(h).strip() for h in excluded_hpo if str(h).strip()]
    try:
        pp = build_phenopacket(hpo_terms, sample_id=sample_id, excluded=excluded_list, labels=labels)
        (out_dir / "phenopacket.json").write_text(json.dumps(pp, indent=2), encoding="utf-8")
    except OSError:
        pass

    cmd = build_lirical_cmd(observed=hpo_terms, negated=excluded_list, data_dir=data_dir,
                            output_dir=str(out_dir), prefix=output_prefix, assembly=asm,
                            vcf_path=vcf_for_lirical,
                            exomiser_dir=exomiser_dir if genotype_aware else "",
                            sample_id=sample_id, base_cmd=base_cmd)
    proc = exec_fn(cmd)
    if getattr(proc, "returncode", 1) != 0:
        return {"status": "error", "error": f"LIRICAL failed: {_proc_tail(proc)}",
                "cmd": " ".join(cmd), "candidates": []}

    tsv_path = _find_lirical_tsv(out_dir, output_prefix)
    if tsv_path is None:
        return {"status": "error", "error": "LIRICAL produced no TSV output",
                "cmd": " ".join(cmd), "candidates": []}
    try:
        candidates = parse_lirical_tsv(tsv_path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return {"status": "error", "error": f"cannot read LIRICAL TSV: {exc}", "candidates": []}

    # LIRICAL's genotype-aware TSV names the gene by Entrez id (NCBIGene:24), not a symbol, and the
    # two-track reconcile keys on the SYMBOL — fill it from LIRICAL's staged hgnc_complete_set.txt so the
    # differential carries gene symbols (for the report + the reconcile join). Best-effort: an
    # absent/unreadable map just leaves ``gene`` empty.
    apply_entrez_symbols(candidates, str(Path(data_dir) / "hgnc_complete_set.txt"))

    return {
        "status": "ok", "tool": "run_lirical",
        "mode": "genotype_aware" if genotype_aware else "phenotype_only",
        "assembly": asm, "n_hpo_terms": len(hpo_terms), "n_excluded_hpo": len(excluded_list),
        "hpo_terms": hpo_terms,             # the VALIDATED terms the posterior is actually conditioned on
        "phenotype_notes": phenotype_notes,  # IDs dropped/forwarded by the ontology gate (for Diagnostics)
        "chr_stripped": chr_stripped,   # was the VCF de-chr'd for Exomiser? (genotype-aware only)
        "n_candidates": len(candidates), "tsv_path": str(tsv_path),
        "candidates": [c.as_dict() for c in candidates],
        "note": ("" if genotype_aware else
                 "phenotype-only: no VCF+Exomiser DB, so the posterior weighs symptoms alone — stage an "
                 "Exomiser DB (deploy/lirical/build_and_stage.sh EXOMISER=1) for genotype-aware scoring."),
        "raw_data_to_llm": False,
    }


def _proc_tail(proc: Any, n: int = 1500) -> str:
    return ((getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "")[-n:]).strip()


def _find_lirical_tsv(out_dir: Path, prefix: str) -> "Path | None":
    """Locate LIRICAL's TSV output under ``out_dir``. Prefers the exact ``<prefix>.tsv``; falls back to
    the first ``<prefix>*.tsv`` (LIRICAL versions vary the exact suffix, e.g. ``.tsv`` vs a dated name)."""
    exact = out_dir / f"{prefix}.tsv"
    if exact.exists():
        return exact
    matches = sorted(out_dir.glob(f"{prefix}*.tsv"))
    return matches[0] if matches else None


# --- Entrez → gene-symbol mapping (LIRICAL emits Entrez ids; reconcile keys on symbols) ---------------


def entrez_to_symbol_map(hgnc_path: str) -> dict[str, str]:
    """Parse LIRICAL's staged ``hgnc_complete_set.txt`` into ``{entrez_id: symbol}`` (both from the named
    header columns, so column-order changes are tolerated). Returns ``{}`` if the file is absent/malformed
    — the caller then leaves gene symbols empty rather than failing."""
    p = Path(hgnc_path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            try:
                i_sym, i_entrez = header.index("symbol"), header.index("entrez_id")
            except ValueError:
                return {}
            for line in fh:
                cells = line.rstrip("\n").split("\t")
                if len(cells) > max(i_sym, i_entrez):
                    entrez, sym = cells[i_entrez].strip(), cells[i_sym].strip()
                    if entrez and sym:
                        out[entrez] = sym
    except OSError:
        return {}
    return out


def apply_entrez_symbols(candidates: list[DiseaseCandidate], hgnc_path: str) -> int:
    """Fill each candidate's ``gene`` SYMBOL from its ``entrez_gene_id`` (``NCBIGene:24`` → ``ABCA4``)
    using :func:`entrez_to_symbol_map`. Only fills a blank ``gene``; returns how many were resolved. A
    no-op (returns 0) when no candidate carries an Entrez id or the HGNC map is unavailable."""
    if not any(c.entrez_gene_id for c in candidates if not c.gene):
        return 0
    mapping = entrez_to_symbol_map(hgnc_path)
    if not mapping:
        return 0
    n = 0
    for c in candidates:
        if c.gene or not c.entrez_gene_id:
            continue
        entrez = c.entrez_gene_id.split(":")[-1].strip()   # "NCBIGene:24" -> "24"
        sym = mapping.get(entrez)
        if sym:
            c.gene = sym
            n += 1
    return n


# --- the end-to-end differential: LIRICAL + literature, always both ----------------------------------


def _candidate_from_dict(d: dict[str, Any]) -> DiseaseCandidate:
    """Rebuild a :class:`DiseaseCandidate` from its ``as_dict()`` form (``sources`` comes back as a
    sorted list). Unknown keys are ignored so an older/newer serialised candidate still loads."""
    known = {f for f in DiseaseCandidate.__dataclass_fields__}          # noqa: SLF001 - dataclass API
    kw = {k: v for k, v in d.items() if k in known}
    kw["sources"] = set(kw.get("sources") or ())
    kw["matched_hpo"] = list(kw.get("matched_hpo") or [])
    kw["evidence_pmids"] = list(kw.get("evidence_pmids") or [])
    kw["flags"] = list(kw.get("flags") or [])
    return DiseaseCandidate(**kw)


def plan_literature_queries(lirical_candidates: list[DiseaseCandidate],
                            candidate_genes: Iterable[str] = (), *,
                            max_queries: int = 6) -> list[tuple[str, str]]:
    """Which ``(gene, disease)`` pairs to ask the literature about, highest value first.

    Two populations, and the SECOND is the point of this whole line:
      1. LIRICAL's top candidates — the literature confirms, grades, or contradicts them.
      2. Genes from the variant shortlist that LIRICAL never scored — the gap. A gene can be absent
         from LIRICAL's output because it is genuinely irrelevant, or because nobody has curated it
         into OMIM/HPOA yet, and LIRICAL cannot tell those apart. Only the literature can.

    Capped at ``max_queries``: each query is a full RAG loop, and an uncapped differential over a
    whole variant shortlist would take longer than the analysis it belongs to.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for c in lirical_candidates:
        if c.gene and c.gene not in seen:
            seen.add(c.gene)
            pairs.append((c.gene, c.disease_name))
    for g in candidate_genes:
        g = str(g).strip()
        if g and g not in seen:
            seen.add(g)
            pairs.append((g, ""))          # no curated disease name — ask the gene-centric question
    return pairs[:max_queries]


def diagnose(*, hpo_terms: Iterable[str], literature_runner: "Callable[..., dict] | None" = None,
             lirical_result: "dict[str, Any] | None" = None, candidate_genes: Iterable[str] = (),
             max_literature_queries: int = 6, rescue_threshold: str = "STRONG",
             hpo_labels: "dict[str, str] | None" = None, **lirical_kwargs: Any) -> dict[str, Any]:
    """The full differential: run BOTH tracks, then adjudicate them into one ranked answer.

    This is the entry point that makes the phenotype line able to answer a case LIRICAL alone
    cannot. Three situations it handles that ``run_lirical`` on its own does not:

    * **LIRICAL has the answer** — the literature grades and cites it (``concordant``).
    * **LIRICAL has a WRONG answer** — the retrieved literature disputes/refutes the association and
      outranks it (``conflict``), instead of a stale curation being reported with false confidence.
    * **LIRICAL has NO answer** — it is not staged, it errored, or the gene is not curated. The
      differential is then built from the literature (``literature_only``) rather than coming back
      empty, which is what "cannot diagnose" used to mean here.

    ``literature_runner`` is the ``paperqa2_evidence`` contract runner — in production, the one
    :func:`~bioagent.tools.phenotype_evidence.make_deep_literature_runner` builds over
    ``deep_literature``. Without one the result degrades to LIRICAL-only rather than failing.
    """
    notes: list[str] = []
    if lirical_result is None:
        lirical_result = run_lirical(hpo_terms=hpo_terms, labels=hpo_labels, **lirical_kwargs)

    lirical_ok = lirical_result.get("status") == "ok"
    lirical_candidates = [_candidate_from_dict(d) for d in (lirical_result.get("candidates") or [])]
    if not lirical_ok:
        notes.append(f"LIRICAL unavailable ({lirical_result.get('status')}): "
                     f"{lirical_result.get('error') or lirical_result.get('note') or ''}".strip()
                     + " — no calibrated post-test probability is available for this case.")
    notes.extend(lirical_result.get("phenotype_notes") or [])

    # --- the literature track ---
    literature: list[dict[str, Any]] = []
    pairs = plan_literature_queries(lirical_candidates, candidate_genes,
                                    max_queries=max_literature_queries)
    if literature_runner is None:
        notes.append("no literature runner wired — LIRICAL's ranking is reported unchanged "
                     "(wire deep_literature to grade, cite, and gap-fill it).")
    elif not pairs:
        notes.append("nothing to ask the literature about: LIRICAL produced no gene-labelled "
                     "candidate and no candidate_genes were supplied.")
    else:
        hpo_list = [str(h).strip() for h in hpo_terms if str(h).strip()]
        for gene, disease in pairs:
            literature.append(paperqa2_evidence(gene, disease, hpo_list, runner=literature_runner))

    reconciled = reconcile(lirical_candidates, literature, rescue_threshold=rescue_threshold)
    differential = adjudicate(reconciled)

    if lirical_ok and literature:
        mode = "lirical+literature"
    elif literature:
        mode = "literature_only"
    else:
        mode = "lirical_only"

    conflicts = [d for d in differential if d["agreement"] == "conflict"]
    if conflicts:
        notes.append(f"{len(conflicts)} candidate(s) where the literature contradicts LIRICAL — "
                     "the literature was weighted higher; see decision_note on each.")

    # Status follows the line's graceful-degrade convention: "error" is reserved for something that
    # actually FAILED. A host where neither track is wired has not failed — it is ``not_installed``,
    # the same word ``run_lirical`` uses, so every existing caller already knows to continue without
    # a differential instead of treating a missing deployment as a broken run.
    if differential or lirical_ok or literature:
        status = "ok"
    elif lirical_result.get("status") == "error":
        status = "error"
    else:
        status = "not_installed"

    return {
        "status": status,
        "tool": "diagnose_disease",
        "mode": mode,
        "n_candidates": len(differential),
        "differential": differential,
        "top": differential[0] if differential else None,
        "lirical": {k: v for k, v in lirical_result.items() if k != "candidates"},
        "literature": literature,
        "reconciled": reconciled.as_dict(),      # provenance: the untouched two-track view
        "notes": notes,
        "raw_data_to_llm": False,
    }


# --- the Scientist tool (phenotype → disease differential) -------------------------------------------


def make_phenotype_differential_tool() -> Any:
    """The ``run_lirical`` tool: phenotype-driven differential diagnosis. Defaults to the in-process path
    (which reports ``not_installed`` — LIRICAL runs only on HPC3); the gateway swaps in the HPC3
    ``lirical.sif`` line via the tool router when ``phenotype_on_hpc`` is set. Mirrors
    :func:`bioagent.tools.variant_annotation.make_variant_annotation_tool`."""
    from ..agents.research_harness import HarnessTool

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # In-process (no LIRICAL on the eyeserver): data_dir is empty → run_lirical returns not_installed.
        # The gateway's SlurmAnalysisExecutor injects data_dir/exomiser/assembly + the VCF and runs it in
        # lirical.sif on HPC3, where this SAME function executes with a real exec_fn (phenotype_cli).
        return run_lirical(
            hpo_terms=args.get("hpo_terms") or [],
            excluded_hpo=args.get("excluded_hpo") or (),
            vcf_path=str(args.get("vcf_path", "")),
            workspace=str(getattr(ctx, "workspace", "") or ""),
            data_dir=str(args.get("data_dir", "")),
            assembly=str(args.get("assembly", "hg38") or "hg38"),
            sample_id=str(args.get("sample_id", "sample-1") or "sample-1"),
            exomiser_hg19=str(args.get("exomiser_hg19", "")),
            exomiser_hg38=str(args.get("exomiser_hg38", "")),
        )

    return HarnessTool(
        "run_lirical",
        "Phenotype-driven DIFFERENTIAL DIAGNOSIS: rank candidate DISEASES by a calibrated post-test "
        "probability, given the patient's phenotype (as HPO terms) and — when a VCF is loaded — the "
        "variant findings. Runs LIRICAL (HPO/OMIM likelihood-ratio model) on HPC3, DOWNSTREAM of "
        "annotate_variants. Use it to answer 'how confident is each candidate disease' (e.g. 'RP 70% / "
        "LCA 20%') when overlapping symptoms can't pin the diagnosis alone. Get `hpo_terms` and "
        "`excluded_hpo` from map_phenotype_to_hpo — run it on the patient's clinical description FIRST "
        "and pass its output through. Do NOT write HPO IDs from memory: IDs one digit apart are "
        "different real phenotypes, and every ID here is checked against the HPO release (unknown ones "
        "are dropped). If a VCF + the Exomiser database are "
        "available it scores GENOTYPE-AWARE (variants sharpen the ranking); otherwise PHENOTYPE-ONLY (a "
        "valid posterior from symptoms alone). Returns a ranked list of diseases (name, OMIM/ORPHA id, "
        "gene, post-test probability, composite likelihood ratio). Reports ONLY LIRICAL's output; never "
        "invents a disease or probability. Returns status 'not_installed' if LIRICAL is not staged — then "
        "continue without the differential.",
        {"type": "object", "properties": {
            "hpo_terms": {"type": "array", "items": {"type": "string"},
                          "description": "observed HPO term IDs for the patient's phenotype (required), "
                                         "e.g. ['HP:0000510','HP:0000662']"},
            "excluded_hpo": {"type": "array", "items": {"type": "string"},
                             "description": "HPO term IDs the patient explicitly does NOT have (optional)"},
            "sample_id": {"type": "string",
                          "description": "the proband's sample id in the VCF (optional)"},
            "vcf_path": {"type": "string",
                         "description": "VCF for genotype-aware scoring (defaults to the run's dataset)"}}},
        _exec,
        reads_private_data=True, category="annotation",
    )


def make_diagnose_disease_tool(literature_fn: "Callable[[dict, Any], dict] | None" = None,
                               lirical_fn: "Callable[[dict, Any], dict] | None" = None) -> Any:
    """The ``diagnose_disease`` tool: the FULL differential — LIRICAL *and* the literature, adjudicated.

    Where ``run_lirical`` reports one track and stops (returning nothing usable when LIRICAL is not
    staged or the gene is not curated), this tool always consults both and returns a single ranked
    answer in which the literature outranks LIRICAL on disagreement.

    It COMPOSES the two existing tools rather than re-implementing either, so each keeps its own
    execution route: ``lirical_fn`` is the ``run_lirical`` executor (HPC3 ``lirical.sif`` once the
    gateway routes it) and ``literature_fn`` the ``deep_literature`` executor (HPC3 ``paperqa.sif``,
    because the PubMedBERT index lives on /dfs3b where the eyeserver cannot read it). The registry
    binds both AFTER routing, so this tool automatically follows wherever they run. Missing either
    one is not an error — the differential degrades to the track it has and says so in ``notes``.
    """
    from ..agents.research_harness import HarnessTool

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        runner = None
        if literature_fn is not None:
            from .phenotype_evidence import make_deep_literature_runner
            runner = make_deep_literature_runner(literature_fn, ctx)
        lirical_args = {k: args[k] for k in
                        ("hpo_terms", "excluded_hpo", "sample_id", "vcf_path", "data_dir",
                         "assembly", "exomiser_hg19", "exomiser_hg38") if k in args}
        return diagnose(
            hpo_terms=args.get("hpo_terms") or [],
            excluded_hpo=args.get("excluded_hpo") or (),
            candidate_genes=args.get("candidate_genes") or (),
            literature_runner=runner,
            # Delegate the LIRICAL track to the ROUTED run_lirical when the gateway wired one, so the
            # phenotype line runs in its own container exactly as it does on its own.
            lirical_result=lirical_fn(lirical_args, ctx) if lirical_fn is not None else None,
            max_literature_queries=int(args.get("max_literature_queries", 6) or 6),
            vcf_path=str(args.get("vcf_path", "")),
            workspace=str(getattr(ctx, "workspace", "") or ""),
            data_dir=str(args.get("data_dir", "")),
            assembly=str(args.get("assembly", "hg38") or "hg38"),
            sample_id=str(args.get("sample_id", "sample-1") or "sample-1"),
            exomiser_hg19=str(args.get("exomiser_hg19", "")),
            exomiser_hg38=str(args.get("exomiser_hg38", "")),
        )

    return HarnessTool(
        "diagnose_disease",
        "FULL differential diagnosis: combines LIRICAL's calibrated post-test probability with the "
        "published literature (deep_literature / PaperQA2) and returns ONE ranked list. Prefer this "
        "over run_lirical whenever you want the actual diagnosis rather than LIRICAL's raw output. "
        "It is the only path that still answers when LIRICAL cannot: if LIRICAL is not staged, "
        "errors, or the gene is not curated into OMIM/HPOA, the differential is built from the "
        "literature instead of coming back empty. When the two tracks DISAGREE the literature is "
        "weighted higher (a cited, retrieved refutation outranks a curated call, because the "
        "curation lags the literature) and the candidate is flagged `agreement='conflict'`. Get "
        "`hpo_terms` from map_phenotype_to_hpo — never write HPO IDs from memory. Pass "
        "`candidate_genes` from the variant shortlist (annotate_variants): genes LIRICAL did not "
        "score are exactly where the literature track earns its keep. Each returned candidate "
        "carries `final_score`, `agreement` (concordant | conflict | literature_only | lirical_only "
        "| unsupported), `decision_note`, LIRICAL's untouched `posttest_prob`, and the ClinGen "
        "`evidence_tier` + PMIDs. `final_score` is a RANKING score, NOT a probability — quote "
        "`posttest_prob` when you need a calibrated number, and cite only the PMIDs returned.",
        {"type": "object", "properties": {
            "hpo_terms": {"type": "array", "items": {"type": "string"},
                          "description": "observed HPO term IDs for the patient's phenotype (required), "
                                         "e.g. ['HP:0000510','HP:0000662']"},
            "excluded_hpo": {"type": "array", "items": {"type": "string"},
                             "description": "HPO term IDs the patient explicitly does NOT have (optional)"},
            "candidate_genes": {"type": "array", "items": {"type": "string"},
                                "description": "gene symbols from the variant shortlist to ask the "
                                               "literature about even if LIRICAL never scored them "
                                               "(this is the gap-fill path), e.g. ['CRB1','ABCA4']"},
            "max_literature_queries": {"type": "integer",
                                       "description": "cap on literature queries, default 6 (each is a "
                                                      "full RAG loop)"},
            "sample_id": {"type": "string",
                          "description": "the proband's sample id in the VCF (optional)"},
            "vcf_path": {"type": "string",
                         "description": "VCF for genotype-aware scoring (defaults to the run's dataset)"}},
         "required": ["hpo_terms"]},
        _exec,
        reads_private_data=True, category="annotation",
    )
