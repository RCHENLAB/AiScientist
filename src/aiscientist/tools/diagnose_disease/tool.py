"""The ``diagnose_disease`` tool. Documentation: ``TOOL.md`` in this folder.

Split out of ``tools/phenotype_dx.py`` by ``scripts/refactor/split_tools.py``: the code is the old
module's text, verbatim, with only the imports rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable
from ..run_lirical.tool import DiseaseCandidate, run_lirical


# ClinGen gene-disease validity tiers — the currency of the LITERATURE track, strongest first. Disputed/
# Refuted are contradictory evidence: they rank below the positive tiers so they never pass a positive
# threshold (they can only argue AGAINST a call, never rescue one).
CLINGEN_TIERS = ("DEFINITIVE", "STRONG", "MODERATE", "LIMITED", "DISPUTED", "REFUTED", "NONE")


_TIER_RANK = {t: i for i, t in enumerate(CLINGEN_TIERS)}


def tier_at_least(tier: str, threshold: str) -> bool:
    """True if ``tier`` is at least as strong as ``threshold`` (DEFINITIVE strongest). Unknown → NONE."""
    return _TIER_RANK.get((tier or "NONE").upper(), _TIER_RANK["NONE"]) <= \
        _TIER_RANK.get((threshold or "NONE").upper(), _TIER_RANK["NONE"])


def paperqa2_evidence(gene: str, disease: str, hpo_terms: Iterable[str] = (), *,
                      runner: "Callable[..., dict] | None" = None) -> dict[str, Any]:
    """The literature EVIDENCE track (PaperQA2). Placeholder until the corpus/QA is wired: returns
    ``status='not_enabled'`` unless a ``runner`` implementing the contract is injected. Contract: given
    (gene, candidate disease, HPO terms) it returns
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
    :func:`~aiscientist.tools.phenotype_evidence.make_deep_literature_runner` builds over
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
    from ..sdk import HarnessTool

    def _exec(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        runner = None
        if literature_fn is not None:
            from .evidence import make_deep_literature_runner
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
