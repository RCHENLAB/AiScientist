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

plus the evidence accumulated for/against it and a ``status`` that a later step can close out
(``supported`` / ``refuted`` / ``inconclusive``). A hypothesis that is proposed and never resolved
stays ``open`` and is reported as such — an honest loose end beats a quiet drop.

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


def _norm(text: str) -> str:
    """Bag-of-words normal form used for duplicate detection. Two statements that differ only in
    punctuation, casing, or filler words collapse to the same key, so the model re-proposing the
    same idea after every step does not fill the ledger with near-identical rows."""
    return " ".join(_WORD.findall((text or "").lower()))


@dataclass(frozen=True)
class Hypothesis:
    """One falsifiable claim the run generated (never one the user asked for — those are the plan)."""

    id: str
    statement: str
    prediction: str = ""
    test: str = ""
    origin_step: str = ""          # the step whose result provoked it
    status: str = "open"           # open | supported | refuted | inconclusive
    evidence: tuple[str, ...] = ()  # one line per adjudication, newest last
    tested_by: tuple[str, ...] = ()  # step texts added to the plan to test this

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "statement": self.statement, "status": self.status}
        for key in ("prediction", "test", "origin_step"):
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
    def add(self, statement: str, *, prediction: str = "", test: str = "",
            origin_step: str = "") -> Hypothesis | None:
        """Record a new hypothesis. Returns ``None`` when the statement is empty or duplicates one
        already in the ledger (by :func:`_norm`) — the caller treats ``None`` as "nothing new"."""
        statement = (statement or "").strip()
        if not statement or self.find(statement) is not None:
            return None
        h = Hypothesis(id=f"h{len(self.items) + 1}", statement=statement,
                       prediction=(prediction or "").strip(), test=(test or "").strip(),
                       origin_step=(origin_step or "").strip())
        self.items.append(h)
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
            if h.test:
                line += f"\n    test: {h.test}"
            if h.origin_step:
                line += f"\n    arose from: {h.origin_step}"
            for ev in h.evidence:
                line += f"\n    evidence: {ev}"
            lines.append(line)
        head = ("Hypotheses this run GENERATED (not planned up front) — statement, what it predicts, "
                "and how it was or would be tested:")
        return head + "\n" + "\n".join(lines)
