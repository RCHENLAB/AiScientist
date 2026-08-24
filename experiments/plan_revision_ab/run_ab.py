"""Can Qwen3.6-35B-A3B make a SMALL edit to a plan without disturbing the rest of it?

The question this answers is narrow and decision-relevant: in plan mode the user does not text-edit
the agenda — they type natural-language feedback and the PI RE-DRAFTS THE WHOLE PLAN
(``ResearchLab._pi_plan(feedback=..., prior_agenda=...)``). The only thing protecting the steps the
user did not mention is one sentence of prompt ("keep what they did not object to"). Qwen3.6-35B-A3B
activates ~3B parameters, and "change one line, copy the rest verbatim" is exactly the ability that
degrades first at low active-parameter counts. So: measure it, do not guess.

TWO ARMS, ONE MODEL. The comparison is deliberately NOT "small model vs big model" — swapping the
model is expensive (a 194 GB model needs a whole GPU node) and would be the wrong thing to buy if
the fault is the scaffolding rather than the weights:

  A  redraft   the CURRENT production path — the real ``_pi_plan``, whole agenda regenerated.
  B  patch     same model, same plan, but the model returns ONLY {"step": n, "new_text": "..."}
               and the edit is applied to the agenda by CODE. Untouched steps cannot change,
               because nothing regenerates them.

If B ≫ A, the plan-editing weakness is the scaffolding and a bigger model buys nothing here. If A
is already near-perfect, the current path is fine and this whole line of worry is closed.

METRICS (per trial, over R repetitions because the model is stochastic)
  target_hit   the step the user asked about actually changed
  intent_ok    the changed step contains what was asked for (keyword check)
  collateral   how many OTHER steps changed text — the number that matters
  count_delta  steps added/removed
  clean        target_hit AND intent_ok AND collateral == 0 AND count_delta == 0

USAGE
  python experiments/plan_revision_ab/run_ab.py --port 42777 --reps 5
  (--port is a local port forwarded to a vLLM /v1 endpoint; see the module docstring in
   gateway/vllm_client.py — this uses that exact client, so the calls match production.)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bioagent.agents.research_harness import HarnessContext          # noqa: E402
from bioagent.agents.research_lab import LabConfig, ResearchLab      # noqa: E402
from bioagent.gateway import vllm_client                             # noqa: E402

MODEL = "QuantTrio/Qwen3.6-35B-A3B-AWQ"

# A realistic dataset profile, so the planning prompt is the one production actually builds.
DATASET_RESULT = {
    "n_obs": 24817,
    "n_vars": 21453,
    "obs_keys": ["sampleid", "majorclass", "percent.mt", "n_genes", "donor"],
    "obs_categoricals": {
        "sampleid": {"DDX41": 12044, "WT": 12773},
        "majorclass": {"MG": 5120, "Rod": 9033, "Cone": 2210, "BC": 4401, "AC": 2515,
                       "RGC": 1538},
        "donor": {"d1": 6300, "d2": 6100, "d3": 6200, "d4": 6217},
    },
}


@dataclass
class Case:
    """One revision request: a plan the user is looking at, and what they typed."""

    name: str
    question: str
    agenda: list[str]
    feedback: str
    target: int                      # 0-based index of the step the user is talking about
    must_contain: tuple[str, ...]    # lowercase substrings that show the request was honoured
    accept_any: bool = False         # True: any ONE of must_contain suffices


CASES: list[Case] = [
    Case(
        name="resolution",
        question="Compare DDX41 vs WT within each major cell class and interpret the changed genes.",
        agenda=[
            "QC the cells: per-cell metrics, filter, normalize and log1p, report counts before/after",
            "Cluster the cells with Leiden at resolution 0.5 and produce a UMAP",
            "Assign cell-type labels to each cluster from canonical retinal markers",
            "Run DDX41 vs WT differential expression within each major cell class",
            "Run pathway enrichment on the changed genes per cell class",
            "Summarise which changes are shared across cell classes and which are class-specific",
        ],
        feedback="Use resolution 1.0 for the clustering instead of 0.5.",
        target=1,
        must_contain=("1.0",),
    ),
    Case(
        name="reference_group",
        question="Compare DDX41 vs WT within each major cell class and interpret the changed genes.",
        agenda=[
            "QC the cells: per-cell metrics, filter, normalize and log1p, report counts before/after",
            "Reuse the existing majorclass labels; do not re-cluster",
            "Run differential expression between DDX41 and WT within each major cell class",
            "Run pathway enrichment on the up- and down-regulated genes per cell class",
            "Search the literature for the pathways that came out enriched",
            "Summarise shared versus cell-class-specific changes",
        ],
        feedback="Make WT the explicit reference group in the DE step so the fold changes point the right way.",
        target=2,
        must_contain=("reference", "wt as", "against wt", "vs wt", "relative to wt"),
        accept_any=True,
    ),
    Case(
        name="add_threshold",
        question="Compare DDX41 vs WT within each major cell class and interpret the changed genes.",
        agenda=[
            "QC the cells: filter, normalize and log1p, report counts",
            "Reuse the existing majorclass labels; do not re-cluster",
            "Run DDX41 vs WT differential expression within each major cell class",
            "Run pathway enrichment on the changed genes per cell class",
            "Search the literature for the enriched pathways",
            "Summarise shared versus cell-class-specific changes",
        ],
        feedback="In the QC step, drop cells with more than 10 percent mitochondrial reads.",
        target=0,
        must_contain=("10",),
    ),
    Case(
        name="skip_clustering",
        question="Compare DDX41 vs WT within each major cell class and interpret the changed genes.",
        agenda=[
            "QC the cells: filter, normalize and log1p, report counts",
            "Cluster the cells de novo with Leiden and produce a UMAP",
            "Run DDX41 vs WT differential expression within each cluster",
            "Run pathway enrichment on the changed genes per cluster",
            "Search the literature for the enriched pathways",
            "Summarise shared versus cluster-specific changes",
        ],
        feedback="Step 2 should reuse the existing majorclass labels rather than clustering de novo.",
        target=1,
        must_contain=("majorclass", "existing label", "reuse"),
        accept_any=True,
    ),
]


# --- arm B: return a PATCH, apply it in code ---------------------------------

_PATCH_SYSTEM = (
    "You are revising ONE step of an analysis plan. You are given the numbered plan and the "
    "user's requested change. Identify the SINGLE step the user is talking about and rewrite ONLY "
    "that step. Do NOT rewrite, reorder, add, or remove any other step. "
    'Reply with ONLY a JSON object: {"step": <1-based step number>, "new_text": "<the rewritten '
    'step>"}. If the request genuinely cannot be expressed as a change to one step, reply '
    '{"step": 0, "new_text": ""}.'
)


def _extract_json(raw: str) -> dict | None:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            return None
    return obj if isinstance(obj, dict) else None


def patch_revise(complete_fn, agenda: list[str], feedback: str) -> list[str]:
    """Arm B. The model chooses ONE step and rewrites it; code applies the edit.

    The untouched steps are literally the same objects — no regeneration, so collateral damage is
    structurally impossible rather than merely discouraged by a prompt."""
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(agenda))
    raw = complete_fn([
        {"role": "system", "content": _PATCH_SYSTEM},
        {"role": "user", "content": f"Plan:\n{numbered}\n\nRequested change:\n{feedback}"},
    ])
    obj = _extract_json(raw) or {}
    try:
        n = int(obj.get("step", 0))
    except (TypeError, ValueError):
        return list(agenda)
    text = str(obj.get("new_text") or "").strip()
    if not (1 <= n <= len(agenda)) or not text:
        return list(agenda)                       # unusable reply -> plan unchanged, never worse
    return [*agenda[:n - 1], text, *agenda[n:]]


# --- scoring ------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


@dataclass
class Trial:
    target_hit: bool
    intent_ok: bool
    collateral: int
    count_delta: int
    after: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.target_hit and self.intent_ok and self.collateral == 0 and self.count_delta == 0


def score(case: Case, after: list[str]) -> Trial:
    before = case.agenda
    count_delta = len(after) - len(before)
    # Align by position; a plan that changed length is already a failure of "small edit", and
    # positional comparison is what the user sees in the UI.
    target_hit = (case.target < len(after)
                  and _norm(after[case.target]) != _norm(before[case.target]))
    hay = _norm(after[case.target]) if case.target < len(after) else ""
    hits = [k for k in case.must_contain if _norm(k) in hay]
    intent_ok = bool(hits) if case.accept_any else len(hits) == len(case.must_contain)
    collateral = sum(
        1 for i in range(min(len(before), len(after)))
        if i != case.target and _norm(before[i]) != _norm(after[i])
    )
    return Trial(target_hit, intent_ok, collateral, count_delta, list(after))


# --- driver -------------------------------------------------------------------


def build_lab(complete_fn) -> ResearchLab:
    """A ResearchLab wired to the real catalog and a realistic dataset profile, so ``_pi_plan``
    builds the SAME prompt production builds. The Scientist is never invoked — only planning is."""
    ctx = HarnessContext(decisions={"dataset_result": DATASET_RESULT}, workspace=Path("/tmp"))

    class _Stub:
        def __init__(self) -> None:
            from bioagent.agents.registry import build_scientist_catalog
            self.catalog = build_scientist_catalog()

        def add_tools(self, *_a, **_k):
            return None

    return ResearchLab(ctx, LabConfig(max_steps=20, auto_select_skill=False),
                       complete_fn=complete_fn, scientist=_Stub())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True, help="local port forwarded to vLLM /v1")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", default=str(Path(__file__).parent / "results.json"))
    args = ap.parse_args()

    def complete_fn(messages):
        return vllm_client.complete(args.port, MODEL, messages, timeout=600.0)

    lab = build_lab(complete_fn)
    results: dict[str, dict[str, list[dict]]] = {}

    for case in CASES:
        results[case.name] = {"redraft": [], "patch": []}
        for rep in range(args.reps):
            # Arm A — the production path, verbatim. NOTE: since commit `plan patch`, `_pi_plan`
            # itself tries a single-step patch before redrafting, so re-running this experiment
            # measures the FIXED path. That is the point: the arm-A numbers moving from 0% clean to
            # ~100% is the proof the fix works on the real model, not just in unit tests.
            try:
                kind, payload = lab._pi_plan(case.question, lambda _e: None,
                                             feedback=case.feedback, prior_agenda=case.agenda,
                                             allow_clarify=True, latest_feedback=case.feedback)
                after = list(payload) if kind == "agenda" else list(case.agenda)
                a = score(case, after)
                if kind != "agenda":
                    a = Trial(False, False, 0, 0, list(case.agenda))   # asked a question instead
            except Exception as exc:  # noqa: BLE001
                print(f"  [redraft {case.name} rep{rep}] ERROR {type(exc).__name__}: {exc}")
                continue
            results[case.name]["redraft"].append(a.__dict__)

            # Arm B — patch + deterministic apply.
            try:
                b = score(case, patch_revise(complete_fn, case.agenda, case.feedback))
            except Exception as exc:  # noqa: BLE001
                print(f"  [patch {case.name} rep{rep}] ERROR {type(exc).__name__}: {exc}")
                continue
            results[case.name]["patch"].append(b.__dict__)
            print(f"{case.name:16s} rep{rep}  redraft: hit={a.target_hit} intent={a.intent_ok} "
                  f"collateral={a.collateral} delta={a.count_delta} clean={a.clean}   |   "
                  f"patch: hit={b.target_hit} intent={b.intent_ok} collateral={b.collateral} "
                  f"delta={b.count_delta} clean={b.clean}")

    Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\n=== SUMMARY (rate over all trials) ===")
    for arm in ("redraft", "patch"):
        trials = [Trial(**t) for c in results.values() for t in c[arm]]
        if not trials:
            continue
        n = len(trials)
        print(f"{arm:8s} n={n:3d}  clean={sum(t.clean for t in trials) / n:5.0%}  "
              f"target_hit={sum(t.target_hit for t in trials) / n:5.0%}  "
              f"intent_ok={sum(t.intent_ok for t in trials) / n:5.0%}  "
              f"mean_collateral={statistics.mean(t.collateral for t in trials):.2f}  "
              f"any_collateral={sum(t.collateral > 0 for t in trials) / n:5.0%}  "
              f"len_changed={sum(t.count_delta != 0 for t in trials) / n:5.0%}")
    print(f"\nRaw trials -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
