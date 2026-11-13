"""The lab's data types: specialists, the run configuration, rounds, verdicts, results and resume state."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from ...reporting.qc_summary import qc_result_summary
from ..tool_names import canonical_tool_name
from ..scope import QC_ONLY_TOOLS, qc_only_run
from ..preset_pipelines import PresetPipeline
from ..research_harness import HarnessConfig, step_succeeded


# complete_fn(messages) -> assistant text. Plain reasoning turns for PI/Critic.
LabRoleFn = Callable[[list[dict[str, Any]]], str]
# code_executor(code) -> result dict. Runs a CodeAct snippet (sandbox on the server).
CodeExecutor = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class Specialist:
    """A domain-expert persona the Scientist adopts for a step (Virtual-Lab style:
    different agenda steps are handled by different specialists)."""

    name: str
    persona: str                     # system addendum prepended to the Scientist brief
    keywords: tuple[str, ...] = ()   # route a step to this specialist by its wording


DEFAULT_SPECIALISTS: tuple[Specialist, ...] = (
    Specialist(
        "QC & preprocessing specialist",
        "You specialize in single-cell QC and preprocessing: cell/gene filtering, mitochondrial "
        "content, normalization, and highly-variable-gene selection. Be rigorous about thresholds "
        "and report exactly what was filtered and why.",
        ("qc", "quality", "filter", "mito", "normal", "preprocess", "hvg", "doublet"),
    ),
    Specialist(
        "Clustering & cell-type specialist",
        "You specialize in dimensionality reduction, clustering (Leiden/UMAP) and identifying marker "
        "genes / candidate cell types. Treat every cell-type assignment as a hypothesis to validate.",
        ("cluster", "umap", "leiden", "marker", "cell type", "celltype", "annotat",
         "differential", "subpopulation", "pca"),
    ),
    Specialist(
        "Pathway & enrichment specialist",
        "You specialize in pathway / GO enrichment (GSEA/ORA) and biological interpretation of gene "
        "sets. Ground every pathway claim in the enrichment statistics the tools return.",
        ("enrich", "pathway", "gsea", "ora", "kegg", "reactome", "ontology", "interpret", "function"),
    ),
)

GENERALIST = Specialist(
    "Generalist bioinformatician",
    "You are a careful generalist: use the available tools, ground every claim in their outputs, "
    "and never invent numbers or gene symbols.",
)


def _route_specialist(step: str, roster: tuple[Specialist, ...]) -> Specialist:
    """Pick the specialist whose expertise best matches the step (most keyword hits);
    fall back to the generalist when nothing matches. Deterministic + testable."""
    s = step.lower()
    best_score, best = 0, GENERALIST
    for sp in roster:
        score = sum(1 for k in sp.keywords if k in s)
        if score > best_score:
            best_score, best = score, sp
    return best


#: The Critic's score bands, as ONE piece of data — the console shows what a score MEANS instead of
#: a bare number, and it must be the same meaning the Critic was given. The prompt above states the
#: bands in prose (a model reads prose, not a table); ``test_critic_score_bands`` asserts every
#: boundary here still appears in ``_CRITIC_SYSTEM``, so the two cannot drift apart silently.
#: English, deliberately: this is the model's own rubric wording, and translating it would put a
#: second, slightly different rubric in front of the reader.
CRITIC_SCORE_BANDS: tuple[tuple[float, str], ...] = (
    (0.95, "goal fully met; every quantitative claim tied to an evidence artifact"),
    (0.80, "solid, usable result; a minor claim is thin or a small sub-goal is unmet"),
    (0.60, "partially meets the goal, or a material claim rests on prose with no backing artifact"),
    (0.00, "no usable result, goal not met, or a claim contradicted by the tool results"),
)


def critic_score_band(score: "float | int | None") -> str:
    """The rubric band a Critic score falls in, as the Critic itself was told to apply it.

    A bare "quality 0.76" is unreadable without the rubric, and the rubric lived only inside a
    prompt string. Returns "" for a missing/non-numeric score rather than inventing a band.
    """
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return ""
    for floor, label in CRITIC_SCORE_BANDS:
        if score >= floor:
            return label
    return CRITIC_SCORE_BANDS[-1][1]


def _parse_team(raw: str, max_n: int) -> list[Specialist]:
    """Parse the PI's team JSON into Specialist personas (independent-context experts).
    Tolerates code fences / a surrounding sentence; returns [] on failure (caller falls back)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    parsed: Any = None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    out: list[Specialist] = []
    if isinstance(parsed, list):
        for e in parsed[:max_n]:
            if not isinstance(e, dict):
                continue
            title = str(e.get("title") or "").strip()
            expertise = str(e.get("expertise") or "").strip()
            goal = str(e.get("goal") or "").strip()
            if not title:
                continue
            persona = expertise + (f" Your goal: {goal}" if goal else "")
            kws = tuple(set(re.findall(r"[a-z]{4,}", (title + " " + expertise).lower())))
            out.append(Specialist(title, persona or "Domain expert.", kws))
    return out


@dataclass(frozen=True)
class LabConfig:
    # None (default) = derive the round budget from the agenda so EVERY planned step gets to run.
    # Each step is still capped at ``max_revisions`` retries (force-advance), so total work is
    # inherently bounded by len(agenda)×(1+max_revisions) — no step is ever starved. An explicit
    # int is a hard cap (used by tests, or a cloud override via AISCIENTIST_MAX_ROUNDS).
    max_rounds: int | None = None
    # Runaway guard on PI agenda length — NOT a planning budget. The PI plans as many steps as the
    # analysis genuinely needs (a thorough study is commonly 6-12 and up to ~20 is fine); this only
    # stops a hallucinated/pathological plan. Tunable via AISCIENTIST_MAX_STEPS.
    max_steps: int = 20
    max_revisions: int = 2              # revisions on one step before force-advancing
    # A step the plan did not contain (exploration proposes one after a surprising result) is run
    # only after the user approves it. Nobody to ask — bypass mode, a resumed run, an earlier
    # proposal left unanswered — means it is NOT run; it is listed for the user to ask for.
    # False restores the unattended behaviour (the step is added without asking): an operator's
    # choice for autonomous campaigns, AISCIENTIST_NEW_STEPS_AUTO=1.
    new_steps_need_approval: bool = True
    # What this conversation's earlier run(s) already did, when this run CONTINUES one: the steps,
    # their findings and what the dataset handed on carries (gateway ``_prior_work_block``). Shown
    # to the PI and the plan review so the new plan adds to that work instead of repeating it.
    prior_work: str = ""
    # Planner/scheduler. "linear" = the flat-agenda _run_loop (default, unchanged). "dag" =
    # structure the agenda into a dependency DAG and run it with the ready-set scheduler +
    # Coordinator (_run_dag): data-driven ordering, structural task reuse, scoped node briefs.
    # Branch feat/dag-planner — opt-in until proven, so main/prod stay on the linear loop.
    # "langgraph" = the SAME DAG executed by a LangGraph StateGraph (agents/lab_graph.py): the
    # graph owns nodes/edges/state/checkpointing, every node still runs through _run_one_node.
    # Requires the optional `langgraph` package; opt-in and not yet at parity with "dag" (no
    # Coordinator ordering, no mid-run agenda growth) — see ResearchLab._run_langgraph.
    planner: str = "linear"
    # Real multi-agent (DAG only): the team's experts CLAIM each ready node by expertise fit (an LLM
    # decides who does what) instead of deterministic keyword routing. Off = keyword routing.
    multi_agent: bool = False
    # DAG concurrency: max nodes run at once. 1 = sequential (default, unchanged). >1 lets the
    # scheduler co-run ONLY nodes with disjoint footprints (see _concurrency_safe) — in practice an
    # independent literature/background branch runs alongside the sequential analysis chain; two
    # analysis nodes never overlap (shared checkpoints + scanpy global state). Decision nodes run solo.
    max_concurrency: int = 1
    # Per-agent evolving memory (Axis C — docs/agent_memory_design.md). Off = no memory (today's
    # behaviour). On: each expert reads its PRIVATE lessons/episodes into its brief before acting,
    # writes an episode after, and reflects (distils lessons) at end of run — cross-run learning with
    # frozen weights. ``agent_memory_dir`` is the PERSISTENT root (outside any per-run workspace, so
    # memory survives across runs); None disables even when the flag is on.
    agent_memory: bool = False
    agent_memory_dir: str | None = None
    # Literature grounding: the model reads the accepted findings and writes several DISTINCT
    # Europe PMC queries (per cell class / per pathway / disease mechanism) instead of one generic
    # keyword mash-up. Cap the count so a literature step stays a handful of fast searches.
    max_literature_queries: int = 4
    multi_specialist: bool = True       # route each step to a domain specialist persona
    specialists: tuple[Specialist, ...] = DEFAULT_SPECIALISTS
    # Axis A — execution mode. "single" = one Scientist per step (the default loop, unchanged).
    # "team" = Virtual-Lab multi-agent: the PI forms a team, a team meeting DESIGNS the approach
    # and (after execution) INTERPRETS the results. Experts give diverse independent takes in
    # round 1, then COLLABORATE across rounds by building on the PI's shared synthesis (not each
    # other's raw turns, so they stay batchable). "auto" = the PI routes single vs team itself.
    mode: str = "single"
    team_size: int = 3                  # max experts the PI forms in team mode
    # Default 2 so collaboration actually happens: round 1 = diverse independent takes, round 2 =
    # experts BUILD ON the PI's shared synthesis + the Critic's score-driven feedback. A meeting
    # ends early (before using all rounds) once the Critic's score clears meeting_accept_score, so
    # easy topics stay cheap and contested ones get more deliberation — A100-adaptive.
    meeting_rounds: int = 2
    meeting_accept_score: float = 0.85  # Critic meeting-score that ends a meeting early (converged)
    # A100 limit: one vLLM serves the whole team, so the experts in a meeting are issued
    # CONCURRENTLY and vLLM's continuous batching runs them together on the single GPU. This
    # caps in-flight requests so a big team can't blow the KV-cache pool. Tune to the card.
    max_meeting_concurrency: int = 4
    # Meeting experts may call READ-ONLY tools (_MEETING_TOOLS) instead of only talking, so an
    # expert can verify its assigned evidence or fetch outside evidence the others lack. Costs
    # extra turns per expert; off falls back to plain completions.
    meeting_tools: bool = True
    meeting_tool_calls: int = 4          # tool turns one expert may spend per meeting round
    # PI↔Critic step meetings (docs/pi_critic_meeting_protocol.md). Two bounded PI↔Critic exchanges
    # wrapped around each step: a PRE-FLIGHT necessity/reasonableness gate before the Scientist runs
    # (skip a circular/redundant/unfounded step, or amend its brief) and a POST-STEP contribution
    # review after the Critic (prune remaining steps a completed step made moot). A deterministic
    # floor (enrichment without a contrast) hard-skips regardless of the models. Off = today's loop.
    # Fully enacted on the linear planner; the DAG planner enacts amend + the floor only (a model
    # "skip"/downstream-prune there needs the scheduler's dependency-aware replan — see the doc).
    step_meetings: bool = False
    # PI↔Critic review of the WHOLE draft agenda, BEFORE any step runs — split OUT of
    # ``step_meetings`` and defaulted ON, because it was the only layer that could catch an
    # incoherent plan and it was never reachable in production: the gateway never set
    # ``step_meetings``, so the review existed solely for tests while real runs went straight from
    # a one-shot draft to execution. A production plan then re-clustered a dataset that already
    # carried 11 expert cell-type labels, planned a pseudobulk step against data with no donor
    # column, and contradicted itself between steps 3 and 5 — every one of which is on this
    # Critic's checklist. It costs two completions per run (one Critic, one PI, and the PI round is
    # skipped when the Critic finds nothing), which is cheap against a whole run spent on a plan
    # that could not have worked. Env kill-switch: AISCIENTIST_PLAN_REVIEW=0.
    plan_review: bool = True
    # The plan as a YAML DOCUMENT (agents/plan_doc.py) beside the prose agenda: every step carries
    # the tool it runs and the settings the plan fixes, the researcher may edit the document itself
    # (applied by code, no model call), an approved setting is passed to the tool as written, and
    # the finished run is read back against the approved plan. Off = the prose agenda alone: nothing
    # binds a tool to the number on the plan card. The gateway turns it on unless AISCIENTIST_PLAN_YAML=0.
    plan_yaml: bool = False
    # Hypothesis-driven exploration — the ONLY path by which the plan GROWS mid-run. Off = today's
    # behaviour exactly: the agenda drafted before any data is seen can be skipped/amended/pruned but
    # never extended, so the run can only ever execute the paths it started with. On: after a step is
    # ACCEPTED the PI reads its result against the whole plan and, if the result genuinely contradicts
    # the plan's premise, records a FALSIFIABLE hypothesis (statement + prediction + discriminating
    # test) and appends the step(s) that test it — each one still passing the pre-flight gate, the
    # Critic, and ``max_steps`` before it costs anything. Later steps adjudicate the open hypotheses,
    # so the ledger closes the loop instead of just generating work. See ``agents/hypotheses.py``.
    # On is the operator's permission, not a default for every step: a step is explored only when a
    # skill it uses declares ``exploration`` in its SKILL.md metadata (``_explores_after``, #102).
    hypothesis_driven: bool = False
    # Hard cap on steps ADDED by exploration across the whole run (``max_steps`` still caps the total
    # plan length). This is the runaway guard: without it a model that finds everything surprising
    # can extend the plan every time it finishes a step and the run never terminates. Widened from 6
    # once the economics were corrected: on the lab's own free GPUs the binding constraint should be
    # whether the plan still coheres (``max_steps``, the pre-flight gate, the Critic), not a small
    # cash-flavoured number. Tunable via AISCIENTIST_MAX_NEW_STEPS.
    max_new_steps: int = 16
    # Multi-CYCLE campaign. 1 (default) = today's behaviour exactly: plan once, execute, write up.
    # >1 = after a cycle finishes, the PI re-plans the next one FROM WHAT THE DATA SAID (open
    # hypotheses, accepted findings), and the manuscript is written once over every cycle's rounds.
    # Complementary to ``hypothesis_driven``, not a replacement: exploration reacts to one step
    # inside a cycle, a cycle re-plans wholesale. Bounded by max_cycles plus the deterministic
    # "nothing left to chase" / "no progress" exits in ``_run_campaign``.
    max_cycles: int = 1
    # Skill INDUCTION (agents/skill_induction.py): at the end of a run, generalize an accepted
    # ``run_code`` procedure into a reusable ``SKILL.md`` + ``reference.py`` that later runs can
    # find and adapt. Off = the library only ever holds hand-authored skills (today's behaviour).
    # Requires ``induced_skills_dir``: induced skills are written OUTSIDE the repo, and with no
    # directory configured there is nowhere safe to put them, so the flag alone does nothing.
    skill_induction: bool = False
    induced_skills_dir: str | None = None
    max_induced_skills: int = 2         # per run — a library of near-duplicates is worse than none
    # The gateway's id for this run: an induced skill records it as ``origin_run``, so its review
    # and the run's skill request (gateway/skill_requests.py) point at each other.
    run_id: str = ""
    # Run-scope context management (agents/context_budget.py). The Scientist's brief carries every
    # accepted finding, so that prefix grows linearly and a long run ends up paying for its own
    # history until the per-step trimmer starts discarding real work. On: the run MEASURES that
    # carried block against its share of the served window and compacts it — deterministically
    # decided, model used only to write the digest, evidence pointers re-attached mechanically.
    # Off = today's behaviour (the per-step trimmer still runs; nothing measures the run scope).
    context_management: bool = False
    compact_keep_recent: int = 3        # most recent accepted steps always kept verbatim
    scientist: HarnessConfig = field(default_factory=HarnessConfig)
    # Output room (in tokens) GUARANTEED for a single-shot PI/Critic/synthesize reply. The
    # window (``scientist.max_model_len``) caps prompt+output together; when the prompt is
    # small the reply may use everything left over (so a long manuscript is not capped), but
    # we never let it drop below this — truncating an oversized prompt instead. Generous by
    # default because the synthesize node writes the full manuscript.
    reply_reserve_tokens: int = 8192
    # Optional pre-selected research-path guidance. STEERS the PI's planning (the PI
    # still drafts the agenda + plan mode still lets the user edit it) — it does not
    # bypass the PI. None = let the PI auto-select a skill (below), else free planning.
    preset_prompt: str | None = None
    # Axis B — PI-autonomous skill selection. When no ``preset_prompt`` is given (the user
    # did not force a path), the PI reads the skill library's descriptions and picks the
    # best-matching research protocol itself; its body becomes the planning guidance. An
    # explicit ``preset_prompt`` always wins (the gateway dropdown = optional override).
    auto_select_skill: bool = True
    skill_library: "tuple[PresetPipeline, ...] | None" = None  # None -> load from preset_pipelines/
    # User-PINNED preset pipelines (the console's pipeline multi-select): MANDATORY for this run.
    # auto_select_skill still runs and ADDS the PI's best-fit pipeline on top (pinned are must-have).
    pinned_skills: "tuple[PresetPipeline, ...]" = ()
    # User-REQUIRED atomic skills (the console's skill multi-select): names from the skill
    # library the run MUST apply. Unlike the global on-demand manifest, these are mandatory.
    required_skills: "tuple[str, ...]" = ()


@dataclass(frozen=True)
class CriticVerdict:
    verdict: str   # "accept" | "revise"
    score: float
    critique: str
    reviewed: bool = True
    # The check of the step's declared outputs (skill_outputs.py), when it had any: what was
    # expected, from which skill or pipeline, and what was found. The report's Diagnostics read it.
    outputs: "dict[str, Any] | None" = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CriticVerdict":
        outputs = d.get("outputs")
        return cls(str(d.get("verdict", "revise")), float(d.get("score", 0.0)),
                   str(d.get("critique", "")), bool(d.get("reviewed", True)),
                   outputs if isinstance(outputs, dict) else None)


@dataclass(frozen=True)
class PreflightDecision:
    """Outcome of the PI↔Critic pre-flight gate for one step (docs/pi_critic_meeting_protocol.md)."""
    action: str            # "proceed" | "amend" | "skip"
    reason: str = ""
    amendment: str = ""
    by: str = "model"      # "off" | "guard" (deterministic floor) | "critic" | "pi"


@dataclass
class LabRound:
    round_no: int
    step_index: int
    step: str
    specialist: str
    scientist_result: dict[str, Any]   # HarnessResult.to_dict()
    verdict: CriticVerdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_no": self.round_no,
            "step_index": self.step_index,
            "step": self.step,
            "specialist": self.specialist,
            "scientist_result": self.scientist_result,
            "verdict": asdict(self.verdict),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LabRound":
        return cls(
            int(d.get("round_no", 0)), int(d.get("step_index", 0)),
            str(d.get("step", "")), str(d.get("specialist", "")),
            dict(d.get("scientist_result", {})), CriticVerdict.from_dict(d.get("verdict", {})),
        )


def _stopped_round_answer(rnd: LabRound) -> str:
    """Stopped QC steps use their records; preserve other analyses' outcomes and all raw rounds."""
    if not qc_only_run([rnd]):
        return str(rnd.scientist_result.get("final_answer") or "(no answer)")
    summaries = []
    for step in rnd.scientist_result.get("steps") or []:
        if not isinstance(step, dict) or canonical_tool_name(step.get("tool")) not in QC_ONLY_TOOLS:
            continue
        result = step.get("result")
        if step_succeeded(step) and isinstance(result, dict):
            summaries.append(f"{step['tool']}: {qc_result_summary(result)}")
        elif isinstance(result, dict) and result.get("error"):
            summaries.append(f"{step['tool']} failed: {result['error']}")
    return " ".join(summaries) or "QC results are recorded in the run's technical details."


def _rounds_by_agenda_index(agenda: list[str], rounds: "list[LabRound]") -> "dict[int, LabRound]":
    """Map each round to the 0-based agenda step it ran, by the step's TEXT.

    ``LabRound.step_index`` is the agenda position only on the linear loop. The DAG scheduler, the
    LangGraph path and multi-cycle runs number rounds in completion order instead: in run
    78a707cd79e9 round 5 is the clustering step, agenda step 4, so keying a resume by ``step_index``
    handed the marker-gene step the clustering result. ``step_index`` still decides between two steps
    with the same text. A later round for the same step wins (a revise, then its accept). A round
    whose text is no longer in the agenda (a resume replaced that step's text) maps nowhere."""
    positions: dict[str, list[int]] = {}
    for i, text in enumerate(agenda):
        positions.setdefault(text, []).append(i)
    by_index: dict[int, LabRound] = {}
    for r in rounds:
        candidates = positions.get(r.step)
        if not candidates:
            continue
        by_index[r.step_index - 1 if (r.step_index - 1) in candidates else candidates[0]] = r
    return by_index


@dataclass
class LabResult:
    question: str
    agenda: list[str]
    rounds: list[LabRound]
    converged: bool
    accepted_steps: int
    final_answer: str
    # Hypotheses the RUN generated (empty unless ``config.hypothesis_driven``) — the research paths
    # the original plan did not contain. Last field with a default so every existing positional
    # construction keeps working.
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    # Papers the EXPERTS looked up while designing the study (meeting-phase ``literature_search`` /
    # ``deep_literature``). Deliberately NOT merged into the report's ``## References``: those are
    # claimed to support the findings and are drawn only from Critic-ACCEPTED analysis steps, while
    # these were never adjudicated — they informed the plan. Discarding them was the other error:
    # a run can spend a dozen real, DOI-backed lookups shaping its design and show none of them.
    design_background: list[dict[str, Any]] = field(default_factory=list)
    # The claim audit run before the write-up (agents/claim_audit.py): each candidate headline finding,
    # the separate checks it went through and its status. Binding for both writers; shown in the
    # technical report. Empty when the audit is off or nothing was accepted.
    claim_audit: dict[str, Any] = field(default_factory=dict)
    # The steps the user skipped at a failed step's decision point ("Skip this step"), by their text.
    # The rounds show that such a step was never accepted, not that the user chose to leave it; the
    # QC reply says which (RCHENLAB/AiScientist issue 75).
    skipped_steps: list[str] = field(default_factory=list)
    # The approved plan had a ``write_report`` step (#102): the gateway writes the report after the
    # analysis. False: the run ends with its answer in the chat.
    report_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "agenda": list(self.agenda),
            "rounds": [r.to_dict() for r in self.rounds],
            "converged": self.converged,
            "accepted_steps": self.accepted_steps,
            "final_answer": self.final_answer,
            "hypotheses": list(self.hypotheses),
            "design_background": list(self.design_background),
            "claim_audit": dict(self.claim_audit),
            "skipped_steps": list(self.skipped_steps),
            "report_requested": self.report_requested,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LabResult":
        return cls(
            str(d.get("question", "")), list(d.get("agenda", [])),
            [LabRound.from_dict(r) for r in d.get("rounds", [])],
            bool(d.get("converged", False)), int(d.get("accepted_steps", 0)),
            str(d.get("final_answer", "")),
            [h for h in (d.get("hypotheses") or []) if isinstance(h, dict)],
            [c for c in (d.get("design_background") or []) if isinstance(c, dict)],
            dict(d.get("claim_audit") or {}),
            [str(s) for s in (d.get("skipped_steps") or []) if str(s).strip()],
            bool(d.get("report_requested", False)),
        )


@dataclass(frozen=True)
class ResumeState:
    """Enough to re-enter :meth:`ResearchLab.run` WITHOUT re-planning — the A2 continuation state.

    ``agenda`` is the prior run's plan (the caller may have edited one step's text, e.g. a new
    clustering resolution). ``prior_rounds`` are ALL the accepted rounds from the prior run — the
    ones NOT re-run are reused verbatim as read-only findings; their on-disk checkpoints
    (``work/adata_*.h5ad``) must still exist. ``from_step_index`` is the CHANGED step (always
    re-run). Which DOWNSTREAM steps also re-run is decided per-run: when ``redo_indices`` is None the
    lab EVALUATES which later steps actually depend on the change and re-runs only those (an
    independent literature step, say, is kept); pass an explicit set to force the choice.
    ``modify_note`` steers the changed step; ``guidance`` restores the skill steering;
    ``self_sourced`` is the plan's own-judgement disclosure, which the write-up quotes.
    """

    agenda: list[str]
    prior_rounds: list["LabRound"]
    from_step_index: int            # 0-based index of the changed step (always re-run)
    modify_note: str = ""
    guidance: str | None = None
    redo_indices: "frozenset[int] | None" = None   # None = evaluate; else the exact 0-based set to re-run
    self_sourced: str = ""
    report_requested: bool = False   # the plan had a write_report step, kept out of ``agenda`` (#102)

    @classmethod
    def from_run_state(cls, state: dict[str, Any], from_step_index: int, *, modify_note: str = "",
                       guidance: str | None = None,
                       redo_indices: "set[int] | frozenset[int] | None" = None) -> "ResumeState":
        """Build a resume from a persisted run_state (a :meth:`LabResult.to_dict`, plus optional
        ``guidance``). Keeps ALL accepted rounds — the run loop decides per step whether to reuse or
        re-run it (see :meth:`ResearchLab._evaluate_redo_indices`)."""
        rounds = [LabRound.from_dict(r) for r in state.get("rounds", [])]
        kept = [r for r in rounds if r.verdict.verdict == "accept"]
        return cls(list(state.get("agenda", [])), kept, int(from_step_index),
                   modify_note=modify_note, guidance=guidance or state.get("guidance"),
                   redo_indices=frozenset(redo_indices) if redo_indices is not None else None,
                   self_sourced=str(state.get("self_sourced") or ""),
                   report_requested=bool(state.get("report_requested", False)))

    @classmethod
    def from_interrupted(cls, state: dict[str, Any], *, guidance: str | None = None) -> "ResumeState":
        """Pick up a run that stopped part-way — a crash, a gateway restart, a dropped session — from
        the run_state.json it checkpoints after every round: keep every step that has an accepted
        result and run every step that has none, starting with the first. Nothing is re-evaluated or
        re-run that already finished. Raises ``ValueError`` when no step is left to run."""
        resume = cls.from_run_state(state, 0, guidance=guidance)
        done = _rounds_by_agenda_index(resume.agenda, resume.prior_rounds)
        todo = [i for i in range(len(resume.agenda)) if i not in done]
        if not todo:
            raise ValueError("Every planned step of this run already has an accepted result — "
                             "there is nothing left to continue. Re-run a chosen step instead.")
        return cls(resume.agenda, resume.prior_rounds, todo[0], guidance=resume.guidance,
                   redo_indices=frozenset(todo), self_sourced=resume.self_sourced,
                   report_requested=resume.report_requested)


def _from_fields(cls: Any, data: "dict[str, Any]") -> Any:
    """``cls(**data)`` with only the fields ``cls`` has: a plan saved before a deploy that drops a
    field is still restored after it."""
    from dataclasses import fields

    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})
