"""The LITERATURE evidence track for phenotype→disease diagnosis, backed by ``deep_literature``.

This is the runner the contract in ``docs/paperqa2_evidence_layer_contract.md`` left open: given a
**gene + candidate disease + the patient's HPO terms**, return a structured, GROUNDED evidence record
— ``{association, clingen_tier, disease, evidence:[{pmid, quote, study_type}]}`` — that slots into
``phenotype_dx.paperqa2_evidence(..., runner=…)``.

Why it exists: LIRICAL scores only what is CURATED into HPO/HPOA/OMIM. Anything newer or rarer than
the curation — a gene reported last year, a phenotype expansion, an ultra-rare association — is
invisible to it, and LIRICAL's silence there is not evidence of absence, it is absence of data. This
module is what fills that hole, and it is also what lets the literature CONTRADICT a curated call
(``DISPUTED``/``REFUTED``), which is the only way a stale curation can be corrected at run time.

The trust model — the whole point of this file — is that the tier is never the model's word alone:

  1. **Retrieval decides existence.** No retrieved passage ⇒ ``association=False``, ``tier=NONE``.
     The prose is not consulted; a model that "knows" the association without a passage is ignored.
  2. **The passages CAP the grade.** :func:`evidence_ceiling` computes the strongest tier the
     retrieved evidence can support (how many INDEPENDENT sources, and do any carry experimental
     support). The model may grade lower than the ceiling — never higher. So "DEFINITIVE" asserted
     off one case report is recorded as LIMITED.
  3. **Every claim carries its passage.** Each evidence item is a real retrieved chunk with its
     source id, so a reviewer can check the grade against the text that produced it.

Everything here is pure except :func:`make_deep_literature_runner`, which is where the injected
``deep_literature`` executor is actually called — so the grading is unit-tested without a corpus,
a GPU, or a network.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from .phenotype_dx import CLINGEN_TIERS, tier_at_least

# --- source identity ---------------------------------------------------------------------------
# A PaperQA context names its source in a free-text citation ("Smith et al. ... PMID: 28492367" /
# "https://doi.org/10.1..."). We key INDEPENDENCE on that id: two chunks from one paper are ONE
# source, and the ceiling below counts sources, not chunks — otherwise a single heavily-chunked
# paper would look like a replicated body of evidence.
_PMID_RE = re.compile(r"(?:pmid[:\s#]*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{6,9})", re.I)
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,;\)\]]+)", re.I)


def extract_pmid(text: str) -> str:
    """The first PMID in a citation string (``PMID: 28492367`` or a pubmed URL), else ``""``."""
    m = _PMID_RE.search(text or "")
    return m.group(1) if m else ""


def extract_doi(text: str) -> str:
    """The first DOI in a citation string, else ``""``. Many corpus PDFs carry a DOI but no PMID;
    keeping it means such a passage still has a checkable identity instead of being dropped."""
    m = _DOI_RE.search(text or "")
    return m.group(1).rstrip(".") if m else ""


def source_key(citation: str) -> str:
    """A stable identity for one SOURCE (paper), used to count independent support. Prefers the
    PMID, then the DOI, then a normalised prefix of the citation text (enough to separate papers
    without splitting one paper across its chunk-to-chunk citation whitespace differences)."""
    text = (citation or "").strip()
    pmid = extract_pmid(text)
    if pmid:
        return f"pmid:{pmid}"
    doi = extract_doi(text)
    if doi:
        return f"doi:{doi.lower()}"
    return "cite:" + re.sub(r"\s+", " ", text.lower())[:80]


# --- study type --------------------------------------------------------------------------------
# ClinGen weights evidence by WHAT KIND of study produced it, so the ceiling needs the study type of
# each passage. Ordered most- to least-specific: the first pattern that matches wins, so a passage
# describing a knockout mouse in a cohort paper is scored on the experimental work (the thing that
# actually raises a ClinGen tier), not on the cohort framing.
_STUDY_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("functional", re.compile(
        r"\b(knock[- ]?out|knock[- ]?in|knockdown|morphant|morpholino|zebrafish|mouse model|murine|"
        r"in vitro|in vivo|minigene|luciferase|western blot|immunostain\w*|organoid|iPSC|"
        r"rescue experiment|functional (?:assay|stud\w+|characteri[sz]ation)|electroretinogra\w+ in mice)\b",
        re.I)),
    ("cohort", re.compile(
        r"\b(cohort|case[- ]control|consecutive patients|unrelated (?:families|probands|patients)|"
        r"\d+\s+(?:families|probands|patients|individuals)|screen\w+ of \d+|registry)\b", re.I)),
    ("review", re.compile(r"\b(review|meta[- ]analys\w+|systematic search|literature survey)\b", re.I)),
    ("case_report", re.compile(
        r"\b(case report|we (?:report|describe|present) a|a (?:single )?patient|the proband|"
        r"one family)\b", re.I)),
)
STUDY_TYPES = ("case_report", "cohort", "functional", "review")


def classify_study_type(text: str) -> str:
    """Label one retrieved passage with the ClinGen-relevant study type it describes. Defaults to
    ``case_report`` — the WEAKEST class — when nothing matches, so an unclassifiable passage can
    never inflate the ceiling."""
    for label, pattern in _STUDY_TYPE_PATTERNS:
        if pattern.search(text or ""):
            return label
    return "case_report"


# --- what the retrieved passages can support ----------------------------------------------------


def evidence_ceiling(evidence: "list[dict[str, Any]]") -> str:
    """The STRONGEST ClinGen tier the retrieved passages could justify — the cap the model's own
    grade is clamped to in :func:`grade_evidence`.

    The ladder follows the ClinGen validity framework's two axes, replication and experimental
    support, counting INDEPENDENT sources (:func:`source_key`) rather than passages:

    ==================  ==========================================================
    independent sources tier
    ==================  ==========================================================
    0                   ``NONE``    — nothing retrieved; there is nothing to grade
    1                   ``LIMITED`` (``MODERATE`` if that source is a cohort or functional study)
    2                   ``MODERATE`` (``STRONG`` with experimental support)
    3+                  ``STRONG``  (``DEFINITIVE`` with experimental support AND 4+ sources)
    ==================  ==========================================================

    ``DEFINITIVE`` is deliberately hard to reach: in ClinGen it means replication across independent
    groups over years plus functional data, and a corpus slice of a handful of papers is not that.
    """
    if not evidence:
        return "NONE"
    sources = {e.get("source_id") or source_key(str(e.get("citation", ""))) for e in evidence}
    n = len(sources)
    types = {str(e.get("study_type", "")) for e in evidence}
    functional = "functional" in types
    if n == 1:
        return "MODERATE" if (functional or "cohort" in types) else "LIMITED"
    if n == 2:
        return "STRONG" if functional else "MODERATE"
    return "DEFINITIVE" if (functional and n >= 4) else "STRONG"


# --- reading the model's own verdict off the grounded answer -------------------------------------
# The answer is PROSE, so these patterns are read as a VOTE that is then clamped by the ceiling —
# never as the grade itself. Contradiction is matched before support: a passage that refutes an
# association is the one case where the literature must be able to overrule a curated LIRICAL call,
# so it has to survive an answer that also mentions the association positively.
_REFUTED_RE = re.compile(
    r"\b(refut\w+|disprov\w+|excluded as a cause|not a (?:disease|causative) gene|"
    r"no longer considered|withdrawn)\b", re.I)
_DISPUTED_RE = re.compile(
    r"\b(disput\w+|contradict\w+|conflicting (?:evidence|reports)|questioned|"
    r"failed to replicate|could not be replicated|challeng\w+ the association)\b", re.I)
_NEGATION_RE = re.compile(
    r"\b(no (?:evidence|support|studies|papers|reports|association|link)|not (?:associated|linked|"
    r"supported|established|reported)|does not (?:support|establish|show)|"
    r"insufficient evidence|nothing in the (?:provided )?(?:context|corpus)|"
    r"i cannot answer|unable to answer|the (?:provided )?context does not)\b", re.I)
_TIER_MENTION_RE = re.compile(r"\b(DEFINITIVE|STRONG|MODERATE|LIMITED|DISPUTED|REFUTED)\b")


def read_stated_tier(answer: str) -> str:
    """The tier the grounded answer itself names (the model's vote), or ``""`` if it named none.
    Takes the LAST mention: an answer that walks the rubric before concluding ends on its verdict."""
    hits = _TIER_MENTION_RE.findall((answer or "").upper())
    return hits[-1] if hits else ""


def _weaker(a: str, b: str) -> str:
    """The weaker of two positive tiers (used to clamp a stated grade to what the passages support)."""
    return a if not tier_at_least(a, b) else b


def grade_evidence(answer: str, contexts: "Iterable[dict[str, Any]]", *, gene: str, disease: str,
                   max_evidence: int = 8) -> dict[str, Any]:
    """Turn a ``deep_literature`` response into the contract's evidence record, grounded by rule.

    ``contexts`` are PaperQA's retrieved chunks (``{citation, summary, score}``). The three trust
    rules from the module docstring are enforced here, in order: retrieval decides existence, the
    passages cap the grade, and every graded claim keeps the passage it came from.
    """
    evidence: list[dict[str, Any]] = []
    for c in contexts or []:
        quote = str(c.get("summary") or c.get("quote") or "").strip()
        citation = str(c.get("citation") or "").strip()
        if not quote and not citation:
            continue
        evidence.append({
            "pmid": extract_pmid(citation) or extract_pmid(quote),
            "doi": extract_doi(citation),
            "citation": citation,
            "source_id": source_key(citation or quote),
            "quote": quote[:600],
            "study_type": classify_study_type(f"{quote} {citation}"),
        })
    # Keep the best-scored passages, one pass, stable: a run must not depend on how many chunks the
    # retriever happened to return.
    evidence = evidence[:max_evidence]

    text = answer or ""
    notes: list[str] = []

    # RULE 1 — retrieval decides existence. Nothing retrieved ⇒ nothing to grade, whatever the prose says.
    # ``evidence_status`` is what the adjudicator keys on, and the distinction it draws matters
    # clinically: UNGRADED (the corpus said nothing) must not count against a candidate, while
    # UNSUPPORTED (relevant papers came back and none support the link) is a real, if weak, signal.
    if not evidence:
        return {"association": False, "clingen_tier": "NONE", "disease": disease, "gene": gene,
                "evidence": [], "evidence_status": "ungraded",
                "notes": ["no passage retrieved — ungraded (absence of data, not "
                          "evidence of absence)"], "stated_tier": read_stated_tier(text)}

    stated = read_stated_tier(text)
    ceiling = evidence_ceiling(evidence)

    # CONTRADICTION — checked first, and NOT capped by the positive ceiling: refuting evidence is
    # graded on its own axis. This is the branch that lets the literature overrule a curated call.
    if _REFUTED_RE.search(text) or stated == "REFUTED":
        notes.append("the retrieved literature REFUTES this gene–disease association")
        return {"association": False, "clingen_tier": "REFUTED", "disease": disease, "gene": gene,
                "evidence": evidence, "evidence_status": "contradicted", "notes": notes,
                "stated_tier": stated}
    if _DISPUTED_RE.search(text) or stated == "DISPUTED":
        notes.append("the retrieved literature DISPUTES this gene–disease association")
        return {"association": False, "clingen_tier": "DISPUTED", "disease": disease, "gene": gene,
                "evidence": evidence, "evidence_status": "contradicted", "notes": notes,
                "stated_tier": stated}

    # A plain "no evidence in the corpus" answer: passages came back but none support the link.
    if _NEGATION_RE.search(text):
        notes.append("the answer reports no support for this association in the corpus")
        return {"association": False, "clingen_tier": "NONE", "disease": disease, "gene": gene,
                "evidence": evidence, "evidence_status": "unsupported", "notes": notes,
                "stated_tier": stated}

    # RULE 2 — the passages CAP the grade. The model may grade lower, never higher.
    tier = ceiling
    if stated and stated in CLINGEN_TIERS:
        tier = _weaker(stated, ceiling)
        if tier != stated:
            notes.append(f"answer claimed {stated}; capped to {tier} — "
                         f"{len({e['source_id'] for e in evidence})} independent source(s) retrieved")
    return {"association": True, "clingen_tier": tier, "disease": disease, "gene": gene,
            "evidence": evidence, "evidence_status": "graded", "notes": notes,
            "stated_tier": stated}


# --- the question we actually ask ----------------------------------------------------------------


def build_evidence_question(gene: str, disease: str, hpo_terms: "Iterable[str]" = (),
                            labels: "dict[str, str] | None" = None) -> str:
    """The retrieval question for ONE gene–disease pair, built from the SPECIFIC inputs (contract
    rule 3): a generic "what disease is this" retrieves generic passages. The patient's phenotype is
    named because a gene–disease link that does not match THIS phenotype is not support for THIS
    case, and the rubric is spelled out so the answer states a gradeable tier."""
    labels = labels or {}
    pheno = ", ".join(f"{t} ({labels[t]})" if labels.get(t) else str(t)
                      for t in (str(x).strip() for x in hpo_terms) if t)
    target = f"{gene}–{disease}" if disease else f"{gene}"
    q = (f"Is there published evidence that variants in {gene} cause {disease or 'disease'}? "
         f"Assess the {target} gene–disease association.")
    if pheno:
        q += f" The patient's phenotype is: {pheno}."
    q += (" Answer using ONLY the retrieved passages. State how many independent studies/families "
          "support the association and whether there is functional/experimental support. Then give "
          "the ClinGen gene-disease validity classification as a single word from: DEFINITIVE, "
          "STRONG, MODERATE, LIMITED, DISPUTED, REFUTED. If the passages do not support the "
          "association, say so explicitly.")
    return q


def make_deep_literature_runner(literature_fn: "Callable[[dict[str, Any], Any], dict[str, Any]]",
                                ctx: Any = None, *, max_evidence: int = 8,
                                hpo_labels: "dict[str, str] | None" = None
                                ) -> "Callable[..., dict[str, Any]]":
    """Adapt the ``deep_literature`` tool into the ``paperqa2_evidence(runner=…)`` contract.

    ``literature_fn`` is the tool executor — ``(args, ctx) -> dict`` — so the caller passes whatever
    ``deep_literature`` it has: the in-process :func:`~bioagent.tools.paperqa_search.run_paperqa`,
    or the HPC3-routed executor the registry builds (the index lives on /dfs3b, which the gateway
    cannot read, so in production it is the routed one).

    ``hpo_labels`` (``{HP:0000510: 'Rod-cone dystrophy'}``) is bound HERE rather than passed per call,
    because the returned runner has to match the contract's ``(gene, disease, hpo_terms)`` signature
    exactly. Names retrieve far better than bare IDs, so bind them when the caller has them.

    A literature failure is never fatal: any non-``ok`` status comes back as an ungraded record
    (``association=False``, ``tier=NONE``) carrying the reason, so the differential degrades to
    LIRICAL-only instead of erroring.
    """
    def _runner(*, gene: str, disease: str, hpo_terms: "list[str] | None" = None,
                labels: "dict[str, str] | None" = None) -> dict[str, Any]:
        question = build_evidence_question(gene, disease, hpo_terms or (), labels or hpo_labels)
        try:
            resp = literature_fn({"question": question}, ctx) or {}
        except Exception as exc:                      # noqa: BLE001 - literature must not kill a run
            return {"association": False, "clingen_tier": "NONE", "disease": disease, "gene": gene,
                    "evidence": [], "evidence_status": "ungraded",
                    "notes": [f"deep_literature raised {type(exc).__name__}: {exc}"]}
        if resp.get("status") != "ok":
            return {"association": False, "clingen_tier": "NONE", "disease": disease, "gene": gene,
                    "evidence": [], "evidence_status": "ungraded",
                    "notes": [f"deep_literature unavailable ({resp.get('status', 'unknown')}): "
                              f"{resp.get('error') or resp.get('note') or ''}".strip()]}
        graded = grade_evidence(resp.get("answer") or resp.get("formatted_answer") or "",
                                resp.get("contexts") or [], gene=gene, disease=disease,
                                max_evidence=max_evidence)
        graded["question"] = question
        return graded

    return _runner
