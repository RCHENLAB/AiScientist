"""Claim audit: check the report's candidate conclusions BEFORE it is written, one short question at a time.

MEASURED (experiments/plan_vs_exec_ab, 3645cf3): asked on their own, both Qwen3.6 and Qwen3.8 answer
"is a same-direction ribosomal up-shift across every cell type, with a 1.6x depth gap, a headline
finding?" (no, a depth artefact) and "why are rod genes 'down' in amacrine cells?" (ambient RNA /
misassignment) correctly, 2/2. The same models then headlined the up-shift and invented a "paracrine
cascade" in their 20-page reports. The knowledge is there; it is not APPLIED while writing, where the
caveat lands in Limitations and the conclusion stays unchanged.

So the checks are pulled out of writing. The model first lists the candidate headline claims. Each
claim then goes through the checks that apply to this dataset, each as a separate short call that
carries the design facts (depth ratio, samples per arm) from the dataset profile rather than trusting
the model to recall them. The verdicts become a binding block for both writers: a ROBUST claim may be
headlined, a REWORD claim is kept but stated descriptively, and a LIKELY-TECHNICAL claim may only
appear as a caveat. Every verdict is kept with its reason for the technical report.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

AskFn = Callable[[list[dict[str, Any]]], str]

MAX_CLAIMS = 8

# Facts the models got WRONG or did not apply when asked (3645cf3). Stated, not left to be inferred.
STAT_FACTS = (
    "- Per-cell total-count normalisation followed by log1p does NOT remove a sequencing-depth difference "
    "between libraries: deeper cells detect more genes and have fewer zeros, and rank tests (Wilcoxon) "
    "pick that up. A global, same-direction shift of abundant, housekeeping or ribosomal genes across "
    "cell types, under a depth gap, is most likely technical.\n"
    "- Genes that mark a DIFFERENT, abundant cell type (e.g. rod phototransduction genes Rho/Gnat1/Pde6g "
    "in amacrine or bipolar cells) usually reflect ambient RNA or misassigned cells, not regulation in "
    "the cell type where they appear.\n"
    "- With one library per condition, cells are pseudoreplicates: p-values, FDR/q-values and "
    "enrichment significance computed across cells are not evidence, and nothing may be called "
    "'significant'. Do not report counts of terms or genes passing an FDR/p cutoff as findings "
    "(e.g. '34 pathways at FDR<0.05'); describe pathways by direction and effect size (NES) instead.\n"
    "- A two-arm comparison shows association, not causation: do not write that the condition "
    "causes, drives, triggers or induces a change."
)


@dataclass
class Check:
    name: str
    question: str
    # what failing this check means for the claim
    fail_status: str


@dataclass
class AuditedClaim:
    claim: str
    step: Any = None
    genes: list[str] = field(default_factory=list)
    cell_types: list[str] = field(default_factory=list)
    checks: dict[str, dict[str, str]] = field(default_factory=dict)
    status: str = "unclear"


# ---- design facts (deterministic, from the dataset profile) -------------------------------------

_REPLICATE_COL = re.compile(r"sample|donor|replicate|mouse|animal|individual|patient|orig\.ident|library|batch|lane", re.I)


def design_facts(dataset_result: dict[str, Any] | None) -> dict[str, Any]:
    """What the checks may assume about the design, read from the profile, never from the model."""
    dr = dataset_result or {}
    dba = dr.get("design_by_arm") or {}
    arms = dba.get("cells_by_arm") or {}
    facts: dict[str, Any] = {"arms": dict(arms), "label_column": dba.get("label_column"),
                             "condition_column": dba.get("condition_column"),
                             "cells_by_label_and_arm": dict(dba.get("cells_by_label_and_arm") or {})}
    med = (dba.get("qc_median_by_arm") or {}).get("nCount_RNA") or {}
    vals = [float(v) for v in med.values() if isinstance(v, (int, float)) and v]
    facts["depth_ratio"] = round(max(vals) / min(vals), 2) if len(vals) >= 2 else None
    facts["depth_by_arm"] = dict(med)
    # Replicates: a column other than the condition itself that splits the data into more groups than
    # there are arms could hold biological replicates. None such (typical: one library per arm) means 1.
    cats = dr.get("obs_categoricals") or {}
    n_arms = max(len(arms), 2)
    replicated = any(
        k != facts["condition_column"] and _REPLICATE_COL.search(k) and int((v or {}).get("n") or 0) > n_arms
        for k, v in cats.items())
    facts["one_sample_per_arm"] = bool(arms) and not replicated
    facts["single_cell"] = bool(dba.get("label_column")) or str(dr.get("dataset_kind", "")).lower() in {
        "single_cell", "scrna", "snrna", "h5ad"}
    facts["snrna_hint"] = dba.get("snrna_hint") or ""
    return facts


def applicable_checks(facts: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    ratio = facts.get("depth_ratio")
    if ratio and ratio >= 1.25:
        checks.append(Check(
            "global_shift",
            f"Is this claim about a change that goes the SAME direction across most or all of the cell "
            f"types it covers (a global shift), rather than a change specific to one or two cell types? "
            f"The arms differ {ratio:.1f}x in median counts per cell ({facts.get('depth_by_arm')}). Answer "
            f"\"fail\" if it is a global shift that this depth gap alone could produce; \"pass\" if it is "
            f"cell-type-specific, or if depth could not explain it.",
            "likely_technical"))
    if facts.get("single_cell"):
        checks.append(Check(
            "cell_type_plausibility",
            "Does this claim describe genes changing in a cell type where those genes are NOT normally "
            "expressed, e.g. markers of a different, abundant cell type? Answer \"fail\" if so; \"pass\" if "
            "the genes are plausibly expressed in the stated cell type, or if the claim names no genes.",
            "likely_technical"))
    if facts.get("one_sample_per_arm"):
        checks.append(Check(
            "inference",
            "Does this claim rest on, or state, p-values, FDR/q-values, 'significant' results, or "
            "enrichment significance computed by treating cells as independent samples? This design has "
            "ONE library per condition. Answer \"fail\" if it does; \"pass\" if it is stated as a "
            "descriptive effect size or direction.",
            "reword"))
    checks.append(Check(
        "causal",
        "Does this claim assert causation (the condition causes / drives / triggers / induces the change) "
        "rather than an association seen in a two-arm comparison? Answer \"fail\" if it asserts causation; "
        "\"pass\" otherwise.",
        "reword"))
    return checks


# ---- the model calls ----------------------------------------------------------------------------

_EXTRACT_SYSTEM = (
    "You list the CANDIDATE HEADLINE FINDINGS a research report could draw from the accepted analysis "
    "results below: one per item, each a short declarative sentence stated the way a report would state "
    "it, naming the genes, cell types or pathways involved. Include the findings a writer would be "
    "TEMPTED to headline even if you suspect they are artefacts; each will be checked separately. At most "
    f"{MAX_CLAIMS}. Reply with ONLY JSON: "
    '{"claims": [{"claim": "...", "step": <step number or null>, "genes": ["..."], "cell_types": ["..."]}]}'
)

_CHECK_SYSTEM = (
    "You check ONE candidate finding from a single-cell / omics analysis against ONE question, using the "
    "design facts and statistical facts given. Be strict and literal. Reply with ONLY JSON: "
    '{"verdict": "pass" or "fail", "reason": "<one sentence>"}'
)


def _json_obj(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        val = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return val if isinstance(val, dict) else None


def _facts_text(facts: dict[str, Any]) -> str:
    lines = [f"- Arms (cells): {facts.get('arms')}"]
    if facts.get("depth_ratio"):
        lines.append(f"- Median counts per cell by arm: {facts.get('depth_by_arm')} ({facts['depth_ratio']:.1f}x)")
    lines.append("- Biological replicates: " + ("NONE, one library per condition" if facts.get("one_sample_per_arm")
                                                 else "possibly present (a replicate-like column exists)"))
    if facts.get("snrna_hint"):
        lines.append(f"- Protocol: {facts['snrna_hint']}")
    return "\n".join(lines)


def extract_claims(ask: AskFn, summary: str, question: str) -> list[AuditedClaim]:
    raw = ask([{"role": "system", "content": _EXTRACT_SYSTEM},
               {"role": "user", "content": f"Research question:\n{question}\n\nAccepted step results:\n{summary}"}])
    obj = _json_obj(raw) or {}
    out: list[AuditedClaim] = []
    for c in (obj.get("claims") or [])[:MAX_CLAIMS]:
        if isinstance(c, dict) and str(c.get("claim") or "").strip():
            out.append(AuditedClaim(claim=str(c["claim"]).strip(), step=c.get("step"),
                                    genes=[str(g) for g in (c.get("genes") or [])][:12],
                                    cell_types=[str(t) for t in (c.get("cell_types") or [])][:12]))
    return out


def _run_check(ask: AskFn, claim: AuditedClaim, check: Check, facts: dict[str, Any], evidence: str) -> dict[str, str]:
    user = (f"Design facts (from the dataset itself):\n{_facts_text(facts)}\n\nStatistical facts:\n{STAT_FACTS}\n\n"
            f"Candidate finding:\n{claim.claim}\n"
            + (f"Genes: {', '.join(claim.genes)}\n" if claim.genes else "")
            + (f"Cell types: {', '.join(claim.cell_types)}\n" if claim.cell_types else "")
            + (f"\nEvidence excerpt:\n{evidence}\n" if evidence else "")
            + f"\nQuestion: {check.question}")
    obj = _json_obj(ask([{"role": "system", "content": _CHECK_SYSTEM}, {"role": "user", "content": user}])) or {}
    verdict = str(obj.get("verdict") or "").strip().lower()
    return {"verdict": verdict if verdict in {"pass", "fail"} else "unclear",
            "reason": str(obj.get("reason") or "").strip()[:400]}


def _status(claim: AuditedClaim, checks: list[Check]) -> str:
    by = {c.name: c for c in checks}
    fails = [n for n, r in claim.checks.items() if r.get("verdict") == "fail"]
    if any(by[n].fail_status == "likely_technical" for n in fails if n in by):
        return "likely_technical"
    if fails:
        return "reword"
    if any(r.get("verdict") == "unclear" for r in claim.checks.values()):
        return "unclear"
    return "robust"


def _evidence_for(claim: AuditedClaim, summary: str) -> str:
    """The accepted-step line the claim came from (or the first lines mentioning its genes)."""
    lines = [ln for ln in summary.splitlines() if ln.strip()]
    if claim.step is not None:
        for ln in lines:
            if re.match(rf"-\s*Step {re.escape(str(claim.step))}\b", ln):
                return ln[:2500]
    hits = [ln for ln in lines if any(g and g in ln for g in claim.genes)]
    return "\n".join(hits)[:2500]


def enabled() -> bool:
    return (os.environ.get("BIOAGENT_CLAIM_AUDIT", "1").strip().lower() not in {"0", "false", "no", "off"})


def audit(ask: AskFn, summary: str, question: str, dataset_result: dict[str, Any] | None,
          *, max_workers: int = 6,
          tested_counts: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    """Run the whole audit. Returns a plain dict (JSON-safe) so it can ride on LabResult / run_state.
    ``tested_counts`` are the cells per group and arm AFTER QC, as the run's tools counted them."""
    facts = design_facts(dataset_result)
    if tested_counts:
        facts["cells_tested_by_label_and_arm"] = {str(g): dict(v) for g, v in tested_counts.items()}
    checks = applicable_checks(facts)
    claims = extract_claims(ask, summary, question)
    jobs = [(c, k) for c in claims for k in checks]
    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as ex:
            results = list(ex.map(lambda ck: _run_check(ask, ck[0], ck[1], facts, _evidence_for(ck[0], summary)), jobs))
        for (c, k), r in zip(jobs, results):
            c.checks[k.name] = r
    for c in claims:
        c.status = _status(c, checks)
    return {"facts": facts, "checks": [asdict(k) for k in checks], "claims": [asdict(c) for c in claims]}


_HEAD = {
    "robust": "PASSED THE CHECKS: may lead the report (title, abstract, conclusion), still as descriptive "
              "observations; never call them robust, significant or confirmed",
    "reword": "REWORD: keep, but state it descriptively (effect size and direction; no 'significant', "
              "no p/FDR as evidence, no causal verbs)",
    "likely_technical": "LIKELY TECHNICAL: do NOT present as a finding and do NOT put it in the title, "
                        "abstract or conclusion as biology; mention it only as a caveat ('may reflect ...')",
    "unclear": "UNCLEAR: present cautiously, as a hypothesis",
}


def render_block(result: dict[str, Any] | None) -> str:
    """The binding block handed to the writers. Empty when there is nothing to say."""
    claims = (result or {}).get("claims") or []
    if not claims:
        return ""
    out = ["CLAIM AUDIT (binding; each candidate finding was checked separately BEFORE writing):"]
    facts = (result or {}).get("facts") or {}
    auth = _authoritative_numbers(facts)
    if auth:
        out.append(auth)
    for status in ("robust", "reword", "likely_technical", "unclear"):
        group = [c for c in claims if c.get("status") == status]
        if not group:
            continue
        out.append(f"{_HEAD[status]}:")
        for c in group:
            why = "; ".join(f"{n}: {r.get('reason')}" for n, r in (c.get("checks") or {}).items()
                            if r.get("verdict") == "fail" and r.get("reason"))
            out.append(f"  - {c.get('claim')}" + (f"  [{why}]" if why else ""))
    out.append("Lead the report with the findings that PASSED THE CHECKS. Statistical facts that apply:\n"
               + STAT_FACTS)
    return "\n".join(out)


def _authoritative_numbers(facts: dict[str, Any]) -> str:
    """The design numbers from the dataset itself, stated once so no writer recomputes or copies them.

    Two measured failures this closes: a report quoting a 1.38-fold depth gap it derived from genes
    per cell instead of the 1.61-fold counts gap, and reports copying a step write-up whose per-arm
    cell counts contradicted the dataset (run 8847: AC "469 WT / 181 DDX41" for a true 258 / 392)."""
    lines = []
    if facts.get("depth_ratio"):
        lines.append(f"  - Sequencing depth: median counts (nCount) per cell {facts.get('depth_by_arm')} = "
                     f"{facts['depth_ratio']:.2f}-fold. Quote THIS ratio for the depth gap.")
    def _rows(table: dict) -> str:
        return "; ".join(f"{lab} {', '.join(f'{a} {n}' for a, n in counts.items())}"
                         for lab, counts in table.items() if isinstance(counts, dict))

    # Two tables, never one. The profile counts the UPLOADED FILE; QC then removes cells. Stated as
    # the only authority ("a step count that differs is wrong: use these"), the file's counts
    # overrode the tested ones: run f3731e0b7136's manuscript said Endothelial "7 DDX41 / 27 WT" were
    # below the floor, where run_de had tested 5 / 22.
    by = facts.get("cells_by_label_and_arm") or {}
    tested = facts.get("cells_tested_by_label_and_arm") or {}
    if by:
        lines.append(f"  - Cells per class and arm IN THE UPLOADED FILE, before QC (the dataset "
                     f"profile): {_rows(by)}. Use these only to describe the data as uploaded.")
    if tested:
        lines.append(f"  - Cells per class and arm AFTER QC, as the analysis tested or refused them "
                     f"(the tools' own counts): {_rows(tested)}. Every statement about how many cells "
                     "a class had in the analysis, including which classes fell below a cell floor, "
                     "MUST use these. A per-arm count that matches neither table is wrong.")
    elif by:
        lines.append("  - QC only removes cells: a per-arm count ABOVE the uploaded-file count is wrong, "
                     "and a smaller one may be the post-QC count a step tested.")
    return ("AUTHORITATIVE DESIGN NUMBERS (from the dataset and the tools, not from any step's prose):\n"
            + "\n".join(lines)) if lines else ""
