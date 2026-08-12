"""Run-scope context management — the long-horizon half of the token budget.

There are two scales at which this system can run out of context, and only one of them was handled.

*Within a step*, ``ResearchHarness._trim_history`` already measures the running message history
against the served window and drops or compresses old turns. That is code-level and it works.

*Across a run* nothing measured anything. The Scientist's brief is rebuilt for every step and
carries ``_accepted_findings_block`` — every accepted finding so far, with its evidence pointers.
That block grows linearly with the run, so a 20-step campaign feeds a bigger and bigger prefix into
every step until the per-step trimmer starts throwing away the actual work to make room. The
long-horizon system was quietly paying for its own history.

THE RULE THIS MODULE ENFORCES
-----------------------------
**The decision to compact is made by code that measured the payload; the model only writes prose.**
Asking a model "are you running out of room?" is not context management — it is a guess by a party
with no access to the tokenizer, made inside the very prompt that is overflowing. So:

- :func:`estimate_tokens` measures, in the same char-per-token unit the harness budgets with;
- :func:`assess` turns a measurement into a deterministic :class:`Pressure` verdict;
- :func:`should_compact` decides, from budget/step/milestone/manual signals — no LLM;
- :func:`fold_rounds` picks WHICH rounds fold, again deterministically (oldest first, keep the most
  recent verbatim, never fold what a hypothesis still points at);
- the model is handed the folded text and asked for one digest. Its output is prose only.

EVIDENCE POINTERS SURVIVE COMPACTION, ALWAYS
--------------------------------------------
Downstream steps are told to VERIFY an upstream claim by reading the artifact that backs it. If
compaction let the model drop an artifact path, it would silently convert grounded findings into
unverifiable assertions — the exact failure the data-grounding work exists to prevent. So the
pointers are re-attached by :func:`compact_block` from the original rounds, mechanically, whatever
the model wrote. The digest can lose detail; it cannot lose provenance.

Pure functions over plain data. The LLM is injected. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: Same unit ``research_harness`` budgets with, so the two scales agree about what a token costs.
#: Deliberately an overcount — running out of window is far worse than compacting slightly early.
CHARS_PER_TOKEN = 2.6

#: Fractions of the carry budget at which the run is considered under pressure / critical.
TIGHT_AT = 0.60
CRITICAL_AT = 0.85

#: Fraction of ``max_steps`` at which "you are running out of plan" becomes worth surfacing.
STEPS_MILESTONE_AT = 0.75

COMPACT_SYSTEM = (
    "You are the Principal Investigator writing a HANDOVER SUMMARY of the earlier part of a study "
    "that is still running. The steps below are finished and accepted; their full text is about to "
    "be replaced by what you write, and every later step will read YOUR summary instead of them.\n"
    "Preserve, in this order of priority: (1) every quantitative result — counts, thresholds, "
    "assembly, gene and cell-type names, statistics — verbatim, because later steps and the final "
    "report are grounded in them and a number you round or drop is simply lost; (2) what was "
    "concluded, and what was explicitly NOT concluded; (3) any decision or parameter choice a later "
    "step must stay consistent with. DROP: narration, restated method boilerplate, and anything that "
    "merely says a step ran.\n"
    "Do NOT add interpretation, do NOT speculate beyond what the steps state, and do NOT write "
    "artifact paths — those are re-attached mechanically and anything you invent would be wrong.\n"
    "Write compact prose, one short paragraph per finished step, no preamble and no closing remarks."
)


def estimate_tokens(text: str) -> int:
    """Approximate token count for a payload. Deliberately the same crude, safe estimate the
    harness uses; exactness is not the point — a stable, conservative unit is."""
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


@dataclass(frozen=True)
class Pressure:
    """A measured verdict on how much of the carry budget the run's history is using."""

    tokens: int
    budget: int
    state: str          # "ok" | "tight" | "critical"

    @property
    def ratio(self) -> float:
        return (self.tokens / self.budget) if self.budget > 0 else 0.0

    @property
    def needs_attention(self) -> bool:
        return self.state in ("tight", "critical")


def carry_budget(window: int, *, reserve: int = 0, share: float = 0.35) -> int:
    """How many tokens the run's CARRIED history may occupy.

    Only a share of the window, because the carried findings are a prefix: the step's own brief,
    the tool schemas, the growing tool results and the reply all have to fit alongside it. Giving
    the history most of the window is how you get a run that can remember everything and do nothing.
    """
    usable = max(0, window - max(0, reserve))
    return max(1, int(usable * share))


def assess(text: str, window: int, *, reserve: int = 0, share: float = 0.35) -> Pressure:
    """Measure a carried payload against its budget. Pure arithmetic — no model involved."""
    budget = carry_budget(window, reserve=reserve, share=share)
    tokens = estimate_tokens(text)
    ratio = tokens / budget if budget > 0 else 0.0
    state = "critical" if ratio >= CRITICAL_AT else "tight" if ratio >= TIGHT_AT else "ok"
    return Pressure(tokens=tokens, budget=budget, state=state)


@dataclass(frozen=True)
class Trigger:
    """Why the run should pause or compact. ``kind`` is stable enough to branch on in the UI."""

    kind: str           # "context" | "steps" | "milestone" | "manual"
    reason: str
    urgent: bool = False   # critical context pressure: compacting is no longer optional


def should_compact(pressure: Pressure, *, steps_done: int = 0, max_steps: int = 0,
                   milestone: str = "", manual: bool = False) -> "Trigger | None":
    """The deterministic trigger. Returns ``None`` when there is nothing to do.

    Order matters: a manual request always wins (the human asked), then genuine context pressure,
    then the softer "you are nearly out of plan" and milestone signals, which are worth surfacing
    to a human but are not emergencies.
    """
    if manual:
        return Trigger("manual", "you asked for a compaction", urgent=False)
    if pressure.state == "critical":
        return Trigger("context", f"carried history is at {pressure.ratio:.0%} of its budget",
                       urgent=True)
    if pressure.state == "tight":
        return Trigger("context", f"carried history is at {pressure.ratio:.0%} of its budget")
    if max_steps > 0 and steps_done >= int(max_steps * STEPS_MILESTONE_AT):
        return Trigger("steps", f"{steps_done} of {max_steps} planned steps used")
    if milestone:
        return Trigger("milestone", milestone)
    return None


def fold_rounds(n_rounds: int, *, keep_recent: int = 3, pinned: "set[int] | None" = None) -> list[int]:
    """Indices of the rounds to FOLD into a digest — oldest first, keeping the most recent verbatim.

    ``pinned`` indices are never folded: those are the rounds an OPEN hypothesis still points at,
    and a hypothesis whose supporting evidence has been summarised away cannot be adjudicated
    honestly later. Returns ``[]`` when there is nothing worth folding.
    """
    pinned = pinned or set()
    foldable = [i for i in range(max(0, n_rounds - max(0, keep_recent))) if i not in pinned]
    return foldable if len(foldable) >= 2 else []   # folding a single round buys nothing


def compact_block(digest: str, evidence: "list[str]", n_folded: int) -> str:
    """Assemble the replacement block: the model's prose plus every evidence pointer re-attached.

    The pointers come from the ORIGINAL rounds, not from the digest, so provenance survives
    regardless of what the model wrote or forgot."""
    head = (f"Summary of the first {n_folded} accepted step(s) of this run (their full text has "
            "been compacted to stay inside the context window; the numbers below are verbatim):")
    out = f"{head}\n{(digest or '').strip() or '(no digest available)'}"
    if evidence:
        out += ("\n  evidence from the compacted steps (verify against these; read-only):"
                + "".join(f"\n    - {p}" for p in evidence))
    return out


def make_digest(folded_text: str, complete_fn: "Callable[[list[dict[str, Any]]], str]") -> str:
    """One bounded LLM call: prose only. Returns ``''`` on any failure, and the caller then keeps
    the uncompacted text — degrading to "we did not save room" is always safer than degrading to
    "we lost the findings"."""
    try:
        return (complete_fn([
            {"role": "system", "content": COMPACT_SYSTEM},
            {"role": "user", "content": folded_text},
        ]) or "").strip()
    except Exception:  # noqa: BLE001 - compaction is best-effort and must never break a run
        return ""
