"""The hypothesis ledger — the state that turns a plan-executor into a research loop.

WHY THIS EXISTS
---------------
Before this module the lab could only EXECUTE a plan drafted once, before any result existed
(``_pi_plan``), and afterwards the plan could only SHRINK (``_preflight_gate`` skip /
``_poststep_review`` prune). A surprising result at step 3 had nowhere to go: there was no object
to record "this is unexpected, here is what would explain it, here is the test that would tell",
and no mechanism to add the step that runs that test. So the system could never open a research
path it did not set out on.

The ledger is that missing state. A hypothesis is only worth carrying if it is FALSIFIABLE, so the
record forces the three parts that make it so:

    statement   — what we think is going on,
    prediction  — what we should observe if it is TRUE,
    test        — the analysis whose outcome DISTINGUISHES it from the obvious alternative,

plus the RIVAL explanation it is competing with, the DISCRIMINATOR that separates the two, the
evidence accumulated for/against it, and a ``status`` that a later step can close out
(``supported`` / ``refuted`` / ``inconclusive``). A hypothesis that is proposed and never resolved
stays ``open`` and is reported as such — an honest loose end beats a quiet drop.

WHY A RIVAL IS MANDATORY
------------------------
The first production run with this module on (``c135ae589d96``, Ddx41 retina) generated exactly one
hypothesis — "the shifts are driven by a 1.6x depth imbalance" — and marked it ``supported``. It had
no rival, so every result merely consistent with it read as support, and "supported" degraded into
"not contradicted". Its prediction ("pseudobulk will collapse the log2FCs") was moreover guaranteed
by the design: with one library per arm, pseudobulk returns nothing significant whether or not the
biology is real. The run concluded the opposite of the published result for that model, which its
own tables carried. So ``admit()`` refuses a hypothesis that names no alternative, draws no
contrast, or rests on a test whose outcome this dataset's design already fixes. The model proposes;
this module decides what is admissible, and says why when it refuses.

Pure data + string helpers: no LLM, no I/O, no clock. The LLM proposes and adjudicates
(``ResearchLab._explore_after_step``); this module only keeps the books, deduplicates, and renders
the ledger for a prompt or a report. That split is what makes the whole loop offline-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

_WORD = re.compile(r"[a-z0-9]+")

#: A hypothesis is resolved once its status is one of these — only ``open`` ones are still worth
#: spending a step on, and only ``open`` ones are offered back to the model for adjudication.
RESOLVED = ("supported", "refuted", "inconclusive")
STATUSES = ("open", *RESOLVED)

#: Adjudication is a verdict between the hypothesis and its rival, not a status handed down. The
#: mapping is the whole point: "supported" is only reachable by the evidence pushing AWAY from the
#: rival, so it can no longer be spent on a result that is merely consistent with the claim.
_FAVOURED_TO_STATUS = {"hypothesis": "supported", "rival": "refuted", "neither": "inconclusive"}


def _norm(text: str) -> str:
    """Bag-of-words normal form used for duplicate detection. Two statements that differ only in
    punctuation, casing, or filler words collapse to the same key, so the model re-proposing the
    same idea after every step does not fill the ledger with near-identical rows."""
    return " ".join(_WORD.findall((text or "").lower()))


#: Tests whose unit of replication is the SAMPLE. Naming one of these is enough on its own: without
#: replicates they cannot produce a meaningful answer, so both rival explanations predict the same
#: "nothing significant" and the discriminator decides nothing.
_REPLICATE_TESTS = ("pseudobulk", "deseq", "edger", "limma", "t test", "anova", "mixed model")

#: Generic statistics vocabulary. On its OWN this indicts nothing — an ORA or GSEA FDR is computed
#: on a single ranking and is a perfectly good discriminator under n=1. (Measured: treating these as
#: disqualifying on their own refused 2 of 7 discriminators a competent scientist would write for
#: this very study, including "GSEA showing adhesion terms at FDR < 0.05 favours the biology".) They
#: only indict when applied BETWEEN THE ARMS, which is the comparison that needs replicates.
_SIGNIFICANCE_WORDS = ("significan", "p value", "pvalue", "padj", "adjusted p", "fdr", "q value")

#: Phrases that put a statistic between the two arms rather than inside one ranking.
_BETWEEN_ARM_CUES = ("between the arms", "between arms", "between the two arms", "between groups",
                     "between conditions", "between the conditions", "between samples",
                     "between the samples", "across the arms", "arm level", "sample level")


def _needs_replicates(discriminator: str) -> bool:
    """True when the discriminator's outcome is fixed by the replication structure rather than by
    which explanation is true. Punctuation is normalised away so "t-test"/"t test" and
    "between-arm"/"between arm" read the same."""
    d = " " + re.sub(r"[^a-z0-9]+", " ", (discriminator or "").lower()).strip() + " "
    if any(f" {t} " in d or d.startswith(f"{t} ") or f" {t}" in d for t in _REPLICATE_TESTS):
        return True
    if any(w in d for w in _SIGNIFICANCE_WORDS):
        return any(c in d for c in _BETWEEN_ARM_CUES)
    return False


@dataclass(frozen=True)
class DesignFacts:
    """The few facts about the STUDY DESIGN that decide whether a proposed test can fail.

    ``replicates_per_arm`` is the number of independent biological units (donor / animal / library)
    per arm — NOT cells. ``None`` means unknown, and an unknown design never blocks a hypothesis:
    this gate exists to catch a specific provable mistake, not to referee falsifiability in general.
    """

    replicates_per_arm: "int | None" = None

    @property
    def can_test_between_arms(self) -> bool:
        """False when the design cannot produce a between-arm p-value that means anything."""
        return self.replicates_per_arm is None or self.replicates_per_arm >= 2


@dataclass(frozen=True)
class Hypothesis:
    """One falsifiable claim the run generated (never one the user asked for — those are the plan)."""

    id: str
    statement: str
    prediction: str = ""
    test: str = ""
    rival: str = ""                # the other explanation that would produce the same observation
    discriminator: str = ""        # which outcome favours THIS one and which favours the rival
    origin_step: str = ""          # the step whose result provoked it
    status: str = "open"           # open | supported | refuted | inconclusive
    evidence: tuple[str, ...] = ()  # one line per adjudication, newest last
    tested_by: tuple[str, ...] = ()  # step texts added to the plan to test this

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "statement": self.statement, "status": self.status}
        for key in ("prediction", "test", "rival", "discriminator", "origin_step"):
            if getattr(self, key):
                d[key] = getattr(self, key)
        if self.evidence:
            d["evidence"] = list(self.evidence)
        if self.tested_by:
            d["tested_by"] = list(self.tested_by)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Hypothesis":
        status = str(d.get("status", "open")).strip().lower()
        return cls(
            id=str(d.get("id", "")),
            statement=str(d.get("statement", "")),
            prediction=str(d.get("prediction", "")),
            test=str(d.get("test", "")),
            rival=str(d.get("rival", "")),
            discriminator=str(d.get("discriminator", "")),
            origin_step=str(d.get("origin_step", "")),
            status=status if status in STATUSES else "open",
            evidence=tuple(str(x) for x in (d.get("evidence") or [])),
            tested_by=tuple(str(x) for x in (d.get("tested_by") or [])),
        )


@dataclass
class HypothesisLedger:
    """Append-mostly store of the run's hypotheses. Ids are deterministic (``h1``, ``h2``, …) so a
    replayed run produces identical events and an offline test can assert on them."""

    items: list[Hypothesis] = field(default_factory=list)

    # -- writes ---------------------------------------------------------------
    def admit(self, statement: str, *, prediction: str = "", test: str = "", rival: str = "",
              discriminator: str = "", origin_step: str = "",
              design: "DesignFacts | None" = None) -> "tuple[Hypothesis | None, str]":
        """Gate then record. Returns ``(hypothesis, "")`` on success, ``(None, reason)`` on refusal.

        Every refusal names itself so the caller can show it. The model proposes; this decides what
        is admissible. See the module docstring for the run that made each rule necessary."""
        statement = (statement or "").strip()
        rival = (rival or "").strip()
        discriminator = (discriminator or "").strip()
        prediction = (prediction or "").strip()

        if not statement:
            return None, "the statement is empty"
        if self.find(statement) is not None:
            return None, "the ledger already holds this hypothesis"
        if not rival:
            return None, ("no rival explanation — an account with no alternative cannot be tested, "
                          "only narrated. Name what else would produce this observation.")
        if _norm(rival) == _norm(statement):
            return None, ("the rival restates the hypothesis, so nothing is being contrasted. "
                          "Name a DIFFERENT explanation of the same observation.")
        if not discriminator:
            return None, ("no discriminator — say which outcome would favour this explanation and "
                          "which would favour the rival.")
        if prediction and _norm(discriminator) == _norm(prediction):
            return None, ("the discriminator just restates the prediction, so it cannot separate "
                          "the two explanations. Say what the RIVAL predicts instead.")
        if design is not None and not design.can_test_between_arms \
                and _needs_replicates(discriminator):
            return None, (
                f"the discriminator rests on a between-arm significance test, but this design has "
                f"{design.replicates_per_arm} replicate(s) per arm — that test returns 'nothing "
                f"significant' whether or not the hypothesis is true, so its outcome is fixed by "
                f"the design and cannot separate the explanations. Discriminate on effect "
                f"direction, cell-type specificity, or an orthogonal measurement instead.")

        h = Hypothesis(id=f"h{len(self.items) + 1}", statement=statement, prediction=prediction,
                       test=(test or "").strip(), rival=rival, discriminator=discriminator,
                       origin_step=(origin_step or "").strip())
        self.items.append(h)
        return h, ""

    def add(self, statement: str, *, prediction: str = "", test: str = "", rival: str = "",
            discriminator: str = "", origin_step: str = "",
            design: "DesignFacts | None" = None) -> Hypothesis | None:
        """:meth:`admit` without the reason — ``None`` means "nothing new", as before."""
        h, _ = self.admit(statement, prediction=prediction, test=test, rival=rival,
                          discriminator=discriminator, origin_step=origin_step, design=design)
        return h

    def find(self, statement_or_id: str) -> Hypothesis | None:
        """Look a hypothesis up by id (``h2``) or by statement text (normalized). ``None`` if absent."""
        key = (statement_or_id or "").strip()
        if not key:
            return None
        for h in self.items:
            if h.id == key:
                return h
        nkey = _norm(key)
        if not nkey:
            return None
        for h in self.items:
            if _norm(h.statement) == nkey:
                return h
        return None

    def link_test(self, statement_or_id: str, step: str) -> bool:
        """Attach a plan step that was added to TEST this hypothesis. False if unknown/duplicate."""
        h = self.find(statement_or_id)
        step = (step or "").strip()
        if h is None or not step or step in h.tested_by:
            return False
        self._replace(h, tested_by=(*h.tested_by, step))
        return True

    def resolve(self, statement_or_id: str, status: str, evidence: str = "") -> Hypothesis | None:
        """Close out (or re-open) a hypothesis with an evidence line. Unknown id / unknown status is
        a no-op returning ``None`` — a garbled model reply must never corrupt the ledger."""
        h = self.find(statement_or_id)
        status = (status or "").strip().lower()
        if h is None or status not in STATUSES:
            return None
        evidence = (evidence or "").strip()
        ev = (*h.evidence, evidence) if evidence and evidence not in h.evidence else h.evidence
        return self._replace(h, status=status, evidence=ev)

    def adjudicate(self, statement_or_id: str, favoured: str,
                   evidence: str = "") -> Hypothesis | None:
        """Close out a hypothesis by saying which of the two explanations the evidence favoured.

        ``favoured`` is ``hypothesis`` / ``rival`` / ``neither``, and maps to supported / refuted /
        inconclusive. This is the entry point the run should use: :meth:`resolve` takes a status
        directly, which lets "supported" be asserted rather than won, and that is how a run marked
        its only hypothesis supported on a prediction its design guaranteed."""
        status = _FAVOURED_TO_STATUS.get((favoured or "").strip().lower())
        if status is None:
            return None
        return self.resolve(statement_or_id, status, evidence)

    def _replace(self, h: Hypothesis, **changes: Any) -> Hypothesis:
        updated = replace(h, **changes)
        self.items[self.items.index(h)] = updated
        return updated

    # -- reads ----------------------------------------------------------------
    def open_items(self) -> list[Hypothesis]:
        return [h for h in self.items if h.status == "open"]

    def resolved_items(self) -> list[Hypothesis]:
        return [h for h in self.items if h.status in RESOLVED]

    def to_list(self) -> list[dict[str, Any]]:
        return [h.to_dict() for h in self.items]

    @classmethod
    def from_list(cls, rows: "list[dict[str, Any]] | None") -> "HypothesisLedger":
        return cls([Hypothesis.from_dict(r) for r in (rows or []) if isinstance(r, dict)])

    def __len__(self) -> int:
        return len(self.items)

    def render(self, *, max_items: int = 12) -> str:
        """The ledger as a compact prompt/report block. ``''`` when empty, so callers can drop the
        section entirely rather than printing an empty heading."""
        if not self.items:
            return ""
        lines: list[str] = []
        for h in self.items[:max_items]:
            line = f"- [{h.id}] ({h.status}) {h.statement}"
            if h.prediction:
                line += f"\n    predicts: {h.prediction}"
            if h.rival:
                line += f"\n    rival: {h.rival}"
            if h.discriminator:
                line += f"\n    tells them apart: {h.discriminator}"
            if h.test:
                line += f"\n    test: {h.test}"
            if h.origin_step:
                line += f"\n    arose from: {h.origin_step}"
            for ev in h.evidence:
                line += f"\n    evidence: {ev}"
            lines.append(line)
        head = ("Hypotheses this run GENERATED (not planned up front). Each is a CONTEST between "
                "an explanation and a named rival; adjudicating one means saying which side the "
                "evidence favoured, not whether the statement sounds consistent with it:")
        return head + "\n" + "\n".join(lines)
