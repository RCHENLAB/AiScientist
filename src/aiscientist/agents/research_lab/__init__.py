"""Role-based multi-agent research workflow (PI → Scientist → Critic → converge).

A Virtual-Lab-style team loop layered on the existing tool-calling ``ResearchHarness``:

    PI ──plans──▶ agenda (ordered steps)
       │
       ▼   ┌────────────── per step, until accepted or budget ──────────────┐
    Scientist ─runs tools (hybrid: function-calling + CodeAct)─▶ result
       │                                                          │
       ▼                                                          ▼
    Critic ─judges {accept | revise} + score + critique─▶ advance / revise
       │
       ▼ (all steps accepted, or rounds exhausted)
    PI ──synthesizes──▶ final report (grounded ONLY in accepted results)

Design choices the user asked for:

- **Critic is first-class.** A dedicated reviewer scores each step and drives
  convergence; a deterministic guard refuses to "accept" a step whose Scientist run
  errored or produced no answer (the model-critic can't rubber-stamp a failure).
- **Hybrid execution.** The Scientist's toolset is the curated function-calling
  catalog PLUS a ``run_code`` tool — CodeAct (write-and-run Python) exposed *as a
  function tool*. So structured tools stay reliable (vLLM tool-parser) while the
  model still has full code flexibility for the long tail. Biomni's own CodeAct
  remains reachable via the existing ``run_biomni`` tool.
- **LangGraph-ready.** Each role (``_pi_plan`` / ``_scientist`` / ``_critic`` /
  ``_synthesize``) is a pure-ish method over an explicit state — a 1:1 map onto
  LangGraph nodes, with the accept/revise branch as the conditional edge. Porting
  later means wiring these methods as nodes, not rewriting the logic.

Everything is offline-testable: PI/Critic completions and the Scientist's tool
chat are all injectable (no GPU/LLM needed for tests).
"""

from __future__ import annotations

import copy
import inspect
import json
import re
import threading
import time
from pathlib import Path
from dataclasses import asdict, replace
from typing import Any, Callable
from .. import claim_audit
from ..dag import LabPlan, TaskNode, lift_agenda_to_dag, parse_dag
from ..cellqc_cutoffs import CUTOFFS as CUTOFF_SETTINGS, cutoff_rows, read_cutoffs
from ..hypotheses import DesignFacts, HypothesisLedger
from ..loop_utils import safe_json_loads
from ..tool_schema import REPORT_TOOL, PlanParameters
from ..plan_doc import (
    CODE_TOOLS,
    PlanDoc,
    PlanError,
    PlanStep,
    apply_edit,
    apply_ops,
    bind_settings,
    call_settings,
    check_adherence,
    check_plan,
    parse_plan_ops,
    parse_plan_yaml,
    split_step,
    sync_plan,
)
from ..tool_names import canonical_tool_name
from ..scope import RunScope, qc_only_run, read_scope, rules_out_report, wants_written_report
from .. import own_choices
from ..preset_pipelines import (
    PresetPipeline,
    compose_pipeline_prompts,
    drop_conflicting_pinned,
    select_pipeline,
)
from .. import skill_outputs
from ..skill_code import fit_scripts, skill_functions
from ..skills import (
    SKILLS as ATOMIC_SKILLS,
    Skill,
    declares,
    default_file,
    get_skill,
    make_search_skills_tool,
    make_skill_reference_tool,
    plan_skill_lines,
    refresh_skills,
    skill_manifest,
    skills_named_in,
    superseded_names,
)
from ..step_numbers import describe_count_mismatches, find_count_mismatches
from ..tool_source import make_tool_source_tool
from ..research_harness import (
    EventFn,
    HarnessContext,
    HarnessResult,
    ResearchHarness,
    _CHARS_PER_TOKEN,
    _msg_tokens,
    evidence_pointers,
    result_digest,
    step_succeeded,
    tool_result_outcome,
)

# The package's modules. Every name they define is importable from here.
from .prompts import (  # noqa: F401
    _ALTERNATIVES_SYSTEM,
    _CLAIM_SYSTEM,
    _COMPARISON_ROUTE_SYSTEM,
    _COORDINATOR_SYSTEM,
    _CRITIC_SYSTEM,
    _DAG_STRUCTURE_SYSTEM,
    _DEEP_LIT_QUESTION_SYSTEM,
    _EXPLORE_SYSTEM,
    _LIT_QUERY_SYSTEM,
    _MEETING_CRITIC_SYSTEM,
    _MEETING_SYNTH_SYSTEM,
    _MODE_ROUTE_SYSTEM,
    _NEXT_CYCLE_SYSTEM,
    _PI_SYSTEM,
    _PLAN_AUTHOR_STEP_SYSTEM,
    _PLAN_BIND_SYSTEM,
    _PLAN_FAILED_MESSAGE,
    _PLAN_PATCH_SYSTEM,
    _PLAN_QA_SYSTEM,
    _PLAN_REFORMAT,
    _PLAN_REVIEW_CRITIC_SYSTEM,
    _PLAN_REVIEW_PI_SYSTEM,
    _PLAN_REVISE_SYSTEM,
    _PLAN_SKILLS_HEADER,
    _PLAN_SKILLS_SYSTEM,
    _POSTSTEP_PI_SYSTEM,
    _PREFLIGHT_GATE_SYSTEM,
    _PREFLIGHT_PI_SYSTEM,
    _QC_REPORT_NOTE,
    _RESUME_EVAL_SYSTEM,
    _SYNTH_SYSTEM,
    _TEAM_FORM_SYSTEM,
    plan_skills_block,
)
from .models import (  # noqa: F401
    CRITIC_SCORE_BANDS,
    CodeExecutor,
    CriticVerdict,
    DEFAULT_SPECIALISTS,
    GENERALIST,
    LabConfig,
    LabResult,
    LabRoleFn,
    LabRound,
    PreflightDecision,
    ResumeState,
    Specialist,
    _from_fields,
    _parse_team,
    _rounds_by_agenda_index,
    _route_specialist,
    _stopped_round_answer,
    critic_score_band,
)
from .plan_parse import (  # noqa: F401
    MAX_LATER,
    NEW_STEP_ASK_SECONDS,
    _APPROVAL_WORDS,
    _BARE_DISCLOSURE_RE,
    _CHANGE_VERBS,
    _NOISE_REPLY_RE,
    _PROPOSAL_OPENERS,
    _PURE_INTERROGATIVES,
    _PURE_INTERROGATIVES_ZH,
    _QUESTION_ANYWHERE,
    _QUESTION_MARKS,
    _QUESTION_OPENERS,
    _SELF_SOURCED_PREFIX,
    _SETTING_RE,
    _STEP_ORDINAL_RE,
    _close_unterminated_json,
    _drop_question_echo,
    _hand_edit_note,
    _node_step_text,
    _norm_step,
    _parse_agenda,
    _parse_plan,
    _parse_plan_extras,
    _parse_plan_patch,
    _parse_query_list,
    _parse_self_sourced,
    _parse_verdict,
    _settings_in,
    _similarity,
    _split_self_sourced,
    _step_title,
    _strip_step_ordinal,
    classify_plan_reply,
    diff_plans,
    is_noise_reply,
    stale_downstream_settings,
)
from .tooling import (  # noqa: F401
    _ANY_TOOL_CALL_RE,
    _BACKTICK_NAME_RE,
    _BACKTICK_TOOL_RE,
    _KWARG_RE,
    _TOOL_NAME_FIXES,
    _declared_tools,
    _describe_param,
    _env_arg,
    _known_tool_names,
    _names_tool,
    _planner_tool_description,
    _primary_tool,
    _tool_ends_step,
    _tool_runs_on,
    _tool_schemas,
    _tool_skill,
    check_plan_tooling,
    make_run_code_tool,
    make_run_in_environment_tool,
    plan_tool_contracts,
    retick_tools,
)
from .step_kinds import (  # noqa: F401
    REPORT_STEP,
    _ADD_INTENT_RE,
    _ANALYSIS_FAMILIES,
    _ASK_VERBS,
    _CELLTYPE_COL_RE,
    _CLUSTERING_STEP_RE,
    _CLUSTER_COLS,
    _COMPARISON_CUE,
    _DE_PRODUCER_RE,
    _DE_PRODUCER_TOOLS,
    _ENRICHMENT_STEP_RE,
    _ENRICHMENT_TOOLS,
    _GENERIC_TOOLS,
    _KEEP_CUE_RE,
    _LITERATURE_HEADLINE_RE,
    _LITERATURE_STEP_RE,
    _LITERATURE_TOOLS,
    _LITERATURE_ZH_RE,
    _LOW_PRIORITY_FOR_LITERATURE_RE,
    _MISSING_WORDS,
    _NEGATED_CLUSTERING_RE,
    _NEGATION,
    _PLAN_CUT_RE,
    _PLAN_TOPICS,
    _QC_COL_RE,
    _READBACK_TARGET_RE,
    _READBACK_VERB_RE,
    _READ_ONLY_TOOLS,
    _REMOVAL_AFTER_RE,
    _REMOVAL_CUE_RE,
    _REPORT_BUSYWORK_RE,
    _REPORT_TOOL_LINE,
    _REQUESTS_LITERATURE_RE,
    _WISH_CLAUSE_SPLIT_RE,
    _annotated_without_contrast,
    _clusters_de_novo,
    _concurrency_safe,
    _contrast_columns,
    _dataset_mode_rule,
    _is_de_producer_step,
    _is_definite_reference,
    _is_enrichment_step,
    _is_label_fork,
    _is_literature_step,
    _is_readback_step,
    _is_report_busywork,
    _is_report_step,
    _literature_step_wanted,
    _literature_wish,
    _looks_like_celltype_col,
    _looks_like_qc_col,
    _missing_analysis_family,
    _node_resources,
    _plan_has_clustering,
    _requests_literature,
    _step_headline,
    _topics_named,
    handed_on_note,
    resolve_plan_reference,
)
from .grounding import (  # noqa: F401
    _ASSEMBLY_CANON,
    _COUNT_RE,
    _COVERAGE_RE,
    _FACT_DIST_KEYS,
    _FACT_KEYS,
    _PRE_QC_RE,
    _annotated_cell_types,
    _assign_evidence,
    _class_labels,
    _collect_facts,
    _grounding_facts,
    _grounding_vocab,
    _label_reference,
    _marker_reference,
    _methods_performed,
    _round_feedback,
    _swap_to_tested,
    _term_label,
    _tested_counts,
    _tool_classes,
    _uncovered_groups,
    correct_coverage_counts,
    pre_qc_counts,
    resolve_evidence,
    verify_report_facts,
)
from .literature import (  # noqa: F401
    MAX_LISTED_CITATIONS,
    _CANNOT_ANSWER,
    _ENTITY_TOKEN_RE,
    _LITERATURE_GENERIC,
    _LITERATURE_NOT_ENTITIES,
    _deep_literature_fallback_question,
    _ensure_literature_agenda,
    _literature_answer,
    _literature_findings_digest,
    _literature_query,
    _literature_step_text,
)


def _call_with_role(fn: "Callable[..., str]", messages: list[dict[str, Any]], role: str) -> str:
    """Call an injected ``complete_fn``, passing ``role`` only to one that accepts it.

    The ``(messages) -> str`` contract is shared by the kernel, skill induction, context digests,
    agent memory and every test double — most of which are plain lambdas. Production's fn takes an
    extra keyword so the gateway can pick a per-role output ceiling and label the usage row. Probing
    the signature keeps both callers valid without a flag day, and without catching ``TypeError``
    around the call, which would silently swallow a real one raised INSIDE the function.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):        # builtins / C callables expose no signature
        return fn(messages)
    accepts_role = "role" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    return fn(messages, role=role) if accepts_role else fn(messages)


# How many times a single hard-failed node may raise a "retry / skip / abort" fork before it just
# force-advances — bounds the retry loop so a persistently-failing step can never hang the run.
_MAX_FAILURE_FORKS = 2
# ...and how many of those forks may reach the PERSON, across the WHOLE run. The per-node cap
# above bounds one step; it says nothing about a run of fifteen steps, which could therefore put
# thirty cards in front of a reviewer. Run 3c5fbc8608a7 is why this exists: one step alone failed
# nine times. Past this many asks the fork still happens and is still bounded per node — the agent
# just stops asking and applies the best alternative itself, which is exactly what it does in
# headless mode. Being asked a fourth time is not more control; it is the same question again from
# a run that has already shown it cannot answer it.
_MAX_HUMAN_FAILURE_ASKS = 3


#: Below this a "describe" answer keeps the design meeting.
_DESCRIBE_CONFIDENCE = 0.6


# Tools a MEETING expert may call. A whitelist, never a blacklist: a meeting must not be able to
# mutate the run's state, and the analysis catalog is full of tools that rewrite the checkpoint
# chain. These three only READ — the data file, and the published literature — which is exactly
# what lets one expert hold evidence the others do not.
_MEETING_TOOLS = frozenset({"inspect_dataset", "literature_search", "deep_literature", "search_corpus"})


_PLAN_FINDING_WORDS = {
    "unknown_tool": "step {step} names `{tool}`, which is not a tool this deployment has",
    "unknown_setting": "step {step}: `{tool}` has no setting `{param}` (it takes: {detail})",
    "bad_value": "step {step}: `{tool}` cannot take {param} = {detail}",
    "unknown_skill": "step {step} names the skill `{param}`, which is not in this deployment's skill library",
}


#: How much of an assigned skill's SKILL.md and code goes into a step's brief; the rest stays one
#: ``read_skill_reference`` away, and the brief says where.
_SKILL_DOC_CHARS = 12_000
# The most of a DATASET.md the planner reads; the whole file is in the run's data/ folder.
_DATASET_MD_CHARS = 8_000


def _dt_now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
_SKILL_CODE_CHARS = 16_000


def _clip_skill_text(text: str, limit: int, name: str, file: "str | None") -> str:
    if len(text) <= limit:
        return text
    where = f"read_skill_reference(name={name!r}" + (f", file={file!r}" if file else "") + ")"
    return text[:limit] + f"\n[... cut at {limit:,} characters; the rest is at {where}]"


def _assigned_skill_block(skill: "Skill") -> str:
    """The brief's part for a step the plan assigned ``skill`` to: what to do with it, then its
    SKILL.md and its code, so the Scientist starts from the vetted method instead of rewriting it."""
    where = (f"registry release {skill.registry_release}" if skill.registry_release
             else "a skill learned by this deployment")
    run = ("run its commands with run_in_environment" if skill.image
           else "run the code with run_code")
    out = [f"The approved plan assigns the skill `{skill.name}` (version {skill.version or 'local'}, "
           f"{where}) to this step. Its guidance and code are below: carry out the step WITH this "
           "skill. Adapt only what it marks for adaptation (this dataset's paths, columns, groups and "
           f"thresholds) and {run}. Do not write the analysis from scratch. If part of the skill does "
           "not fit this data, change only that part and say in your answer what you changed and why."]
    doc = (skill.doc or "").strip()
    if doc:
        out.append(f"--- {skill.name}: SKILL.md ---\n" + _clip_skill_text(doc, _SKILL_DOC_CHARS, skill.name, None))
    first = default_file(skill.files)
    if first:
        out.append(f"--- {skill.name}: {first} ---\n"
                   + _clip_skill_text(skill.files.get(first) or "", _SKILL_CODE_CHARS, skill.name, first))
        others = [f for f in sorted(skill.files) if f != first and not f.startswith("tests/")]
        if others:
            out.append("The skill's other files (read one with `read_skill_reference(name, file=...)`): "
                       + ", ".join(others[:20]))
    if skill.outputs:
        out.append("The skill declares the outputs this step must leave under $AISCIENTIST_ARTIFACTS "
                   "(a * stands for this dataset's names). After the step the platform checks that "
                   "every required one exists, is not empty and, for a figure, is not blank; a step "
                   "without them is not accepted:\n" + skill_outputs.brief_lines(skill.outputs))
    return "\n\n".join(out)


def _skill_name(name: str) -> "str | None":
    """The library's own name for ``name`` (an old name of a migrated skill resolves), or ``None``."""
    found = get_skill(str(name or "").strip())
    return found.name if found is not None else None


#: Turns added to a RETRY whose previous attempt ran out of them. Re-running a heavy step under
#: the cap that already proved too small reaches the same wall: run ed4cfce52a2a spent rounds 14
#: AND 15 on one step, both ending on ``max_steps``, both marked revise. Escalation applies only
#: to the budget stop — more turns cannot help a model stuck on repeated errors.
RETRY_TURN_BONUS = 4


def _budget_bonus(previous: "HarnessResult | None", attempts: int) -> int:
    """Extra Scientist turns for this attempt, from how the PREVIOUS attempt at the same step
    ended. Zero for a first attempt, and zero for any stop reason other than exhausting the
    budget — a revise for wrong content is not a request for more room."""
    if previous is None or attempts <= 0:
        return 0
    if getattr(previous, "stop_reason", None) != "max_steps":
        return 0
    return RETRY_TURN_BONUS * attempts


class _RunCancelled(BaseException):
    """Unwind cancellation past best-effort model/tool fallbacks."""


class ResearchLab:
    """PI → Scientist → Critic loop. Roles are injectable for offline tests."""

    def __init__(
        self,
        ctx: HarnessContext,
        config: LabConfig | None = None,
        *,
        complete_fn: LabRoleFn | None = None,
        scientist: ResearchHarness | None = None,
        code_executor: CodeExecutor | None = None,
    ) -> None:
        self.ctx = ctx
        from ..skills_registry import init_manifest
        init_manifest(ctx)
        self.config = config or LabConfig()
        self._complete_fn = complete_fn
        # Scientist = the tool-calling harness with the FULL registry catalog (curated
        # tools + analysis line + schematic + run_code CodeAct). Tests inject their own.
        from ..registry import build_scientist_catalog
        if scientist is not None:
            self.scientist = scientist
        else:
            catalog = build_scientist_catalog(code_executor=code_executor)
            self.scientist = ResearchHarness(catalog=catalog, config=self.config.scientist)
        # Progressive disclosure: the atomic-skill library (``agents/skills.py``). Two small always-on
        # tools — search_skills (find relevant skills by keyword) + read_skill_reference (guidance on
        # the first call, code on `file=`) — attached to WHATEVER scientist we use (built here OR
        # injected by the gateway) so the fixed registry stays the small always-on core and a skill's
        # guidance/code enter context only when used. MUST run for the injected case too: the gateway
        # builds the catalog WITHOUT these, and the
        # brief tells the model to call them — without this attach they'd be 'unknown tool' in production.
        # `read_tool_source` is attached the same way and for the same reason as the skill tools:
        # the gateway builds the catalog without it, and a brief that tells the model to read an
        # implementation is worthless if the tool is 'unknown tool' at call time. It closes the
        # asymmetry that let run_de's 50-gene cap, run_enrichment's constant background and
        # resolution=1.0 all survive — a tool's DESCRIPTION states intent, its SOURCE states
        # behaviour, and until now the model could only ever see the first.
        self.scientist.add_tools(make_search_skills_tool(), make_skill_reference_tool(),
                                 make_tool_source_tool(lambda: list(self.scientist.catalog)))
        # Effective research-path guidance. An explicit preset (user override) seeds it; if
        # absent, the PI auto-selects a skill in ``run()`` (Axis B). Resolved once and reused
        # across re-plans (plan-mode revisions call ``_pi_plan`` again).
        self._guidance: str | None = self.config.preset_prompt
        # The PI's own-knowledge disclosure for the current plan (``_split_self_sourced``), and the
        # preset pipelines that steered it. Both are read back out by the gateway when it writes
        # plan.md / run_state.json, so a run read months later can answer "which protocol was this,
        # and which of its choices did the protocol not actually specify".
        self._self_sourced: str = ""
        # The plan document (config.plan_yaml): the one under review, and the one that was approved.
        # Steps are pinned to the approved one and the finished run is checked against it.
        self._plan_doc: "PlanDoc | None" = None
        self._approved_plan: "PlanDoc | None" = None
        self._plan_feedback: list[str] = []
        self._plan_adherence: dict[str, Any] = {}
        # Steps the researcher said yes to DURING the run (``_approve_new_step``). They are not in
        # the approved document, and the read-back must not report them as unapproved work.
        self._approved_additions: list[str] = []
        # The plan's write_report step(s), kept out of the analysis and handed to the gateway (#102).
        self._report_requested = False
        self._report_steps: list[str] = []
        # When each step's first attempt began, by its plan text: the check of its declared outputs
        # counts only files written since then as the step's own (skill_outputs.check).
        self._step_started: dict[str, float] = {}
        # What the user limited this run to ("only QC", "no DE"): read once in _run_inner, enforced
        # wherever a step can enter the plan (agents/scope.py).
        self._scope: RunScope = RunScope()
        # How the PI read the request (``"whole"`` analysis / ``"specific"`` analyses / ``""`` when
        # it did not say), and the analyses it left out for a later message (``_parse_plan_extras``).
        self._request_breadth: str = ""
        self._later: list[str] = []
        # Asking the user mid-run (the gateway's decision card), and the steps proposed after a
        # result that were not added: declined, unanswered, or with nobody to ask.
        self._decision_review: "Callable[[Any], dict[str, Any]] | None" = None
        self._proposed_steps: list[dict[str, Any]] = []
        # Skills induced at the end of this run (``_induce_skills``), for its skill request.
        self.induced_skills: list[str] = []
        self._step_asks_unanswered = False
        # The steps the user skipped at a failed step's decision point (``LabResult.skipped_steps``).
        self._skipped_steps: list[str] = []
        # The skills steering THIS run: the user-PINNED ones (mandatory) + one the PI auto-selects on
        # top (Axis B), extended in ``run()``. Carry their reference-code templates (progressive
        # disclosure). Empty = free planning.
        self._skills: "list[PresetPipeline]" = list(self.config.pinned_skills)
        # Axis A — the resolved execution mode + the team the PI formed (team mode only).
        self._mode: str = self.config.mode
        self._team: tuple[Specialist, ...] | None = None
        # Axis C — per-agent evolving memory (docs/agent_memory_design.md). Built only when enabled
        # AND a persistent root is set; rooted OUTSIDE the per-run workspace so it survives across runs.
        self._agent_memory = None
        if self.config.agent_memory and self.config.agent_memory_dir:
            from ..agent_memory import AgentMemory
            self._agent_memory = AgentMemory(self.config.agent_memory_dir)
        # Hypothesis-driven exploration state (config.hypothesis_driven). The ledger is this run's
        # generated research paths; the counter enforces ``max_new_steps`` across BOTH planners.
        # Mutated only from the single-threaded scheduler merge, never from a concurrent node.
        self._ledger = HypothesisLedger()
        self._claim_audit: dict[str, Any] = {}   # set by _synthesize, stamped on the result by run()
        # Papers the meeting experts looked up while designing the study. Written from the expert
        # thread pool, hence the lock; read once at the end of ``run``.
        self._design_citations: list[dict[str, Any]] = []
        self._design_lock = threading.Lock()
        self._new_steps_added = 0
        # Run-scope context state: which accepted rounds have been folded into a digest, and the
        # digest block that stands in for them in every later brief.
        self._compacted_indices: set[int] = set()
        self._compacted_block: str = ""
        # Mid-run durability (see :meth:`_checkpoint_state`). Set per-run by ``run()``.
        self._checkpoint: "Callable[[LabResult], None] | None" = None

    @property
    def plan_context(self) -> "dict[str, Any]":
        """Resolved planning inputs saved alongside a waiting plan, including its full team."""
        return {
            "guidance": self._guidance, "scope": self._scope.to_dict(),
            "skills": [asdict(s) for s in self._skills], "mode": self._mode,
            "team": [asdict(s) for s in self._team] if self._team else None,
            "self_sourced": self._self_sourced, "later": list(self._later),
            "request_breadth": self._request_breadth, "feedback": list(self._plan_feedback),
            "design_background": list(self._design_citations),
        }

    def _restore_plan_context(self, state: "dict[str, Any]") -> None:
        self._guidance = state.get("guidance", self._guidance)
        self._scope = RunScope.from_dict(state.get("scope", {}))
        if "skills" in state:
            # A pipeline's declared outputs come back as DeclaredOutput, which the output checks
            # read (``_expected_outputs``); JSON gave them back as dicts.
            self._skills = [_from_fields(PresetPipeline, {
                **s, "tools": tuple(s.get("tools", ())),
                "outputs": tuple(_from_fields(skill_outputs.DeclaredOutput, o)
                                 for o in s.get("outputs") or () if isinstance(o, dict))})
                            for s in state["skills"]]
        self._mode = str(state.get("mode") or self.config.mode)
        self._team = tuple(_from_fields(Specialist, {**s, "keywords": tuple(s.get("keywords", ()))})
                           for s in state.get("team") or []) or None
        self._self_sourced = str(state.get("self_sourced") or "")
        self._later = list(state.get("later") or [])
        self._request_breadth = str(state.get("request_breadth") or "")
        self._plan_feedback = list(state.get("feedback") or [])
        self._design_citations = list(state.get("design_background") or [])

    @property
    def approved_plan_yaml(self) -> str:
        """The approved plan as YAML text ("" when ``config.plan_yaml`` is off or nothing was
        approved). Read by the gateway, which writes it beside plan.md."""
        return self._approved_plan.to_yaml() if self._approved_plan is not None else ""

    @property
    def plan_adherence(self) -> "dict[str, Any]":
        """The finished run read back against the approved plan (``plan_doc.check_adherence``)."""
        return dict(self._plan_adherence)

    @property
    def guidance(self) -> str | None:
        """The resolved research-path steering (an explicit preset, or the skill the PI selected).
        Exposed so the gateway can persist it into run_state for an A2 resume that stays on the same
        protocol. Meaningful only after :meth:`run` has resolved it."""
        return self._guidance

    @property
    def protocols(self) -> "list[dict[str, str]]":
        """The preset pipeline(s) that steered this plan — key + human label, in load order.

        The auto-router already announced these in the live feed, but that line is ephemeral: the
        run bundle recorded the guidance TEXT and never the protocol's identity, so a result read
        later could not say which protocol it followed, nor whether the plan followed it. Empty
        when the run planned from scratch — which is itself worth recording."""
        return [{"key": s.key, "label": s.label} for s in getattr(self, "_skills", []) or []]

    @property
    def scope(self) -> RunScope:
        """The limit the user's request put on this run (empty when it set none)."""
        return self._scope

    @property
    def request_breadth(self) -> str:
        """``"whole"`` / ``"specific"`` as the PI read the request, ``""`` when it did not say."""
        return self._request_breadth

    @property
    def later(self) -> "list[str]":
        """Analyses the PI left out because the request did not ask for them — what the user can
        ask for next in the same conversation."""
        return list(self._later)

    @property
    def proposed_steps(self) -> "list[dict[str, Any]]":
        """Steps a result prompted that were NOT added to the run (``step``, ``why``, ``decision``:
        declined / unanswered / not_asked), for the user to ask for afterwards."""
        return [dict(p) for p in self._proposed_steps]

    @property
    def self_sourced(self) -> str:
        """What the PI decided from its OWN knowledge rather than from the guidance or the dataset
        profile (see ``_split_self_sourced``). ``''`` when it disclosed nothing."""
        return self._self_sourced

    @property
    def report_requested(self) -> bool:
        """The approved plan has a ``write_report`` step: the gateway writes the report after the
        analysis (#102). ``False``: the run ends with its answer in the chat."""
        return self._report_requested

    @property
    def execution_profile(self) -> "dict[str, Any]":
        """HOW this run was executed — the resolved mode, the team, the planner and the switches.

        Persisted with every result because it was previously unrecoverable. Of 26 production run
        bundles inspected on 2026-08-11, NONE recorded the mode, and the only way to tell a
        Virtual-Lab run from a single-scientist one was to guess from the specialist NAMES in the
        rounds (a PI-formed team invents its own titles; single mode uses the fixed roster). That
        guess is one-directional: custom titles prove a team, but the fixed roster does NOT prove
        single, because a team run whose roster failed to parse falls back to exactly that roster.

        ``mode`` is the RESOLVED value, not the requested one — "auto" is what the user picks and
        tells you nothing about what actually ran."""
        return {
            "mode": self._mode,                                   # resolved: "single" | "team"
            "mode_requested": self.config.mode,                    # what was asked for ("auto"?)
            "team": [e.name for e in self._team] if self._team else None,
            "team_is_pi_formed": bool(self._team) and self._team != self.config.specialists,
            "planner": self.config.planner,
            "multi_agent": self.config.multi_agent,
            "multi_specialist": self.config.multi_specialist,
            "max_concurrency": self.config.max_concurrency,
            "agent_memory": bool(self.config.agent_memory and self.config.agent_memory_dir),
            "hypothesis_driven": self.config.hypothesis_driven,
            "max_cycles": self.config.max_cycles,
            "step_meetings": self.config.step_meetings,
        }

    # -- orchestration (the explicit state machine; maps to a LangGraph) -------

    def _checkpoint_state(self, question: str, agenda: list[str], rounds: "list[LabRound]",
                          accepted: int) -> None:
        """Persist the run's orchestration state MID-RUN — after every round, not only at the end.

        Durability used to be all-or-nothing: ``run_state.json`` was written once, after the whole
        run finished. A gateway restart or a crash during a multi-hour analysis therefore lost the
        orchestration state entirely — the on-disk analysis checkpoints (``work/adata_*.h5ad``)
        survived, but nothing could re-enter the loop and use them, so the run was simply gone.
        Writing after each round makes an interrupted run resumable from its last accepted step,
        which is the durability a graph checkpointer would provide, without the dependency.

        ``converged=False`` and an empty ``final_answer`` mark it as a run still in flight; a
        completed run overwrites this with the real result. Best-effort by construction: a failed
        save must never break the run it is recording."""
        if self._checkpoint is None:
            return
        try:
            state = LabResult(question, list(agenda), list(rounds), False, accepted, "",
                              self._ledger.to_list())
            state.report_requested = self._report_requested
            self._checkpoint(state)
        except Exception as exc:  # noqa: BLE001 - durability metadata must never fail a run
            print(f"[lab] mid-run checkpoint failed: {exc}")

    def run(self, *args: Any, **kwargs: Any) -> LabResult:
        """Run the lab, then stamp on the papers the experts consulted while DESIGNING the study.

        Twelve code paths construct a ``LabResult``; stamping here rather than at each of them is
        what keeps the design background from being present on some exits and missing on others.
        Mid-run ``checkpoint`` snapshots deliberately go without it — it is a property of the
        finished run, and the meeting that produced it may not have happened yet.
        """
        self._should_cancel = kwargs.get("should_cancel") or (args[3] if len(args) > 3 else None)
        self._planning = True
        try:
            self._check_cancelled()
            result = self._run_inner(*args, **kwargs)
        except _RunCancelled:
            question = args[0] if args else kwargs["question"]
            emit = kwargs.get("on_event") or (args[1] if len(args) > 1 else None)
            if emit is not None:
                emit({"type": "run_cancelled", "completed_steps": 0, "agenda": 0})
            result = LabResult(question, [], [], False, 0,
                               "Stopped by the user during planning.")
        finally:
            self._planning = False
        if isinstance(result, LabResult) and not result.design_background:
            with self._design_lock:
                result.design_background = list(self._design_citations)
        if isinstance(result, LabResult) and not result.claim_audit and self._claim_audit:
            result.claim_audit = dict(self._claim_audit)
        if isinstance(result, LabResult) and not result.skipped_steps and self._skipped_steps:
            result.skipped_steps = list(dict.fromkeys(self._skipped_steps))
        if isinstance(result, LabResult) and self._report_requested:
            result.report_requested = True
        if isinstance(result, LabResult) and self._approved_plan is not None and result.rounds:
            # Compared here, on the one exit every path shares, for the same reason as above. A run
            # with no rounds (cancelled at review) has nothing to compare. The report step is the
            # gateway's, after the analysis: it is handed over, not skipped.
            self._plan_adherence = check_adherence(self._approved_plan, [
                {"step": r.step, "accepted": r.verdict.verdict == "accept",
                 "calls": [{"tool": c.get("tool"), "args": c.get("args") or {},
                            "ok": step_succeeded(c)}
                           for c in (r.scientist_result.get("steps") or []) if isinstance(c, dict)]}
                for r in result.rounds] + [
                {"step": s, "accepted": True, "calls": [{"tool": REPORT_TOOL, "args": {}, "ok": True}]}
                for s in self._report_steps], approved_additions=self._approved_additions)
            on_event = kwargs.get("on_event") or (args[1] if len(args) > 1 else None)
            if on_event is not None:
                on_event({"type": "plan_adherence", **self._plan_adherence})
        return result

    def _run_inner(
        self,
        question: str,
        on_event: EventFn | None = None,
        plan_review: "Callable[[str, Any], dict[str, Any]] | None" = None,
        should_cancel: "Callable[[], bool] | None" = None,
        pull_injections: "Callable[[], list[str]] | None" = None,
        resume: "ResumeState | None" = None,
        decision_review: "Callable[[Any], dict[str, Any]] | None" = None,
        should_compact: "Callable[[], bool] | None" = None,
        checkpoint: "Callable[[LabResult], None] | None" = None,
        saved_plan: "PlanDoc | None" = None,
        approved_plan: "PlanDoc | None" = None,
        plan_context: "dict[str, Any] | None" = None,
        saved_plan_decision: "dict[str, Any] | None" = None,
        saved_plan_kind: str = "agenda",
        saved_plan_payload: Any = None,
    ) -> LabResult:
        emit: EventFn = on_event or (lambda _e: None)
        self._checkpoint = checkpoint
        self._decision_review = decision_review
        self._proposed_steps, self._step_asks_unanswered = [], False
        reviewing_resume = resume is not None and resume.needs_plan_review
        if reviewing_resume and approved_plan is None and saved_plan is None:
            saved_plan = resume.approved_plan
            plan_context = resume.plan_context
            if saved_plan_decision is None and not resume.status_recovery:
                index = resume.from_step_index
                change = (f"Change only step {index + 1}. " + resume.modify_note
                          + f"\nRequested step text: {resume.agenda[index]}\n"
                          + "Keep every other step and its settings unchanged. Present the changed "
                            "tool and settings for human review before any execution.")
                saved_plan_decision = {"action": "revise", "feedback": change}
        restored = approved_plan is not None or saved_plan is not None or saved_plan_kind != "agenda"
        if saved_plan is not None and approved_plan is not None:
            raise ValueError("Provide saved_plan or approved_plan, not both")
        if resume is not None and restored and not reviewing_resume:
            raise ValueError("A saved plan cannot also resume completed rounds")
        if reviewing_resume and approved_plan is None and plan_review is None:
            from ..mcp_plan_migration import PlanMigrationRequired
            raise PlanMigrationRequired("This changed saved plan needs the normal plan review before execution.")
        self._skipped_steps = []

        def check_restored(agenda: list[str]) -> None:
            from ..mcp_plan_migration import assert_plan_available
            indices = None
            if reviewing_resume:
                done = _rounds_by_agenda_index(agenda, resume.prior_rounds)
                indices = (set(resume.redo_indices) if resume.redo_indices is not None
                           else set(range(resume.from_step_index, len(agenda))))
                indices |= {i for i in range(len(agenda)) if i not in done}
            assert_plan_available(agenda, self.scientist.catalog,
                                  plan=self._approved_plan or self._plan_doc, indices=indices)

        # A2 continuation: re-enter the SAME state machine without re-planning. Skip skill/mode/PI
        # planning, reuse the prior agenda + accepted rounds, and re-execute from the changed step
        # onward. The kept steps' analysis checkpoints (work/adata_*.h5ad) are reused off disk.
        if resume is not None and not reviewing_resume:
            from ..mcp_plan_migration import assert_plan_available, assert_resume_document

            self._planning = False
            if resume.plan_context is not None:
                self._restore_plan_context(resume.plan_context)
            if resume.approved_plan is not None:
                self.config = replace(self.config, plan_yaml=True)
                self._plan_doc = self._approved_plan = resume.approved_plan
            if resume.guidance is not None:
                self._guidance = resume.guidance
            if resume.self_sourced and not self._self_sourced:
                self._self_sourced = resume.self_sourced     # the plan is not redrafted, so keep its record
            # The plan's report step was kept out of the agenda the first time; its request is not.
            self._report_requested = self._report_requested or resume.report_requested
            agenda = [s for s in resume.agenda if not _is_report_step(s)]
            k = max(0, min(resume.from_step_index, len(agenda) - 1))
            # Decide which downstream steps actually depend on the change and must be re-run; the
            # rest (e.g. an independent literature step) are reused. Explicit set wins; else evaluate.
            redo = set(resume.redo_indices) | {k} if resume.redo_indices is not None \
                else self._evaluate_redo_indices(agenda, k, resume.modify_note or resume.reviewed_note,
                                                resume.prior_rounds, emit)
            # Reuse the prior accepted round for every step NOT being re-run (by 0-based index); any
            # step without a reusable round falls into the re-run set so nothing is silently skipped.
            kept_by_index = {i: r for i, r in _rounds_by_agenda_index(agenda, resume.prior_rounds).items()
                             if i not in redo}
            redo_indices = frozenset(i for i in range(len(agenda))
                                     if i in redo or i not in kept_by_index)
            assert_plan_available(agenda, self.scientist.catalog, plan=resume.approved_plan,
                                  indices=redo_indices)
            assert_resume_document(agenda, resume.approved_plan, redo_indices, resume.modify_note)
            emit({"type": "run_resumed", "agenda": len(agenda), "from_step": k + 1,
                  "redo": sorted(i + 1 for i in redo_indices),
                  "kept": sorted(i + 1 for i in kept_by_index)})
            return self._run_loop(
                question, agenda, emit, should_cancel, pull_injections,
                exec_roster=self.config.specialists, exec_multi=self.config.multi_specialist,
                redo_indices=redo_indices, kept_by_index=kept_by_index,
                seed_notes=resume.modify_note or resume.reviewed_note,
                decision_review=decision_review, should_compact=should_compact)

        self._human_failure_asks = 0
        def _cancelled_before_plan() -> "LabResult | None":
            if should_cancel is not None and should_cancel():
                emit({"type": "run_cancelled", "completed_steps": 0, "agenda": 0})
                return LabResult(question, [], [], False, 0,
                                 "Stopped by the user before the plan was drafted — no tools ran.")
            return None

        if restored:
            if plan_context is not None:
                self._restore_plan_context(plan_context)
            self.config = replace(self.config, plan_yaml=True)
            self._plan_doc = approved_plan if approved_plan is not None else saved_plan
            kind = "agenda" if approved_plan is not None else saved_plan_kind
            payload = (self._plan_doc.agenda() if kind == "agenda" and self._plan_doc is not None
                       else saved_plan_payload)
            exec_roster = self._team or self.config.specialists
            exec_multi = True if self._mode == "team" else self.config.multi_specialist
            if approved_plan is not None:
                check_restored(list(payload))
                self._approve_plan_document(emit)
                plan_review = None
        else:
            self._plan_feedback = []
            # Axis B — PI-autonomous skill selection: if the user did not force a research path,
            # the PI reads the skill library and picks one itself (researchers do not know which
            # protocol they need). The chosen skill's body then STEERS planning, exactly like a
            # user-picked preset. An explicit preset_prompt (already in self._guidance) wins.
            # Axis B. ``self._skills`` already holds the user-PINNED skills (the multi-select — mandatory).
            # The PI ALSO auto-selects the best-fit skill — from the DATASET profile as well as the
            # question, so a VCF / annotated .h5ad routes correctly even from a vague ask — and it AUGMENTS
            # the pinned set (never replaces it), deduped by key. A user-EDITED free-text override
            # (``self._guidance`` from ``preset_prompt``) turns auto OFF — "I'm taking over the guidance";
            # pinned skills do NOT (they leave ``self._guidance`` None), so they still get the auto pick.
            # The dataset's description (DATASET.md): read when it is kept, written by the PI the first
            # time a run reads the dataset. Before the protocol is chosen and the plan drafted, so both
            # read it (RCHENLAB/AiScientist issue 10, part 3).
            self._describe_dataset(emit)
            # The limit the request itself sets ("only QC", "no DE, no enrichment", "a short report"),
            # read before anything that could widen the plan.
            self._scope = read_scope(
                question, {t.name: t.description for t in self.scientist.catalog},
                lambda m: self._complete(m, role="classify"))
            if self._scope.limited or self._scope.brief_report:
                emit({"type": "scope", **self._scope.to_dict(), "text": self._scope.describe()})
            # A request limited to named analyses is planned from the data profile alone: a matched
            # protocol exists to lay out the WHOLE pipeline, which is what the user just declined.
            if self._guidance is None and self.config.auto_select_skill and not self._scope.keep:
                # Feature ② Phase B: the run-start content triage (peek/describe) stamped the PRIMARY
                # file's modality into decisions; hand it to the router so a file's actual CONTENT — not
                # its extension — picks the modality bucket. Absent/low-confidence → the router falls back
                # to the suffix-derived dataset profile (self._dataset_context()).
                _dec = self.ctx.decisions or {}
                chosen = select_pipeline(self._complete, question, self._dataset_context(),
                                         self.config.skill_library, emit,
                                         content_modality=str(_dec.get("content_modality") or ""),
                                         content_confidence=str(_dec.get("content_confidence") or ""),
                                         available_tools=self._usable_tool_names())
                if chosen is not None:
                    # The auto pick is DATASET-derived (select_pipeline reads the dataset profile), so it
                    # is the ground truth on modality. When it names a different ``data_type`` than a
                    # user-PINNED pipeline (e.g. the dataset is a VCF → variant_annotation, but a
                    # single-cell pipeline was pinned from the chat), the dataset wins: drop the
                    # conflicting pinned pipelines so a scanpy protocol is never forced onto a VCF (its
                    # QC→clustering steps would be composed into the plan alongside the variant workflow).
                    kept, dropped = drop_conflicting_pinned(self._skills, chosen)
                    if dropped:
                        self._skills = kept
                        emit({"type": "skills_dropped",
                              "skills": [s.key for s in dropped],
                              "kept": chosen.key,
                              "reason": f"dataset routed to '{chosen.data_type}'"})
                    if chosen.key not in {s.key for s in self._skills}:
                        self._skills.append(chosen)
            # Guidance = the (optional) user-edited free-text override first, then every loaded skill's
            # prompt (pinned + auto). ``self._guidance`` was seeded with the override in __init__.
            skill_text = compose_pipeline_prompts(self._skills)
            if skill_text:
                self._guidance = ((self._guidance + "\n\n") if self._guidance else "") + skill_text
            # User-REQUIRED atomic skills (the console's skill multi-select): the plan MUST apply each of
            # these specific capabilities. Unlike the global manifest (available on demand), these are
            # mandatory, so the directive goes into the PI's planning guidance. Validated against the
            # loaded atomic library; unknown names are dropped.
            _reqs = [s for n in self.config.required_skills if (s := get_skill(n, ATOMIC_SKILLS))]
            if _reqs:
                block = ("REQUIRED skills for this study — the plan MUST apply EACH of these (read its "
                         "guidance with read_skill_reference(name), fetch the code with "
                         "read_skill_reference(name, file=\"reference.py\"), adapt it to the dataset, and run "
                         "it via run_code):\n"
                         + "\n".join(f"- {s.name}" + (f" — {s.summary}" if s.summary else "") for s in _reqs))
                self._guidance = ((self._guidance + "\n\n") if self._guidance else "") + block
                emit({"type": "skills_required",
                      "skills": [{"name": s.name, "summary": s.summary} for s in _reqs]})
            # Announce the active research paths NOW — BEFORE the plan is drafted/reviewed — so the user
            # sees which pipelines (pinned + auto) are steering the plan, not only after they approve it.
            # Each pipeline reports the tools it composes AND the ones this deployment cannot run, so
            # "Loaded preset pipeline: scGPT foundation-model annotation" can never again be announced
            # for a protocol whose defining tool is dead here — the user learns that at load time, not
            # from an empty section in the final report.
            _usable = self._usable_tool_names()
            emit({"type": "skills_loaded",
                  "skills": [{"key": s.key, "label": s.label, "tools": list(s.tools),
                              **({"unavailable_tools": list(gaps)}
                                 if (gaps := s.missing_tools(_usable)) else {})}
                             for s in self._skills]})

            # Axis A — execution mode (Virtual-Lab team vs single scientist). "auto" lets the PI
            # route; the frontend mode toggle sets "team"/"single" explicitly. In team mode the
            # PI forms a team and the team DESIGNS the approach in a meeting (each expert keeps an
            # independent context) — its synthesis augments the planning guidance below.
            # Per-RUN, not per-object: a ResearchLab instance can serve more than one run, and a
            # spent budget carried into the next one would silently stop asking from the first
            # failure — the opposite of the guarantee.
            self._human_failure_asks = 0

            if (self._scope.keep or self.config.prior_work) and self.config.mode == "auto":
                # The design meeting designs a study. A request that names the only analyses it wants,
                # or one that adds a step to work this conversation already did, has no study to design.
                self._mode = "single"
                emit({"type": "mode_selected", "mode": "single",
                      "reason": "scope" if self._scope.keep else "continuation"})
            else:
                self._mode = (self._route_mode(question, emit) if self.config.mode == "auto"
                              else self.config.mode)
            exec_roster, exec_multi = self.config.specialists, self.config.multi_specialist
            if self._mode == "team":
                self._team = self._form_team(question, emit)
                exec_roster, exec_multi = self._team, True
                design = self._team_meeting(
                    question,
                    "How should we carry out what this request asks for? Propose how to do the "
                    "requested analysis on THIS data, the key pitfalls to avoid, and how to validate "
                    "the result. Stay inside the request: an analysis the user did not ask for is a "
                    "suggestion for a later message, not part of this plan.",
                    self._team, "design", emit)
                self._guidance = ((self._guidance + "\n\n") if self._guidance else "") + \
                    ("Team design-meeting synthesis (how to carry out the request — it does not "
                     "widen it):\n") + design

            # PI node. In plan mode the PI may ask a clarifying question first; without it,
            # force a straight agenda (there is no one to answer a clarify).
            if (early := _cancelled_before_plan()) is not None:
                return early
            # In plan mode the reviewed agenda is presented below; streaming the draft as well
            # repeats the same plan card when review leaves it unchanged.
            def draft_event(ev: dict[str, Any]) -> None:
                if plan_review is None or ev.get("type") != "pi_agenda":
                    emit(ev)

            kind, payload = self._pi_plan(question, draft_event, allow_clarify=plan_review is not None)
            if kind == "failed":
                emit({"type": "plan_failed", "reason": payload})
                return LabResult(question, [], [], False, 0, str(payload))
            # PI<->Critic review runs BEFORE the plan reaches the human, not instead of it. The old
            # condition skipped the review whenever a human was curating ("their decision wins") —
            # and "Plan first" is checked by default, so on the path almost every real run takes, the
            # only thing that ever read a plan back was switched off. A human reviewing a plan that
            # has already been critiqued still decides everything; they just decide with the Critic's
            # objections in front of them instead of without them.
            if plan_review is not None and kind == "agenda":
                payload = self._keep_report_step(list(payload), self._apply_scope(
                    self._plan_review(question, list(payload), emit), emit))
                emit({"type": "pi_agenda", "agenda": payload})

        # Plan mode (human-in-the-loop): the user does NOT text-edit the plan — they
        # review it and reply in natural language; that feedback goes BACK to the PI,
        # which re-drafts. The loop runs until the user approves an agenda or cancels.
        # ``plan_review(kind, payload)`` blocks for the user and returns a decision dict:
        #   {"action": "approve"}                 -> run the current agenda
        #   {"action": "revise", "feedback": ...} -> re-plan with the user's notes
        #   {"action": "cancel"}                  -> abort, run nothing
        if plan_review is not None:
            feedback_log = self._plan_feedback
            reported_tooling: set[str] = set()
            pending_cutoffs: "tuple[str, dict[str, Any]] | None" = None
            while True:
                # A Stop clicked while the PI was re-drafting (the previous loop turn's model
                # call) must land here, before another card is pushed at the user.
                if (early := _cancelled_before_plan()) is not None:
                    emit({"type": "plan_cancelled"})
                    return early
                # Tool names + arguments, checked while approving is still a CHOICE. This used to
                # run after approval, where saying "that tool does not exist" costs the reviewer
                # the decision they had already made. Inside the loop, so a revision that
                # introduces a bad name is caught on the same terms as the first draft.
                if kind == "agenda" and saved_plan_decision is None:
                    if restored:
                        check_restored(list(payload))
                    payload = self._check_plan_tooling(list(payload), emit, reported_tooling)
                    self._present_plan_document(question, list(payload), emit)
                    if pending_cutoffs is not None:
                        # The last reply was revised from its words (patch or redraft); the cutoffs
                        # it named are now set on that plan by code (issue 61).
                        said, cutoffs = pending_cutoffs
                        ops = self._cutoff_ops(cutoffs)
                        pinned = self._revise_plan_document(question, said, emit, ops=ops) if ops else None
                        if pinned is not None:
                            payload = pinned
                            emit({"type": "pi_agenda", "agenda": payload})
                            self._present_plan_document(question, list(payload), emit)
                pending_cutoffs = None
                if saved_plan_decision is not None:
                    decision, saved_plan_decision = saved_plan_decision, None
                else:
                    decision = plan_review(kind, payload) or {"action": "cancel"}
                action = str(decision.get("action", "cancel")).lower()
                if action == "approve" and kind == "agenda":
                    self._approve_plan_document(emit)
                    break
                # The researcher edited the plan DOCUMENT. Parsed, checked and applied by code: no
                # model reads it, so a step they did not touch cannot change, and an edit that
                # cannot be applied changes nothing and says why.
                if action == "edit" and kind == "agenda":
                    applied = self._apply_plan_edit(question, str(decision.get("plan_yaml") or ""), emit)
                    if applied is not None:
                        payload, changes = applied
                        feedback_log.append(_hand_edit_note(changes))
                    emit({"type": "pi_agenda", "agenda": payload})
                    continue
                # A reply in words sent while the researcher's own edit of the document was not
                # applied yet: the edit is applied first, by code, and the words then change THAT
                # plan, never the one the edit was typed on. An edit that cannot be applied stops
                # both, and the plan is re-presented as it was.
                draft = str(decision.get("plan_yaml") or "") if action == "revise" and kind == "agenda" else ""
                if draft.strip():
                    applied = self._apply_plan_edit(question, draft, emit, with_reply=True)
                    if applied is None:
                        emit({"type": "pi_agenda", "agenda": payload})
                        continue
                    payload, changes = applied
                    if changes:
                        feedback_log.append(_hand_edit_note(changes))
                if action in ("cancel", "timeout"):
                    # A review that EXPIRED and a review somebody CANCELLED end the same way, but
                    # they are not the same event and must not carry the same sentence. Both of the
                    # English end-of-run messages used to say "by the user" — so a reviewer who had
                    # clicked nothing was told they had stopped their own run, and had no way to
                    # tell a system limit from their own mistake (Ziyao, plan_mode_report_v2_5, C-3:
                    # the ONLY accurate wording in the console was the Chinese one).
                    timed_out = action == "timeout"
                    emit({"type": "plan_cancelled", "reason": "timeout" if timed_out else "user"})
                    prior = payload if kind == "agenda" else []
                    return LabResult(
                        question, prior, [], False, 0,
                        ("The plan review window expired with no reply — nothing was executed. "
                         "Nobody cancelled this run; re-send the question to plan it again."
                         if timed_out else
                         "Run cancelled by the user during plan review — no tools were executed."))
                # revise / answering a clarify -> re-plan with the accumulated feedback
                fb = str(decision.get("feedback", "")).strip()
                # Nothing was asked for. Redrafting on "hmm" is pure downside: the plan can only
                # come back damaged, and once did — a significance threshold moved on its own.
                if kind == "agenda" and is_noise_reply(fb):
                    emit({"type": "plan_no_request", "text": fb[:120]})
                    emit({"type": "pi_agenda", "agenda": payload})
                    continue
                # A QUESTION is not a revision. Everything a reviewer typed used to become one, so
                # "why is step 3 a Wilcoxon test?" did not get an answer — it triggered a redraft,
                # and a redraft damages the steps nobody mentioned. Answer it and hand back the
                # SAME plan, unchanged, so asking costs nothing.
                if kind == "agenda" and fb and classify_plan_reply(fb) == "question":
                    emit({"type": "plan_question", "text": fb})
                    answer = self._answer_plan_question(question, list(payload), fb)
                    emit({"type": "plan_answer", "question": fb, "answer": answer})
                    emit({"type": "pi_agenda", "agenda": payload})   # re-present it untouched
                    continue
                # A cutoff named with a number ("raise the mitochondrial limit to 20") is read by code
                # and set on every step whose tool takes it, so the number typed is the number that
                # runs; one that cannot be used is explained back, and the plan is re-presented as it
                # was (issue 61). A reply that asks for something else too goes to the reviser with
                # the cutoffs pinned, so the rest of it is not lost.
                cutoffs: "dict[str, Any]" = {}
                cutoffs_only = False
                if kind == "agenda" and fb:
                    cutoffs, cutoff_problems, cutoffs_only = self._cutoff_request(fb)
                    if cutoff_problems:
                        emit({"type": "plan_answer", "question": fb,
                              "answer": "Nothing was changed. " + " ".join(cutoff_problems)})
                        emit({"type": "pi_agenda", "agenda": payload})
                        continue
                if cutoffs_only:
                    # Nothing but cutoffs: set by code, no model call. (Before the reference check
                    # below: a step whose tool takes the cutoff is the step the reply points at,
                    # whatever words that step uses.)
                    ops = self._cutoff_ops(cutoffs)
                    revised = self._revise_plan_document(question, fb, emit, ops=ops) if ops else None
                    if revised is not None:
                        feedback_log.append(fb)
                        payload = revised
                    else:
                        said = ", ".join(f"{k} = {v}" for k, v in cutoffs.items())
                        emit({"type": "plan_answer", "question": fb, "answer": (
                            f"Nothing was changed: the plan already uses {said}." if not ops else
                            f"Nothing was changed: {said} could not be set on this plan.")})
                    emit({"type": "pi_agenda", "agenda": payload})
                    continue
                # A change request that points at a step the plan does NOT have, or names one of
                # several equally good candidates, is a question in disguise — the reviewer and the
                # plan disagree about what is in it. Say so and re-present the plan untouched,
                # rather than silently constructing the missing step or picking a candidate. See
                # ``resolve_plan_reference`` for the two production cases this replaces.
                if kind == "agenda" and fb:
                    verdict, topic, hits = resolve_plan_reference(fb, list(payload))
                    if verdict != "ok":
                        emit({"type": "plan_reference", "verdict": verdict, "topic": topic,
                              "steps": hits, "request": fb[:300]})
                        emit({"type": "pi_agenda", "agenda": payload})
                        continue
                # With the plan as a document, a change asked for in words is made on that
                # document: the model names what to change and code applies it, like a hand edit.
                # Only when that cannot be done does the revision fall back to patch-or-redraft.
                if kind == "agenda" and fb:
                    revised = self._revise_plan_document(question, fb, emit, pinned=cutoffs)
                    if revised is not None:
                        feedback_log.append(fb)
                        payload = revised
                        emit({"type": "pi_agenda", "agenda": payload})
                        continue
                # The patch or redraft below works from the words; the cutoffs read from them are set
                # on the plan it gives, when the loop brings the document up to date with it.
                pending_cutoffs = (fb, cutoffs) if cutoffs else None
                if fb:
                    feedback_log.append(fb)
                before_plan = list(payload) if kind == "agenda" else []
                kind, payload = self._pi_plan(
                    question, emit, feedback="\n".join(feedback_log),
                    prior_agenda=payload if kind == "agenda" else None,
                    allow_clarify=True,
                    # The patch is judged against ONLY what the user just said; the accumulated log
                    # still drives a redraft, where the full history is the right context.
                    latest_feedback=fb)
                if kind == "failed":
                    if before_plan:
                        # The revision could not be read. The plan the researcher had is still a
                        # plan: keep it, say so, and let them ask again.
                        emit({"type": "plan_revision_failed", "request": fb[:200]})
                        pending_cutoffs = None   # nothing of that reply is applied, its cutoffs included
                        kind, payload = "agenda", before_plan
                        emit({"type": "pi_agenda", "agenda": payload})
                        continue
                    emit({"type": "plan_failed", "reason": payload})
                    return LabResult(question, [], [], False, 0, str(payload))
                # What the redraft did BESIDES what was asked. The single-step patch path cannot
                # damage a step it did not name (this code applies the edit, so collateral is
                # structurally impossible); the whole-plan redraft can, and did — so diff it and
                # put the collateral in front of the reviewer. See ``diff_plans``.
                if before_plan and kind == "agenda" and not getattr(self, "_last_plan_was_patch", False):
                    delta = diff_plans(before_plan, list(payload))
                    if delta["dropped"] or delta["changed"]:
                        emit({"type": "plan_redraft_delta", "request": fb[:200], **delta})
            agenda = payload
            # NOTE: do NOT re-emit pi_agenda here — _pi_plan() already emitted the agenda
            # when it drafted (and re-emits on every revision), so re-emitting the approved
            # plan would post a second identical "📋 Plan ready" card in plan mode.
        else:
            agenda = payload if kind == "agenda" else [question]

        if restored:
            check_restored(list(agenda))

        if reviewing_resume:
            from ..mcp_plan_migration import PlanMigrationRequired
            updated = self._approved_plan or self._plan_doc
            original = resume.approved_plan
            if updated is None or original is None or [s.id for s in updated.steps] != [s.id for s in original.steps]:
                raise PlanMigrationRequired("A same-run revision must keep the plan's step order and identifiers; "
                                            "review a new study for added, removed, or reordered steps.")
            if resume.status_recovery:
                from ..status_continuation import validate_review
                validate_review(original, updated, resume.redo_indices or frozenset())
            if self._scope.source == "explicit_mcp":
                self._scope = replace(self._scope, keep=tuple(s.tool for s in updated.steps if s.tool))
            elif self._scope.keep:
                changed = {name: new.tool for old, new in zip(original.steps, updated.steps)
                           if old.tool != new.tool and new.tool
                           for name in (old.tool, canonical_tool_name(old.tool))}
                self._scope = replace(self._scope, keep=tuple(dict.fromkeys(
                    changed.get(name, name) for name in self._scope.keep)))
            # A reviewed status-only recovery must not launch a replacement scientific
            # job. Its complete typed call uses the same exact-call guard as explicit
            # native workflows; canonical identity comes only from the reviewed registry.
            from ..tool_names import STATUS_TOOLS
            status_steps = ([updated.steps[i] for i in sorted(resume.redo_indices or ())]
                            if resume.status_recovery else updated.steps)
            if status_steps and all(canonical_tool_name(s.tool) in STATUS_TOOLS for s in status_steps):
                self._scope = replace(self._scope, source="explicit_mcp",
                                      keep=tuple(s.tool for s in status_steps if s.tool))
            return self._run_inner(question, on_event=emit, should_cancel=should_cancel,
                pull_injections=pull_injections, checkpoint=checkpoint,
                decision_review=decision_review, should_compact=should_compact,
                resume=replace(resume, agenda=updated.agenda(), approved_plan=updated,
                               plan_context=self.plan_context, modify_note="", review_requested=False,
                               reviewed_note=resume.modify_note or resume.reviewed_note))

        # PI↔Critic PLAN-TIME review (before any step runs) — the plan-time complement to the per-step
        # meetings: catch an incoherent plan (orphan de-novo clustering never reconciled with provided
        # labels, circular enrichment, a step nothing consumes) at its SOURCE, before any compute. Only
        # In plan mode this already ran ABOVE, before the human saw the plan; here it covers the
        # autonomous path, where nothing else ever reads the plan back.
        if plan_review is None and approved_plan is None:
            agenda = self._keep_report_step(agenda, self._apply_scope(
                self._plan_review(question, agenda, emit), emit))

        # Read-back guard (deterministic). A step that only re-reads a table an earlier step wrote
        # produces nothing and consumes a slot a real analysis needed. The PI prompt already forbids
        # planning report/packaging work, but that ban is worded around rendering .docx/.pdf/.zip
        # and a step phrased "Parse the generated DE tables and report the exact tabular output"
        # walked straight past it — into a production plan, where it displaced the composition and
        # enrichment analyses the dataset actually called for. The plan-review Critic is told to
        # catch these too; this is the floor that does not need a model to agree.
        # Tool names + arguments (deterministic, never a prune). The plan-mode path already ran this
        # BEFORE the human saw the plan — where a warning can still change a decision; this covers
        # the autonomous path, and the steps other machinery inserted after the review.
        # Improvising a missing tool with `run_code` is ALLOWED (Yijun: a scientifically useful
        # ad-hoc script — even installing a package — is acceptable); the failure mode is a reviewer
        # approving a plan believing a named, tested tool will run when the step will be improvised.
        agenda = self._check_plan_tooling(agenda, emit, set())

        readback = [s for s in agenda if _is_readback_step(s)]
        if readback:
            agenda = [s for s in agenda if not _is_readback_step(s)]
            emit({"type": "steps_pruned", "reason": "reads_back_earlier_output", "dropped": readback})
        # Same guarantee for report/packaging busywork on the INITIAL agenda. It was only applied to
        # steps added later (exploration / step meetings), so a PI-drafted "**Artifact export** —
        # Export all computed DE tables … to the artifacts directory for automatic report
        # assembly" reached a plan card. Everything the tools compute is already written to
        # artifacts/ and the bundle is assembled automatically; the step is a slot for nothing.
        # The plan's write_report step is not analysis: it is handed to the gateway, which writes the
        # report from the accepted results once the analysis is done (#102). Taken out BEFORE the
        # busywork rule, which would otherwise drop it as one more report step.
        report_steps = [s for s in agenda if _is_report_step(s)]
        if report_steps:
            agenda = [s for s in agenda if not _is_report_step(s)]
            self._report_requested = True
            self._report_steps = report_steps
            emit({"type": "report_step", "steps": report_steps})
        busy = [s for s in agenda if _is_report_busywork(s)]
        if busy and len(busy) < len(agenda):
            agenda = [s for s in agenda if not _is_report_busywork(s)]
            emit({"type": "steps_pruned", "reason": "report_busywork", "dropped": busy})

        # Dependency-chain check (deterministic). A production plan planned ORA + GSEA whose step
        # text said "reads the DE table the previous step wrote" — and had NO step that produced a
        # DE table. The plan-review Critic passed it. Enrichment consumes a contrast; a plan that
        # schedules the consumer without the producer will fail at run time with "no significant DE
        # genes available", which then reads like a null result. Announce it, and give the run a
        # producer: insert a stratified-contrast step ahead of the first enrichment step when the
        # dataset has a condition column, otherwise drop the orphaned enrichment.
        first_enrich = next((i for i, s in enumerate(agenda) if _is_enrichment_step(s)), None)
        if first_enrich is not None and not any(_is_de_producer_step(s) for s in agenda):
            template = self._default_contrast_step()
            producer = ""
            if template:
                producer = self._author_step(
                    question, agenda,
                    brief=("A differential-expression step that PRODUCES the ranked DE tables the "
                           "enrichment step reads: contrast the dataset's experimental condition "
                           "against its control level with `run_de` (or `run_pseudobulk_de` when "
                           "there are >=2 samples per arm), stratified within the existing cell-type "
                           "labels, and say which unit the statistic runs over."),
                    fallback=template)
            if producer:
                agenda = [*agenda[:first_enrich], producer, *agenda[first_enrich:]]
                emit({"type": "plan_dependency_fixed", "inserted": producer,
                      "before_step": first_enrich + 1,
                      "reason": "enrichment was planned with no step producing a DE table"})
            else:
                dropped = [s for s in agenda if _is_enrichment_step(s)]
                agenda = [s for s in agenda if not _is_enrichment_step(s)]
                emit({"type": "steps_pruned", "reason": "enrichment_without_de", "dropped": dropped})

        # No-contrast guard (deterministic). On an already-annotated dataset with NO experimental
        # contrast, pathway/GO enrichment on a cell type's own identity markers is circular — it just
        # restates the cell type's definition ("meaningless enrichment", per review). The planner is
        # steered away from it (_dataset_context / _PI_SYSTEM rule (d)), but LLMs sometimes plan it
        # anyway, so drop enrichment steps here as a guarantee. Clustering/UMAP (visualization) and
        # marker DE (annotation validation) are KEPT — only the enrichment step is meaningless.
        if _annotated_without_contrast((self.ctx.decisions or {}).get("dataset_result")):
            dropped = [s for s in agenda if _is_enrichment_step(s)]
            if dropped:
                agenda = [s for s in agenda if not _is_enrichment_step(s)]
                emit({"type": "steps_pruned", "reason": "no_experimental_contrast", "dropped": dropped})

        # Plan-time methodological decision — the linear-path HITL. The DAG path surfaces forks
        # per-node (_structure_agenda_dag flags them; _run_one_node pauses); the linear loop had NO
        # such pause, so a fork like "the dataset is already labeled — analyze by the labels or
        # re-cluster de-novo?" was silently auto-decided. When a human is available (decision_review,
        # i.e. manual mode) put that fork to them BEFORE any step runs — the SAME decision card the DAG
        # path uses — and thread their choice through the run as standing guidance. Deterministic
        # detection (no LLM), so it fires reliably. DAG has its own per-node decisions, so skip here.
        seed_notes = ""
        if self.config.planner != "dag" and decision_review is not None:
            fork = self._label_decision(agenda)
            if fork is not None:
                goal, options = fork
                from types import SimpleNamespace
                emit({"type": "decision_point", "node": "plan_decision", "goal": goal,
                      "options": list(options)})
                decision = decision_review(
                    SimpleNamespace(id="plan_decision", goal=goal, options=options)) or {"action": "proceed"}
                if str(decision.get("action", "proceed")).lower() == "cancel":
                    emit({"type": "run_cancelled", "completed_steps": 0, "agenda": len(agenda)})
                    return LabResult(question, agenda, [], False, 0,
                                     "Run cancelled by the user at the plan-time decision — no tools ran.")
                choice = str(decision.get("choice", "")).strip()
                if choice:
                    seed_notes = (f"The user was asked how to proceed and chose: {choice}. "
                                  "Follow this choice for the whole analysis.")
                    emit({"type": "decision_made", "node": "plan_decision", "choice": choice})

        if self.config.plan_yaml and self._approved_plan is None:
            # Nobody reviewed this plan (bypass mode). The plan as it stands is still what its own
            # numbers bind the tools to, and what the finished run is read back against.
            self._present_plan_document(question, list(agenda), emit)
            self._approve_plan_document(emit, reviewed=False)

        self._check_cancelled()
        self._planning = False

        # One execution phase over one agenda. Shared by the single-cycle path (below) and by each
        # cycle of a campaign, so a cycle is executed by exactly the same machinery as a whole run.
        def _execute(cycle_agenda: list[str], *, synthesize: bool) -> LabResult:
            if self.config.planner == "langgraph":
                # Same DAG the hand-written scheduler would run, executed by LangGraph. Opt-in.
                self._pull_injections = pull_injections
                return self._run_langgraph(
                    question, self._structure_agenda_dag(question, cycle_agenda, emit), emit,
                    should_cancel, exec_roster=exec_roster, exec_multi=exec_multi,
                    seed_notes=seed_notes, decision_review=decision_review, synthesize=synthesize)
            if self.config.planner == "dag":
                # Structure the reviewed agenda into a dependency DAG and run the ready-set scheduler.
                # The step TEXT is unchanged; only ordering/scoping/scheduling differ. Fresh runs only —
                # resume (A2) stays on the linear loop until node-id mapping lands.
                plan = self._structure_agenda_dag(question, cycle_agenda, emit)
                return self._run_dag(
                    question, plan, emit, should_cancel, pull_injections,
                    exec_roster=exec_roster, exec_multi=exec_multi, decision_review=decision_review,
                    synthesize=synthesize)
            return self._run_loop(
                question, cycle_agenda, emit, should_cancel, pull_injections,
                exec_roster=exec_roster, exec_multi=exec_multi, seed_notes=seed_notes,
                synthesize=synthesize, decision_review=decision_review,
                should_compact=should_compact)

        if self.config.max_cycles <= 1 or self._scope.limited:
            return _execute(agenda, synthesize=True)   # single cycle — today's path, untouched
        return self._run_campaign(question, agenda, _execute, emit, should_cancel)

    # -- the outer loop: several cycles of plan → execute → re-plan ------------

    def _run_campaign(self, question: str, first_agenda: list[str],
                      execute: "Callable[..., LabResult]", emit: EventFn,
                      should_cancel: "Callable[[], bool] | None") -> LabResult:
        """Run SEVERAL cycles: execute a plan, then re-plan the NEXT one from what the last cycles
        actually found. ``config.max_cycles <= 1`` never reaches here.

        This is a different mechanism from within-cycle exploration, and the two compose. Exploration
        is REACTIVE and local: one accepted step yields one hypothesis and the one step that tests
        it, appended to the plan already running. A CYCLE re-plans wholesale with the full picture —
        it can abandon a line of attack, or spend four steps on a question that only became worth
        asking after cycle 1. Neither subsumes the other: exploration cannot restructure a plan, and
        a cycle boundary is too coarse to catch a surprise at step 3.

        Termination is DETERMINISTIC first, model-judged second — an outer loop whose exit condition
        is an LLM opinion is how a run costs a weekend of GPU time:

        * ``max_cycles`` is a hard ceiling;
        * the user cancelling stops it between cycles;
        * NOTHING LEFT TO CHASE — no open hypothesis and the cycle raised none — stops it, which is
          the honest "the questions we could answer are answered" condition;
        * a re-plan that returns no steps, or the same steps as the cycle just run, stops it (a
          model that keeps proposing the work it already did is not making progress).

        The manuscript is written ONCE at the end over EVERY cycle's rounds, so the report reads as
        one study rather than N stapled reports.
        """
        all_rounds: list[LabRound] = []
        all_agenda: list[str] = []
        accepted_total = 0
        agenda = first_agenda
        cycle = 1
        stop_reason = "max_cycles"
        while True:
            emit({"type": "cycle_start", "cycle": cycle, "max_cycles": self.config.max_cycles,
                  "agenda": list(agenda)})
            open_before = len(self._ledger.open_items())
            n_hyp_before = len(self._ledger)
            res = execute(agenda, synthesize=False)
            # Renumber into one continuous sequence so the report and the process artifacts see a
            # single run, not N restarts.
            for r in res.rounds:
                all_rounds.append(LabRound(len(all_rounds) + 1, len(all_rounds) + 1, r.step,
                                           r.specialist, r.scientist_result, r.verdict))
            all_agenda.extend(res.agenda)
            accepted_total += res.accepted_steps
            new_hyp = len(self._ledger) - n_hyp_before
            open_now = len(self._ledger.open_items())
            emit({"type": "cycle_done", "cycle": cycle, "accepted": res.accepted_steps,
                  "steps": len(res.agenda), "new_hypotheses": new_hyp, "open_hypotheses": open_now})
            # A finished cycle is the honest milestone to compact at: the next cycle re-plans from
            # findings and the ledger, not from every earlier step's prose.
            if self.config.context_management and all_rounds:
                self._maybe_compact(all_rounds, emit, steps_done=len(all_agenda),
                                    milestone=f"cycle {cycle} finished")

            if should_cancel is not None and should_cancel():
                stop_reason = "cancelled"
                break
            if cycle >= self.config.max_cycles:
                stop_reason = "max_cycles"
                break
            # Deterministic convergence: this cycle neither answered an outstanding question nor
            # raised one, so another cycle would re-plan against an unchanged picture. Only
            # meaningful with exploration ON — without it the ledger is empty by construction, and
            # applying this test would stop EVERY campaign after cycle 1.
            if (self.config.hypothesis_driven
                    and open_now == 0 and new_hyp == 0 and open_before == 0):
                stop_reason = "nothing_left_to_chase"
                break
            next_agenda, reason = self._plan_next_cycle(question, all_rounds, cycle + 1, emit)
            if not next_agenda:
                stop_reason = reason or "pi_declined"
                break
            if [_norm_step(s) for s in next_agenda] == [_norm_step(s) for s in agenda]:
                stop_reason = "no_progress"     # the re-plan is the cycle we just ran
                break
            agenda = next_agenda
            cycle += 1

        emit({"type": "campaign_done", "cycles": cycle, "reason": stop_reason,
              "steps": len(all_agenda), "accepted": accepted_total,
              "hypotheses": len(self._ledger), "open": len(self._ledger.open_items())})
        converged = accepted_total == len(all_agenda) and len(all_agenda) > 0
        if stop_reason == "cancelled":
            done = "\n".join(f"- {r.step}: {_stopped_round_answer(r)}"
                             for r in all_rounds if r.verdict.verdict == "accept") \
                   or "- (no steps completed yet)"
            emit({"type": "lab_done", "converged": False, "accepted_steps": accepted_total,
                  "agenda": len(all_agenda), "cancelled": True})
            return LabResult(question, all_agenda, all_rounds, False, accepted_total,
                             f"Run stopped by the user after {cycle} cycle(s) — {accepted_total}/"
                             f"{len(all_agenda)} steps completed:\n{done}", self._ledger.to_list())

        team_interpretation = ""
        if self._mode == "team" and self._team and all_rounds:
            accepted_summary = "\n".join(
                f"- {r.step}: {r.scientist_result.get('final_answer') or '(no answer)'}"
                for r in all_rounds if r.verdict.verdict == "accept") or "(no accepted results)"
            team_interpretation = self._team_meeting(
                question,
                "Interpret the analysis results below from each of your perspectives and state "
                "what we can and cannot conclude:\n" + accepted_summary,
                self._team, "interpretation", emit, rounds_ctx=all_rounds)
        self._induce_skills(all_rounds, emit)  # once per campaign, over every cycle's rounds
        final_answer = self._synthesize(question, all_rounds, emit,
                                        team_interpretation=team_interpretation)
        emit({"type": "lab_done", "converged": converged, "accepted_steps": accepted_total,
              "agenda": len(all_agenda), "cycles": cycle})
        return LabResult(question, all_agenda, all_rounds, converged, accepted_total, final_answer,
                         self._ledger.to_list())

    def _induce_skills(self, rounds: list[LabRound], emit: EventFn) -> list[str]:
        """END OF RUN: generalize an accepted ``run_code`` procedure into a reusable skill.

        Called from the three terminal paths (linear loop, DAG, campaign) AFTER the mid-campaign
        early return, so it fires exactly once per run, never per cycle. Returns the names written.

        Everything risky is delegated to ``skill_induction``, which validates the name, compiles the
        code, refuses collisions, and writes OUTSIDE the repo. Best-effort throughout: any failure
        is emitted and swallowed, because "the lab learned nothing this run" must never become "the
        run failed after the science was done".
        """
        if not (self.config.skill_induction and self.config.induced_skills_dir):
            return []
        from ..skill_induction import candidates, induce, write_skill
        # ALL_SKILLS, not SKILLS: a skill still awaiting review is invisible to the models but its
        # name is taken, and the inducer must not re-learn it under another name.
        from ..skills import ALL_SKILLS as _LIB, register_skill
        try:
            cands = candidates(rounds)
            if not cands:
                return []
            kept, rejected = induce(
                cands, self._complete,
                existing_manifest=skill_manifest(_LIB),
                tool_names=", ".join(t.name for t in self.scientist.catalog if t.name != "finish"),
                taken=set(_LIB), max_new=self.config.max_induced_skills)
            written: list[str] = []
            for skill in kept:
                out = write_skill(self.config.induced_skills_dir, skill,
                                  origin_run=self.config.run_id)
                if out is None:
                    rejected.append(f"could not write {skill.name}")
                    continue
                # ``written_name`` may differ from the proposed one: an improvement over an existing
                # skill lands as <name>_vN alongside it, never on top of it.
                folder, written_name = out
                # Register in-process so the NEXT run in this gateway can use it without a restart.
                register_skill(Skill(name=written_name, summary=skill.description,
                                     doc=skill.skill_md(), files={"reference.py": skill.code},
                                     induced=True, supersedes=skill.supersedes))
                written.append(written_name)
                self.induced_skills.append(written_name)
                emit({"type": "skill_induced", "name": written_name, "review": "pending",
                      "description": skill.description, "reason": skill.reason,
                      "origin_step": skill.origin_step, "path": str(folder),
                      "supersedes": skill.supersedes})
            if not written and rejected:
                # Say why nothing was learned — a silent no-op is indistinguishable from a bug.
                emit({"type": "skill_induction_none", "reasons": rejected[:4]})
            return written
        except Exception as exc:  # noqa: BLE001 - the science is already done; never fail here
            emit({"type": "skill_induction_error", "error": f"{type(exc).__name__}: {exc}"})
            return []

    def _plan_next_cycle(self, question: str, rounds: list[LabRound], cycle: int,
                         emit: EventFn) -> "tuple[list[str], str]":
        """Plan the NEXT cycle from what the previous cycles found. Returns ``(agenda, reason)``;
        an EMPTY agenda means "stop", with ``reason`` explaining why — which is the PI's own way of
        saying the study has answered what it can. Never raises."""
        payload = {
            "research_question": question,
            "cycle": cycle,
            "work_already_done": [
                {"step": r.step, "answer": (r.scientist_result.get("final_answer") or "")[:300],
                 "accepted": r.verdict.verdict == "accept"} for r in rounds][-20:],
            "hypotheses": self._ledger.to_list(),
            "dataset_profile": self._dataset_context(),
            "tools_available": ", ".join(t.name for t in self.scientist.catalog if t.name != "finish"),
            # Code templates the scientist runs with run_code; a step that should use one names it.
            "skills_available": plan_skill_lines(f"{question}\n{self._dataset_context() or ''}")[0],
            "max_steps": self.config.max_steps,
        }
        try:
            raw = self._complete([
                {"role": "system", "content": _NEXT_CYCLE_SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ], role="plan")
        except Exception:  # noqa: BLE001 - a failed re-plan ends the campaign; it never kills the run
            return [], "replan_failed"
        rev = _parse_verdict(raw) or {}
        reason = str(rev.get("reason", "")).strip()
        if not bool(rev.get("continue", False)):
            emit({"type": "cycle_declined", "cycle": cycle, "reason": reason})
            return [], "pi_declined"
        steps = [str(s).strip() for s in (rev.get("agenda") or []) if str(s).strip()]
        steps = [s for s in steps if not _is_report_busywork(s)][:self.config.max_steps]
        if not steps:
            emit({"type": "cycle_declined", "cycle": cycle, "reason": reason or "empty plan"})
            return [], "pi_declined"
        emit({"type": "cycle_planned", "cycle": cycle, "agenda": steps, "reason": reason})
        return steps, reason

    def _evaluate_redo_indices(self, agenda: list[str], k: int, modify_note: str,
                               prior_rounds: "list[LabRound]", emit: EventFn) -> set[int]:
        """0-based step indices to RE-RUN on resume (always includes the changed step ``k``).

        A later step is re-run when it depends — directly or transitively — on the changed step's
        analytical output. Only a checkpoint-free, topic-independent step (typically a literature
        search) may be KEPT: an LLM judges topic-dependence, and a deterministic guard restricts
        keeping to literature/background steps so no analysis step that reads the checkpoint chain is
        ever skipped. Conservative fallback (no model, or an unparseable reply) = re-run everything
        downstream, i.e. the prior behaviour."""
        downstream = list(range(k + 1, len(agenda)))
        redo_all = {k, *downstream}
        if not downstream or self._complete_fn is None:
            return redo_all
        by_index = _rounds_by_agenda_index(agenda, prior_rounds)

        def _line(i: int) -> str:
            r = by_index.get(i)
            ans = (r.scientist_result.get("final_answer") if r else None) or "(no prior result)"
            return f"{i + 1}. {agenda[i]} — prior result: {str(ans)[:180]}"

        change = (modify_note or "").strip() or f"step {k + 1} was re-run with a change"
        user = (
            f"Re-run step:\n  {k + 1}. {agenda[k]}\n  Change: {change}\n\n"
            "Later steps and their prior results:\n" + "\n".join(_line(i) for i in downstream) + "\n\n"
            "Return the step numbers of the LATER steps that MUST be re-run because they depend "
            "(directly or transitively) on the changed step's analytical output (clustering / DE / "
            "matrix / annotations). A step that does NOT consume that output — e.g. a literature "
            "search on the general topic — can be kept. Be conservative: when unsure, re-run. "
            "Return ONLY a JSON array of step numbers, e.g. [3,4], or [] if none."
        )
        try:
            raw = self._complete_fn([
                {"role": "system", "content": _RESUME_EVAL_SYSTEM},
                {"role": "user", "content": user}])
        except Exception:  # noqa: BLE001 - evaluation is best-effort; fall back to re-run all
            return redo_all
        nums = safe_json_loads(raw)
        if not isinstance(nums, list):
            m = re.search(r"\[.*\]", raw or "", re.DOTALL)
            try:
                nums = json.loads(m.group(0)) if m else None
            except (ValueError, TypeError):
                nums = None
        if not isinstance(nums, list):
            return redo_all
        flagged = set()
        for n in nums:
            try:
                j = int(n) - 1
            except (TypeError, ValueError):
                continue
            if k < j < len(agenda):
                flagged.add(j)
        # A downstream step is KEPT only if the model did NOT flag it AND it is a checkpoint-free
        # literature/background step; every other downstream step is re-run (sound — analysis steps
        # read the checkpoint chain the change rebuilds).
        redo = {k}
        for i in downstream:
            if not ((i not in flagged) and bool(_LITERATURE_STEP_RE.search(agenda[i]))):
                redo.add(i)
        emit({"type": "resume_impact", "redo": sorted(x + 1 for x in redo),
              "kept": sorted(i + 1 for i in downstream if i not in redo)})
        return redo

    def _run_loop(
        self,
        question: str,
        agenda: list[str],
        emit: EventFn,
        should_cancel: "Callable[[], bool] | None",
        pull_injections: "Callable[[], list[str]] | None",
        *,
        exec_roster: "tuple[Specialist, ...]",
        exec_multi: bool,
        seed_notes: str = "",
        redo_indices: "frozenset[int] | None" = None,
        kept_by_index: "dict[int, LabRound] | None" = None,
        synthesize: bool = True,
        decision_review: "Callable[[Any], dict[str, Any]] | None" = None,
        should_compact: "Callable[[], bool] | None" = None,
    ) -> LabResult:
        """The Scientist → Critic → advance state machine + final synthesis, shared by a fresh run
        and an A2 resume. A fresh run passes the defaults (``redo_indices`` None → every step is
        executed from 0). A resume passes ``redo_indices`` (the 0-based steps to re-execute) and
        ``kept_by_index`` (the prior accepted round to REUSE verbatim for every other step): the loop
        walks the whole agenda, reusing kept steps and re-running only the changed/dependent ones,
        with ``seed_notes`` steering the re-run steps."""
        rounds: list[LabRound] = []
        pruned: set[int] = set()   # agenda indices dropped by the PI↔Critic plan-review meeting
        step_idx = 0
        attempts = 0          # revisions spent on the current step
        last_result: "HarnessResult | None" = None   # previous attempt AT THIS STEP (turn-budget escalation)
        accepted_steps = 0
        executed = 0          # rounds actually run this call (reused rounds don't count vs the budget)
        critique = ""
        cancelled = False
        user_notes = seed_notes or ""   # standing mid-run guidance the user injects while it executes

        # Round budget: an explicit ``max_rounds`` is a hard cap (tests / cloud override); otherwise
        # derive it from the agenda so every planned step runs — bounded anyway by the per-step
        # ``max_revisions`` force-advance, so "do all the planned work" can never starve a late step.
        # Derived from the CURRENT agenda length on every iteration, not once: hypothesis-driven
        # exploration can append steps mid-run, and a budget frozen at the original length would let
        # a discovered step be added and then starved by a budget that never knew about it. Both
        # growth paths stay bounded (max_new_steps, max_steps), so this cannot run away.
        def round_budget() -> int:
            return (self.config.max_rounds if self.config.max_rounds is not None
                    else len(agenda) * (1 + self.config.max_revisions))

        agenda = list(agenda)   # local copy — exploration appends to it; never mutate the caller's
        while step_idx < len(agenda) and executed < round_budget():
            # A step the plan-review meeting pruned (pre-flight skip, or a post-step review that made
            # it moot) is walked past without running — it is out of the effective agenda.
            if step_idx in pruned:
                step_idx += 1
                continue
            # Resume reuse: a step not in the re-run set is restored verbatim from the prior run
            # (no Scientist/Critic call) — its analysis output/checkpoint is still valid.
            if redo_indices is not None and step_idx not in redo_indices:
                kept = (kept_by_index or {}).get(step_idx)
                if kept is not None:
                    rounds.append(kept)
                    if kept.verdict.verdict == "accept":
                        accepted_steps += 1
                    step_idx += 1
                    continue
            # Stop between steps if the user hit Stop (they often only notice a wrong turn
            # mid-run). The Scientist also checks this between its own tool turns.
            if should_cancel is not None and should_cancel():
                cancelled = True
                emit({"type": "run_cancelled", "completed_steps": accepted_steps, "agenda": len(agenda)})
                break
            # Mid-run injection: fold any notes the user submitted since the last step into
            # standing guidance applied to THIS and every remaining step (it accumulates, so
            # it survives the per-step critique reset below).
            if pull_injections is not None:
                notes = pull_injections()
                if notes:
                    joined = "\n".join(notes)
                    user_notes = (user_notes + "\n" + joined) if user_notes else joined
                    emit({"type": "user_injection", "text": joined})
            # CONTEXT MANAGEMENT, before the step is briefed — the brief is what carries the
            # history, so measuring after building it would be a step too late. The compact
            # "command" arrives here as a CONTROL SIGNAL (``should_compact``), not as a magic
            # string parsed out of a user note: the trigger is code, so its cause should be too.
            if self.config.context_management:
                asked = bool(should_compact and should_compact())
                action = self._maybe_compact(rounds, emit, steps_done=step_idx,
                                             manual=asked, decision_review=decision_review)
                if action == "abort":
                    cancelled = True
                    emit({"type": "run_cancelled", "completed_steps": accepted_steps,
                          "agenda": len(agenda), "reason": "context_stop"})
                    break
            step = agenda[step_idx]
            # PI↔Critic PRE-FLIGHT gate: is running this step justified right now? (necessity /
            # redundancy / precondition / altitude.) skip → walk past it (out of the effective agenda);
            # amend → fold the adjustment into this step's brief. No-op unless config.step_meetings.
            gate = self._preflight_gate(question, step, agenda, step_idx, rounds, emit, pruned=pruned)
            if gate.action == "skip":
                pruned.add(step_idx)
                emit({"type": "steps_pruned", "reason": "preflight", "dropped": [step],
                      "detail": gate.reason})
                step_idx += 1
                attempts = 0
                critique = ""
                continue
            specialist = _route_specialist(step, exec_roster) if exec_multi else GENERALIST
            call_notes = user_notes
            if gate.action == "amend" and gate.amendment:
                amend_line = "[Plan review — adjust how you run THIS step]: " + gate.amendment
                call_notes = (user_notes + "\n" + amend_line) if user_notes else amend_line
            result = self._scientist(question, step, specialist, critique, rounds, emit, should_cancel,
                                     user_notes=call_notes,
                                     extra_steps=_budget_bonus(last_result, attempts))   # Scientist node
            last_result = result
            executed += 1
            verdict = self._critic(question, step, result, emit)                           # Critic node
            rounds.append(LabRound(len(rounds) + 1, step_idx + 1, step, specialist.name, result.to_dict(), verdict))
            # Durably record the run BEFORE deciding what comes next: if the process dies here, the
            # work this round just produced is still resumable. A resume appends its kept rounds only
            # as it walks past them, so the ones still ahead go into the snapshot too — otherwise a
            # second crash forgets steps the first run finished (seen live resuming 78a707cd79e9).
            ahead = [r for i, r in sorted((kept_by_index or {}).items())
                     if i > step_idx and i not in pruned]
            self._checkpoint_state(question, agenda, rounds + ahead,
                                   accepted_steps + (1 if verdict.verdict == "accept" else 0)
                                   + sum(r.verdict.verdict == "accept" for r in ahead))

            # Convergence is LLM-judged: the step advances when the Critic says "accept".
            # (The Critic's deterministic guard still forces "revise" on a failed/empty
            # run, so a broken step can never be rubber-stamped.) No fixed score gate.
            accepted = verdict.verdict == "accept"
            if accepted:
                accepted_steps += 1
                # PI POST-STEP review: did this step change the picture, and is any REMAINING step now
                # moot? Prune those so the plan stays honest (linear loop enacts the prune).
                review = self._poststep_review(question, step, verdict, result, agenda, step_idx,
                                               rounds, emit, pruned=pruned)
                for txt in review.get("prune", []):
                    for j in range(step_idx + 1, len(agenda)):
                        if j not in pruned and agenda[j] == txt:
                            pruned.add(j)
                            emit({"type": "steps_pruned", "reason": "poststep_review",
                                  "dropped": [txt], "detail": review.get("contribution", "")})
                            break
                # EXPLORATION — the plan's only growth path. A result that contradicts the plan's
                # premise becomes a falsifiable hypothesis plus the step(s) that test it, appended to
                # the END of the agenda so every index already in flight (step_idx, pruned, the
                # rounds' step_index) stays valid. Appended steps are ordinary steps: they face the
                # pre-flight gate, the Critic, and the revision cap like any planned one.
                new_steps = self._explore_after_step(question, step, verdict, result.to_dict(),
                                                     agenda, rounds, emit)
                if new_steps:
                    agenda.extend(new_steps)
                    emit({"type": "agenda_extended", "added": new_steps, "agenda": len(agenda)})
                step_idx += 1
                attempts = 0
                critique = ""
            elif _is_literature_step(step):
                # A literature step's query is DETERMINISTIC — an LLM "revise" just re-runs the
                # IDENTICAL Europe PMC query (same junk, same reject). Never revise-loop it: take
                # the one attempt and move on (References fall back to whatever it did accept, or
                # the honest-empty note).
                emit({"type": "step_force_advance", "step": step})
                step_idx += 1
                attempts = 0
                critique = ""
            else:
                attempts += 1
                critique = verdict.critique
                if attempts > self.config.max_revisions:   # stop revising this step; move on (NOT accepted)
                    emit({"type": "step_force_advance", "step": step})
                    step_idx += 1
                    attempts = 0
                    critique = ""

        if cancelled:
            # Don't make another LLM call here — the user may be stopping *because* the
            # model is misbehaving. Return a deterministic summary of what actually
            # completed so they can see it and adjust the question/preset, then re-run.
            accepted = [r for r in rounds if r.verdict.verdict == "accept"]
            done = "\n".join(
                f"- Step {r.step_index} ({r.step}): "
                f"{_stopped_round_answer(r)}"
                for r in accepted
            ) or "- (no steps completed yet)"
            final_answer = (
                f"Run stopped by the user before completion — {accepted_steps}/{len(agenda)} "
                f"planned steps completed:\n{done}"
            )
            emit({"type": "lab_done", "converged": False, "accepted_steps": accepted_steps,
                  "agenda": len(agenda), "cancelled": True})
            return LabResult(question, agenda, rounds, False, accepted_steps, final_answer,
                             self._ledger.to_list())

        # --- Guaranteed literature grounding --------------------------------------------------
        # A literature step is deterministic (Europe PMC keyword search, no LLM tool-choice) and
        # the manuscript's `## References` are built ONLY from an ACCEPTED `literature_search`
        # round. But that step sits LAST in the agenda and shares the bounded round budget
        # (`max_rounds`) with the heavy analysis steps, so a couple of QC/DE revisions can exhaust
        # the budget before it ever runs — leaving References silently empty even though the user
        # asked for literature. If a planned literature step was never reached, run it once here,
        # OUTSIDE the budget, with a deterministic verdict (accept iff it returned DOI/PMID-backed
        # citations). This is what "the run actually calls and accepts literature_search" requires
        # (see handoff/ziyao) — the tool is cheap and needs no LLM Critic to judge a DOI/PMID hit.
        executed_indices = set(_rounds_by_agenda_index(agenda, rounds))   # kept DAG rounds count too
        for i, step in enumerate(agenda):
            if i in executed_indices or i in pruned or not _is_literature_step(step):
                continue
            if should_cancel is not None and should_cancel():
                break
            specialist = _route_specialist(step, exec_roster) if exec_multi else GENERALIST
            emit({"type": "literature_backfill", "step": step})
            result = self._scientist(question, step, specialist, "", rounds, emit,
                                     should_cancel, user_notes=user_notes)
            accepted_lit = result.status == "ok"
            verdict = CriticVerdict(
                "accept" if accepted_lit else "revise",
                1.0 if accepted_lit else 0.0,
                "" if accepted_lit else "literature_search returned no DOI/PMID-backed citations",
            )
            rounds.append(LabRound(len(rounds) + 1, i + 1, step, specialist.name,
                                   result.to_dict(), verdict))
            if accepted_lit:
                accepted_steps += 1

        # Pruned steps leave the EFFECTIVE agenda, so the run still converges on N-of-(N-pruned).
        effective_len = len(agenda) - len(pruned)
        converged = accepted_steps == effective_len and effective_len > 0
        # A campaign cycle that is not the last one produces no write-up: the team interpretation and
        # the manuscript are written ONCE, over every cycle's rounds, by the campaign loop. Emitting
        # ``lab_done`` here too would tell the gateway the whole run had ended mid-campaign.
        if not synthesize:
            return LabResult(question, agenda, rounds, converged, accepted_steps, "",
                             self._ledger.to_list())
        # Team mode: the team INTERPRETS the accepted results in a meeting (independent
        # contexts) before the PI writes the report — multi-angle interpretation, not one voice.
        team_interpretation = ""
        if self._mode == "team" and self._team and rounds:
            accepted_summary = "\n".join(
                f"- {r.step}: {r.scientist_result.get('final_answer') or '(no answer)'}"
                for r in rounds if r.verdict.verdict == "accept") or "(no accepted results)"
            team_interpretation = self._team_meeting(
                question,
                "Interpret the analysis results below from each of your perspectives and state "
                "what we can and cannot conclude:\n" + accepted_summary,
                self._team, "interpretation", emit, rounds_ctx=rounds)
        self._induce_skills(rounds, emit)      # end of run: keep what the lab figured out
        final_answer = self._synthesize(question, rounds, emit, team_interpretation=team_interpretation)
        emit({"type": "lab_done", "converged": converged, "accepted_steps": accepted_steps,
              "agenda": len(agenda), "pruned": len(pruned)})
        return LabResult(question, agenda, rounds, converged, accepted_steps, final_answer,
                         self._ledger.to_list())

    # -- DAG planner + scheduler (feat/dag-planner) ---------------------------

    def _structure_agenda_dag(self, question: str, agenda: list[str], emit: EventFn) -> LabPlan:
        """Turn the reviewed flat agenda into a dependency DAG WITHOUT changing the step text: ask the
        model which earlier step each step consumes. Falls back to a linear DAG (identical to the
        linear loop) on 0/1 steps, no LLM, or any parse failure — so this can never do worse than the
        linear order."""
        if len(agenda) <= 1:
            return lift_agenda_to_dag(agenda)
        ids = [f"s{i + 1}" for i in range(len(agenda))]
        listing = "\n".join(f"{ids[i]}: {agenda[i]}" for i in range(len(agenda)))
        deps: dict[str, list[str]] = {}
        meta: dict[str, dict[str, Any]] = {}   # id -> {decision, options} from the structure pass
        try:
            raw = self._complete([
                {"role": "system", "content": _DAG_STRUCTURE_SYSTEM},
                {"role": "user", "content": f"Research goal: {question}\n\nSteps:\n{listing}\n\n"
                                            "Return the dependency JSON now."},
            ], role="plan")
            obj = safe_json_loads(raw)
            if not isinstance(obj, list):
                m = re.search(r"\[.*\]", raw or "", re.DOTALL)
                obj = json.loads(m.group(0)) if m else None
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict) and item.get("id"):
                        sid = str(item["id"]).strip()
                        deps[sid] = [str(d).strip() for d in (item.get("depends_on") or [])]
                        opts = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
                        meta[sid] = {"decision": bool(item.get("decision")) and len(opts) >= 2,
                                     "options": opts[:4]}
        except Exception:  # noqa: BLE001 - structuring is best-effort; fall back to linear
            deps = {}
        # Build node dicts (goals stay verbatim; only-earlier deps) and validate via parse_dag.
        node_dicts = []
        for i, sid in enumerate(ids):
            d = [x for x in deps.get(sid, []) if x in ids[:i]]   # only reference EARLIER ids
            nd = {"id": sid, "goal": agenda[i], "depends_on": d}
            m = meta.get(sid) or {}
            if m.get("decision"):
                nd["decision"] = True
                nd["options"] = m.get("options") or []
            node_dicts.append(nd)
        plan = parse_dag(json.dumps({"nodes": node_dicts}), max_nodes=len(agenda))
        # Safety net: if the structure pass inferred NO dependency across >2 steps, it almost
        # certainly failed (an analysis pipeline QC→cluster→DE→… is inherently sequential; a real
        # all-parallel plan is vanishingly rare). An all-roots DAG would leave ordering to the
        # Coordinator's luck and could schedule a step before its checkpoint exists — fall back to
        # the linear chain, honouring the "never worse than linear" guarantee. Decision flags are
        # preserved by re-applying them onto the linear chain.
        if plan is not None and len(plan.nodes) > 2 and not any(n.depends_on for n in plan.nodes):
            linear = lift_agenda_to_dag(agenda)
            decided = {n.id: n for n in plan.nodes if n.decision}
            if decided:
                from ..dag import LabPlan, TaskNode
                linear = LabPlan(tuple(
                    (TaskNode(id=n.id, goal=n.goal, depends_on=n.depends_on,
                              decision=True, options=decided[n.id].options)
                     if n.id in decided else n)
                    for n in linear.nodes))
            plan = linear
        plan = plan or lift_agenda_to_dag(agenda)
        # A labels-vs-re-cluster fork the PLAN has already answered is not a question. When no step
        # clusters de-novo, the reviewed plan analyses by the existing labels, and asking again only
        # pauses the run (f3731e0b7136: the structurer copied the fork from its prompt's example onto
        # "validate the existing majorclass labels", in a plan with no clustering step at all).
        if not _plan_has_clustering([n.goal for n in plan.nodes]):
            settled = [n.id for n in plan.nodes if n.decision and _is_label_fork(n.options)]
            if settled:
                from dataclasses import replace as _dc_replace
                from ..dag import LabPlan
                plan = LabPlan(tuple(_dc_replace(n, decision=False, options=())
                                     if n.id in settled else n for n in plan.nodes))
                for sid in settled:
                    emit({"type": "decision_settled", "node": sid,
                          "choice": "Use the existing labels",
                          "reason": "the reviewed plan has no de-novo clustering step"})
        # Deterministic fork (no LLM): if the dataset is already labeled AND a node clusters de-novo,
        # ENSURE that node is a human decision — "analyze by the existing labels vs re-cluster de-novo"
        # — even when the model did not flag it (Qwen often doesn't). Same fork the linear path asks;
        # only when the structurer flagged NO decision of its own, so we never double-ask. ``replace``
        # preserves the node's consumes/produces/suggested_tool (scheduling footprint intact).
        fork = self._label_decision([n.goal for n in plan.nodes])
        if fork is not None and not any(n.decision for n in plan.nodes):
            from dataclasses import replace as _dc_replace
            from ..dag import LabPlan
            _, options = fork
            flagged = False
            new_nodes = []
            for n in plan.nodes:
                if not flagged and _clusters_de_novo(n.goal or ""):
                    new_nodes.append(_dc_replace(n, decision=True, options=tuple(options)))
                    flagged = True
                else:
                    new_nodes.append(n)
            if flagged:
                plan = LabPlan(tuple(new_nodes))
        # _run_dag emits lab_plan_dag at execution start — don't double-emit here.
        return plan

    def _coordinator_pick(self, question: str, plan: LabPlan, ready: list[str],
                          rounds: list[LabRound], emit: EventFn) -> str:
        """Pick the next task among the READY set. A single ready task is taken directly (no LLM); a
        real choice (branches) goes to the Coordinator. Falls back to plan order on any failure."""
        if len(ready) <= 1:
            return ready[0]
        byid = plan.by_id()
        done_lines = "\n".join(f"- {r.step}" for r in rounds if r.verdict.verdict == "accept") or "- (none yet)"
        ready_lines = "\n".join(f"- {rid}: {byid[rid].goal}" for rid in ready)
        try:
            raw = self._complete([
                {"role": "system", "content": _COORDINATOR_SYSTEM},
                {"role": "user", "content": f"Research goal: {question}\n\nAlready done:\n{done_lines}"
                                            f"\n\nReady tasks:\n{ready_lines}\n\nWhich id next?"},
            ], role="classify")
            obj = safe_json_loads(raw)
            nxt = str(obj.get("next")).strip() if isinstance(obj, dict) and obj.get("next") else ""
            if nxt not in ready:   # tolerate a bare id / slug in prose
                nxt = next((rid for rid in ready if re.search(rf"\b{re.escape(rid)}\b", raw or "")), "")
            if nxt in ready:
                emit({"type": "coordinator_pick", "next": nxt, "ready": list(ready)})
                return nxt
        except Exception:  # noqa: BLE001 - scheduling choice is best-effort
            pass
        return ready[0]

    def _claim_specialist(self, question: str, node: TaskNode,
                          roster: "tuple[Specialist, ...]", emit: EventFn) -> Specialist:
        """Real multi-agent: the team's experts CLAIM the ready node whose expertise fits best — the
        agents decide who does what, instead of a keyword lookup. One expert → taken directly; a real
        roster goes to the LLM. Falls back to deterministic ``_route_specialist`` on any failure, so
        behaviour is never worse than keyword routing."""
        if len(roster) <= 1:
            return roster[0] if roster else GENERALIST
        listing = "\n".join(f"{i + 1}. {sp.name} — {sp.persona[:160]}" for i, sp in enumerate(roster))
        try:
            raw = self._complete([
                {"role": "system", "content": _CLAIM_SYSTEM},
                {"role": "user", "content": f"Research goal: {question}\n\nTask: {node.goal}\n\n"
                                            f"Team members:\n{listing}\n\nWhich member claims it?"},
            ], role="classify")
            obj = safe_json_loads(raw)
            idx = None
            if isinstance(obj, dict) and obj.get("member") is not None:
                idx = int(obj["member"]) - 1
            if idx is None:                              # tolerate a bare number in prose
                m = re.search(r"\b([1-9][0-9]*)\b", raw or "")
                idx = int(m.group(1)) - 1 if m else None
            if idx is not None and 0 <= idx < len(roster):
                chosen = roster[idx]
                emit({"type": "node_claim", "node": node.id, "specialist": chosen.name})
                return chosen
        except Exception:  # noqa: BLE001 - claim is best-effort; fall back to keyword routing
            pass
        chosen = _route_specialist(node.goal, roster)
        emit({"type": "node_claim", "node": node.id, "specialist": chosen.name})
        return chosen

    def _run_one_node(self, question: str, node: TaskNode, prior_rounds: list[LabRound],
                      emit: EventFn, should_cancel: "Callable[[], bool] | None",
                      exec_roster: "tuple[Specialist, ...]", exec_multi: bool,
                      decision_review: "Callable[[Any], dict[str, Any]] | None",
                      user_notes: str) -> dict[str, Any]:
        """Execute ONE DAG node end to end: optional human decision (solo nodes only) → expert claim →
        the Scientist/Critic revise loop → terminal. Returns {node, rounds, accepted, cancelled,
        executed}. ``prior_rounds`` is the snapshot of accepted findings at BATCH start, so nodes that
        run concurrently share the same upstream context — never each other's in-flight work. Safe to
        call from a worker thread: it only reads shared state and appends to its own local list."""
        node_notes = ""
        if node.decision:
            emit({"type": "decision_point", "node": node.id, "goal": node.goal,
                  "options": list(node.options)})
            if decision_review is not None:
                decision = decision_review(node) or {"action": "proceed"}
                if str(decision.get("action", "proceed")).lower() == "cancel":
                    return {"node": node, "rounds": [], "accepted": False, "cancelled": True, "executed": 0}
                choice = str(decision.get("choice", "")).strip()
                if choice:
                    node_notes = (f"The user was asked how to proceed with this step and chose: "
                                  f"{choice}. Follow this choice for this step.")
                    emit({"type": "decision_made", "node": node.id, "choice": choice})
        if not exec_multi:
            specialist = GENERALIST
        elif self.config.multi_agent:
            specialist = self._claim_specialist(question, node, exec_roster, emit)
        else:
            specialist = _route_specialist(node.goal, exec_roster)
        step_text = _node_step_text(node)
        step_notes = "\n".join(t for t in (user_notes, node_notes) if t)
        # Axis C — read this expert's PRIVATE memory (advisory) into the brief before it acts.
        mem_block = ""
        if self._agent_memory is not None:
            mem_block = self._agent_memory.read(specialist.name, node.goal)
            if mem_block:
                emit({"type": "memory_read", "node": node.id, "specialist": specialist.name})
        node_rounds: list[LabRound] = []
        executed = 0
        critique = ""
        attempts = 0
        last_result: "HarnessResult | None" = None   # previous attempt AT THIS NODE (turn budget)
        fork_count = 0          # how many times a hard-failure fork was raised for THIS node
        cancelled = False
        accepted = False
        verdict = None
        # PI↔Critic pre-flight gate. In the DAG path we enact an AMEND (fold into the brief) and the
        # deterministic no-contrast-enrichment floor; a model "skip" is surfaced as a recommendation
        # but NOT enacted here — dropping a node with dependents needs the scheduler's dependency-aware
        # replan, so full skip/downstream-prune stays linear-only for now (see the meeting-protocol doc).
        gate = self._preflight_gate(question, node.goal, [node.goal], 0, prior_rounds, emit)
        if gate.action == "amend" and gate.amendment:
            step_notes = "\n".join(t for t in (
                step_notes, "[Plan review — adjust how you run THIS step]: " + gate.amendment) if t)
        while True:   # revise the SAME node in place, then advance
            # The brief is the node's goal plus reuse hints; the approved plan knows the step by its
            # goal. Looked up by the brief, no step matched and no approved setting was ever pinned
            # (prod run caa188e8e672: run_cellqc got species=auto against an approved "mouse").
            result = self._scientist(question, step_text, specialist, critique,
                                     prior_rounds + node_rounds, emit, should_cancel,
                                     user_notes=step_notes, memory=mem_block,
                                     extra_steps=_budget_bonus(last_result, attempts),
                                     plan_step=node.goal)
            last_result = result
            executed += 1
            verdict = self._critic(question, node.goal, result, emit)
            node_rounds.append(LabRound(0, 0, node.goal, specialist.name, result.to_dict(), verdict))
            if verdict.verdict == "accept":
                accepted = True
                break
            if _is_literature_step(node.goal):
                emit({"type": "step_force_advance", "step": node.goal})
                break
            attempts += 1
            critique = verdict.critique
            if attempts > self.config.max_revisions:
                # HARD-FAILED (revisions exhausted). Instead of silently force-advancing, the LLM
                # proposes concrete ALTERNATIVE approaches for this step: manual mode lets the human
                # pick one (retry) / skip / abort; headless/bypass auto-applies the best alternative
                # (self-heal). Bounded to _MAX_FAILURE_FORKS asks per node, then force-advances.
                if fork_count < _MAX_FAILURE_FORKS:
                    action, hint = self._failure_decision(
                        question, node, verdict.critique, prior_rounds, decision_review, emit)
                    if action == "abort":
                        cancelled = True
                        break
                    if action == "retry":
                        fork_count += 1
                        attempts = 0
                        critique = ((verdict.critique or "")
                                    + (f"\n\nAlternative approach to try: {hint}" if hint else "")).strip()
                        emit({"type": "step_retry", "node": node.id, "approach": hint[:120]})
                        continue
                emit({"type": "step_force_advance", "step": node.goal})
                break
        # Axis C — write ONE episode for this node (private to this expert), for cross-run learning.
        if self._agent_memory is not None:
            answer = (node_rounds[-1].scientist_result.get("final_answer") if node_rounds else "") or ""
            self._agent_memory.write_episode(specialist.name, {
                "node": node.goal,
                "action": str(answer)[:240],
                "outcome": "accepted" if accepted else "revised/advanced",
                "note": (verdict.critique[:200] if verdict and verdict.critique else ""),
            })
        return {"node": node, "rounds": node_rounds, "accepted": accepted,
                "cancelled": cancelled, "executed": executed}

    def _propose_alternatives(self, question: str, node: TaskNode, critique: str,
                              prior_rounds: list[LabRound], emit: EventFn) -> list[str]:
        """Ask the LLM for 2-4 CONCRETE alternative approaches to accomplish THIS failed step (different
        params / tool / method / reuse an existing input) — the replacement options a human picks from,
        or the agent auto-applies headless. Scoped to the ONE step; must not change the research goal.
        Returns ``[]`` on any failure (caller falls back to skip)."""
        tools = ", ".join(t.name for t in self.scientist.catalog if t.name != "finish")
        findings = (self._accepted_findings_block(prior_rounds) or "")[:1500]
        try:
            raw = self._complete([
                {"role": "system", "content": _ALTERNATIVES_SYSTEM},
                {"role": "user", "content": (
                    f"Research goal: {question}\n\nStep that FAILED: {node.goal}\n"
                    f"Why it failed (Critic): {(critique or 'no usable result').strip()[:400]}\n\n"
                    f"Tools available: {tools}\n"
                    f"Accepted upstream findings:\n{findings or '(none)'}\n\n"
                    "Propose the alternative approaches now (JSON array of short strings).")},
            ], role="plan")
            obj = safe_json_loads(raw)
            if not isinstance(obj, list):
                m = re.search(r"\[.*\]", raw or "", re.DOTALL)
                obj = json.loads(m.group(0)) if m else None
            if isinstance(obj, list):
                return [str(x).strip() for x in obj if str(x).strip()][:4]
        except Exception:  # noqa: BLE001 - proposing alternatives is best-effort; degrade to skip
            pass
        return []

    def _failure_decision(self, question: str, node: TaskNode, critique: str,
                          prior_rounds: list[LabRound],
                          decision_review: "Callable[[Any], dict[str, Any]] | None",
                          emit: EventFn) -> "tuple[str, str]":
        """A step that HARD-FAILED (revisions exhausted): the LLM proposes concrete ALTERNATIVE
        approaches for this step, and — MANUAL mode — the human picks one (or Skip / Abort) via the
        decision-point channel; HEADLESS/bypass — the agent auto-applies the top alternative (bounded
        self-heal) instead of silently skipping. Returns ``(action, guidance)``, action ∈ {"retry",
        "skip", "abort"}; a chosen/auto-picked alternative rides along as the retry ``guidance`` folded
        into the step's brief. Bare/timeout answer ⇒ skip (== today's force-advance)."""
        from types import SimpleNamespace
        alternatives = self._propose_alternatives(question, node, critique, prior_rounds, emit)
        emit({"type": "step_failure", "node": node.id, "goal": node.goal,
              "critique": (critique or "")[:300], "alternatives": alternatives})

        # Run-level HITL budget. Spent, the run keeps self-healing but stops interrupting: the
        # per-node cap bounds ONE step and says nothing about fifteen of them. Abort stays
        # reachable — Stop ends the run at any point — so what is given up here is the per-failure
        # card, not control of the run.
        asked = getattr(self, "_human_failure_asks", 0)
        if decision_review is not None and asked >= _MAX_HUMAN_FAILURE_ASKS:
            emit({"type": "failure_asks_exhausted", "node": node.id, "asks": asked,
                  "approach": (alternatives[0][:120] if alternatives else "")})
            return ("retry", alternatives[0]) if alternatives else ("skip", "")
        if decision_review is not None:
            self._human_failure_asks = asked + 1

        if decision_review is None:
            # Headless / bypass: SELF-HEAL by auto-applying the best alternative (bounded by the fork
            # cap), instead of silently skipping. No alternative ⇒ skip (today's behaviour).
            return ("retry", alternatives[0]) if alternatives else ("skip", "")

        # Manual: the LLM's alternatives ARE the options, plus explicit Skip / Abort controls.
        skip_opt, abort_opt = "Skip this step", "Abort the run"
        options = [*alternatives, skip_opt, abort_opt]
        goal = (f"Step failed after {self.config.max_revisions + 1} attempts: {node.goal}. "
                f"Reason: {(critique or 'no usable result').strip()[:200]}. "
                "Pick an alternative approach, or skip/abort.")
        fork = SimpleNamespace(id=f"{node.id}-failfork", goal=goal, options=tuple(options), decision=True)
        decision = decision_review(fork) or {"action": "skip"}
        if str(decision.get("action", "")).lower() == "cancel":
            return ("abort", "")
        # Nobody answered. The reviewer's own contract for a timeout is "proceed with the agent's
        # judgment", and for THIS fork the agent's judgment is the alternative it just proposed —
        # the same self-heal the headless path takes above. Landing on Skip instead threw the step
        # away for the one reason that says nothing about whether it should be thrown away.
        if decision.get("timed_out") and alternatives:
            emit({"type": "step_self_healed", "node": node.id, "approach": alternatives[0][:120],
                  "reason": "decision point timed out"})
            return ("retry", alternatives[0])
        choice = str(decision.get("choice", "")).strip()
        low = choice.lower()
        if low == skip_opt.lower():
            # Recorded: the step's rounds say that it was not accepted, not that the user left it.
            self._skipped_steps.append(node.goal)
            return ("skip", "")
        if not choice:
            return ("skip", "")
        if low == abort_opt.lower():
            return ("abort", "")
        return ("retry", choice)   # a chosen alternative (or free text) ⇒ retry WITH it as guidance

    def _run_langgraph(
        self,
        question: str,
        plan: LabPlan,
        emit: EventFn,
        should_cancel: "Callable[[], bool] | None",
        *,
        exec_roster: "tuple[Specialist, ...]",
        exec_multi: bool,
        seed_notes: str = "",
        decision_review: "Callable[[Any], dict[str, Any]] | None" = None,
        synthesize: bool = True,
    ) -> LabResult:
        """Execute the DAG on a LangGraph ``StateGraph`` instead of the hand-written scheduler.

        The division of labour is the whole point: LangGraph owns the graph, the state and its
        checkpointing; every node still runs through :meth:`_run_one_node`, so the Scientist/Critic
        loop, the specialists, the decision points and the evidence grounding are byte-for-byte the
        same work the tested scheduler does. See ``agents/lab_graph.py`` for why the plan is
        re-serialized before it becomes a graph (LangGraph co-runs every ready node; our analysis
        nodes share one checkpoint chain and must not).

        Opt-in (``config.planner == "langgraph"``). Compared with ``_run_dag`` this path does NOT
        yet enact the Coordinator's ordering choice among ready nodes (the graph decides), nor the
        mid-run agenda growth of hypothesis-driven exploration (the graph is compiled up front) —
        both are deliberate omissions of the first increment, not oversights.
        """
        from ..lab_graph import build_lab_graph, order_rounds, serialize_conflicting_nodes

        graph_plan, added_edges = serialize_conflicting_nodes(
            plan, lambda a, b: not _concurrency_safe(a, b))
        emit({"type": "lab_plan_dag", "nodes": graph_plan.to_list()})
        if added_edges:
            # Say it out loud: these edges are NOT the PI's plan, they are the resource constraint
            # that used to live invisibly inside the scheduler's dispatch check.
            emit({"type": "graph_serialized", "edges": [list(e) for e in added_edges],
                  "reason": "nodes share an analysis checkpoint / scanpy global state"})

        user_notes = seed_notes or ""

        def run_node(node: TaskNode, prior_rounds: list[LabRound]) -> dict[str, Any]:
            notes = user_notes
            if pull := getattr(self, "_pull_injections", None):
                extra = pull()
                if extra:
                    notes = (notes + "\n" + "\n".join(extra)) if notes else "\n".join(extra)
            if should_cancel is not None and should_cancel():
                return {"node": node, "rounds": [], "accepted": False, "cancelled": True,
                        "executed": 0}
            return self._run_one_node(question, node, prior_rounds, emit, should_cancel,
                                      exec_roster, exec_multi, decision_review, notes)

        app = build_lab_graph(graph_plan, run_node)
        final = app.invoke({"rounds": [], "executed": 0, "accepted": [], "cancelled": []})

        # Plan order, not completion order — otherwise the report's step numbering depends on which
        # parallel branch happened to finish first.
        ordered = order_rounds(graph_plan, final.get("rounds", []))
        rounds = [LabRound(i + 1, i + 1, r.step, r.specialist, r.scientist_result, r.verdict)
                  for i, r in enumerate(ordered)]
        accepted_steps = len(final.get("accepted", []))
        cancelled = bool(final.get("cancelled"))
        agenda_goals = graph_plan.goals()
        if cancelled:
            emit({"type": "run_cancelled", "completed_steps": accepted_steps,
                  "agenda": len(graph_plan.nodes)})
        self._checkpoint_state(question, agenda_goals, rounds, accepted_steps)
        converged = accepted_steps == len(graph_plan.nodes) and not cancelled
        if not synthesize:
            return LabResult(question, agenda_goals, rounds, converged, accepted_steps, "",
                             self._ledger.to_list())
        final_answer = self._synthesize(question, rounds, emit)
        return LabResult(question, agenda_goals, rounds, converged, accepted_steps, final_answer,
                         self._ledger.to_list())

    def _run_dag(
        self,
        question: str,
        plan: LabPlan,
        emit: EventFn,
        should_cancel: "Callable[[], bool] | None",
        pull_injections: "Callable[[], list[str]] | None",
        *,
        exec_roster: "tuple[Specialist, ...]",
        exec_multi: bool,
        seed_notes: str = "",
        decision_review: "Callable[[Any], dict[str, Any]] | None" = None,
        synthesize: bool = True,
    ) -> LabResult:
        """Ready-set scheduler over the DAG: run READY tasks (deps done) in a Coordinator-chosen
        order, each with a SCOPED brief, until the graph drains or the round budget is spent. Mirrors
        _run_loop's Critic/revise/literature/synthesis behaviour so it is a drop-in for a fresh run."""
        rounds: list[LabRound] = []
        done_ids: set[str] = set()       # terminal nodes (accepted OR force-advanced) — deps satisfied
        accepted_ids: set[str] = set()
        executed = 0
        cancelled = False
        user_notes = seed_notes or ""
        byid = plan.by_id()
        n_nodes = len(plan.nodes)
        agenda_goals = plan.goals()
        emit({"type": "lab_plan_dag", "nodes": plan.to_list()})

        # Round budget: an explicit ``max_rounds`` is a hard cap (tests / cloud override); otherwise
        # derive it from the node count so every planned node runs — same rule as _run_loop, bounded
        # by the per-node ``max_revisions`` force-advance. (``max_rounds`` defaults to None since the
        # planner-budget change; DAG must derive it too, not assume an int.)
        # Recomputed each iteration (not frozen once): hypothesis-driven exploration can add nodes
        # mid-run, and a budget fixed at the original node count would admit a discovered task and
        # then starve it. Growth is bounded by max_new_steps + max_steps, so this cannot run away.
        def round_budget() -> int:
            return (self.config.max_rounds if self.config.max_rounds is not None
                    else len(plan.nodes) * (1 + self.config.max_revisions))

        while executed < round_budget():
            ready = plan.ready_ids(done_ids)
            if not ready:
                break
            if should_cancel is not None and should_cancel():
                cancelled = True
                emit({"type": "run_cancelled", "completed_steps": len(accepted_ids), "agenda": n_nodes})
                break
            if pull_injections is not None:
                notes = pull_injections()
                if notes:
                    joined = "\n".join(notes)
                    user_notes = (user_notes + "\n" + joined) if user_notes else joined
                    emit({"type": "user_injection", "text": joined})
            primary = byid[self._coordinator_pick(question, plan, ready, rounds, emit)]
            # Build a CONCURRENCY-SAFE batch: co-run only ready nodes whose footprints are disjoint
            # (see _concurrency_safe) — in practice an independent literature/background branch runs
            # alongside the sequential analysis chain, never two analysis nodes. A decision node
            # pauses for the user, so it always runs SOLO.
            batch = [primary]
            if self.config.max_concurrency > 1 and not primary.decision:
                res = _node_resources(primary)
                for rid in ready:
                    if rid == primary.id or len(batch) >= self.config.max_concurrency:
                        continue
                    cand = byid[rid]
                    if cand.decision or not _node_resources(cand).isdisjoint(res):
                        continue
                    batch.append(cand)
                    res |= _node_resources(cand)
            snapshot = list(rounds)      # every node in the batch sees the SAME upstream context
            if len(batch) == 1:
                outcomes = [self._run_one_node(question, batch[0], snapshot, emit, should_cancel,
                                               exec_roster, exec_multi, decision_review, user_notes)]
            else:
                emit({"type": "concurrency_batch", "nodes": [n.id for n in batch]})
                from ..loop_utils import ContextThreadPoolExecutor
                with ContextThreadPoolExecutor(max_workers=len(batch)) as ex:
                    futs = [ex.submit(self._run_one_node, question, n, snapshot, emit, should_cancel,
                                      exec_roster, exec_multi, decision_review, user_notes)
                            for n in batch]
                    outcomes = [f.result() for f in futs]
            for oc in outcomes:          # merge deterministically in batch order; renumber rounds
                for r in oc["rounds"]:
                    rounds.append(LabRound(len(rounds) + 1, len(rounds) + 1, r.step,
                                           r.specialist, r.scientist_result, r.verdict))
                executed += oc["executed"]
                done_ids.add(oc["node"].id)
                if oc["accepted"]:
                    accepted_ids.add(oc["node"].id)
                if oc["cancelled"]:
                    cancelled = True
            # Same mid-run durability as the linear loop, taken once per merged batch (the merge is
            # single-threaded, so this never races a worker).
            self._checkpoint_state(question, plan.goals(), rounds, len(accepted_ids))
            # EXPLORATION — the DAG's growth path, run here in the SINGLE-THREADED merge (never
            # inside a worker, so the ledger needs no lock). A discovered task becomes a real node
            # DEPENDING ON the node whose result provoked it, so the scheduler treats it exactly like
            # a planned task: it becomes ready only once its parent is done, it can be picked by the
            # Coordinator, claimed by an expert, and it can itself provoke further exploration.
            if self.config.hypothesis_driven and not cancelled:
                for oc in outcomes:
                    if not oc["accepted"] or not oc["rounds"]:
                        continue
                    node, last = oc["node"], oc["rounds"][-1]
                    new_steps = self._explore_after_step(
                        question, node.goal, last.verdict, last.scientist_result,
                        plan.goals(), rounds, emit)
                    if not new_steps:
                        continue
                    plan = plan.extend([TaskNode(id=plan.next_id(), goal=text,
                                                 depends_on=(node.id,))
                                        for text in new_steps])
                    byid = plan.by_id()
                    n_nodes = len(plan.nodes)
                    agenda_goals = plan.goals()
                    emit({"type": "agenda_extended", "added": new_steps, "agenda": n_nodes,
                          "after_node": node.id})
            if cancelled:
                emit({"type": "run_cancelled", "completed_steps": len(accepted_ids), "agenda": n_nodes})
                break

        # Axis C — EVOLVE: at end of run, each expert that acted reflects its episodes into updated
        # lessons (cross-run learning, distinct from the in-step Critic loop). Best-effort, skipped if
        # cancelled early. One bounded LLM call per acting expert; time-shared on the same model.
        if self._agent_memory is not None and not cancelled:
            for name in dict.fromkeys(r.specialist for r in rounds):   # unique, in order
                if self._agent_memory.reflect(name, self._complete, role_hint=name):
                    emit({"type": "memory_reflect", "specialist": name})

        accepted_steps = len(accepted_ids)
        if cancelled:
            accepted = [r for r in rounds if r.verdict.verdict == "accept"]
            done = "\n".join(
                f"- {r.step}: {_stopped_round_answer(r)}"
                for r in accepted) or "- (no steps completed yet)"
            final_answer = (f"Run stopped by the user before completion — {accepted_steps}/{n_nodes} "
                            f"planned tasks completed:\n{done}")
            emit({"type": "lab_done", "converged": False, "accepted_steps": accepted_steps,
                  "agenda": n_nodes, "cancelled": True})
            return LabResult(question, agenda_goals, rounds, False, accepted_steps, final_answer,
                             self._ledger.to_list())

        # Guaranteed literature grounding: run any planned-but-unreached literature node once here,
        # outside the budget, with a deterministic verdict (accept iff DOI/PMID citations). Same
        # rationale as the linear loop's backfill.
        for node in plan.nodes:
            if node.id in done_ids or not _is_literature_step(node.goal):
                continue
            if should_cancel is not None and should_cancel():
                break
            specialist = _route_specialist(node.goal, exec_roster) if exec_multi else GENERALIST
            emit({"type": "literature_backfill", "step": node.goal})
            result = self._scientist(question, node.goal, specialist, "", rounds, emit,
                                     should_cancel, user_notes=user_notes)
            accepted_lit = result.status == "ok"
            verdict = CriticVerdict(
                "accept" if accepted_lit else "revise", 1.0 if accepted_lit else 0.0,
                "" if accepted_lit else "literature_search returned no DOI/PMID-backed citations")
            rounds.append(LabRound(len(rounds) + 1, len(rounds) + 1, node.goal,
                                   specialist.name, result.to_dict(), verdict))
            done_ids.add(node.id)
            if accepted_lit:
                accepted_ids.add(node.id)

        accepted_steps = len(accepted_ids)
        converged = accepted_steps == n_nodes and n_nodes > 0
        # Mid-campaign cycle: no write-up here (see the same guard in _run_loop).
        if not synthesize:
            return LabResult(question, agenda_goals, rounds, converged, accepted_steps, "",
                             self._ledger.to_list())
        team_interpretation = ""
        if self._mode == "team" and self._team and rounds:
            accepted_summary = "\n".join(
                f"- {r.step}: {r.scientist_result.get('final_answer') or '(no answer)'}"
                for r in rounds if r.verdict.verdict == "accept") or "(no accepted results)"
            team_interpretation = self._team_meeting(
                question,
                "Interpret the analysis results below from each of your perspectives and state "
                "what we can and cannot conclude:\n" + accepted_summary,
                self._team, "interpretation", emit, rounds_ctx=rounds)
        self._induce_skills(rounds, emit)      # end of run: keep what the lab figured out
        final_answer = self._synthesize(question, rounds, emit, team_interpretation=team_interpretation)
        emit({"type": "lab_done", "converged": converged, "accepted_steps": accepted_steps, "agenda": n_nodes})
        return LabResult(question, agenda_goals, rounds, converged, accepted_steps, final_answer,
                         self._ledger.to_list())

    # -- roles ----------------------------------------------------------------
    # Axis B pipeline routing lives in ``preset_pipelines.select_pipeline`` (called from ``run()``).

    # -- Axis A: Virtual-Lab team mode ----------------------------------------

    def _route_mode(self, question: str, emit: EventFn) -> str:
        """``mode="auto"`` only: team vs single.

        The DATASET decides first, deterministically; the LLM only breaks ties. Routing was an
        LLM call over the question alone, so two near-identical questions on the same two-arm
        annotated dataset went different ways — team (a design meeting, ~7 min, 8-9 steps with
        composition and a domain-motivated intron proxy) vs single (~1 min, 4 steps, no
        composition) — with the same words. Measured on prod: the team plans were the sound ones.
        A dataset with an experimental contrast AND existing cell-type labels is exactly the
        multi-disciplinary case the meeting exists for (design + statistics + biology), so it is
        team by rule; a single-arm / label-only descriptive dataset is single by rule; only when
        the profile says neither does the PI's judgement of the question decide.

        The rule says the dataset CAN carry a comparison, not that the request asks for one. A
        descriptive question on a dataset with a contrast column (the retina atlas's
        fovea/macula/periphery ``tissue``: "where are LRAT, CRALBP, rho and ABCR expressed?") held
        a two-round meeting of 20+ minutes whose experts said, in round one, that nothing was
        being compared. So a request that compares none of the dataset's groups is planned by one
        scientist (:meth:`_asks_for_comparison`)."""
        dr = (self.ctx.decisions or {}).get("dataset_result")
        forced = _dataset_mode_rule(dr)
        if forced == "team" and not self._asks_for_comparison(question, _contrast_columns(dr)):
            emit({"type": "mode_selected", "mode": "single", "reason": "descriptive"})
            return "single"
        if forced:
            emit({"type": "mode_selected", "mode": forced, "reason": "dataset_design"})
            return forced
        raw = self._complete([
            {"role": "system", "content": _MODE_ROUTE_SYSTEM},
            {"role": "user", "content": f"Research question:\n{question}\n\nReply 'team' or 'single'."},
        ], role="classify")
        mode = "team" if "team" in raw.strip().lower()[:24] else "single"
        emit({"type": "mode_selected", "mode": mode, "reason": "question"})
        return mode

    def _asks_for_comparison(self, question: str, contrasts: "list[tuple[str, list[str]]]") -> bool:
        """Does the request compare the dataset's groups (``contrasts``)? A comparison named in so
        many words answers yes at once. Otherwise the session's model is asked, and only a
        confident "describe" answers no: a failed call, an unreadable answer or a doubt keeps the
        design meeting, as before this check."""
        if _COMPARISON_CUE.search(question or ""):
            return True
        groups = "; ".join(f"{c} ({', '.join(v)})" if v else c for c, v in contrasts) or "(none named)"
        try:
            raw = self._complete([
                {"role": "system", "content": _COMPARISON_ROUTE_SYSTEM},
                {"role": "user", "content": f"Groupings in the dataset: {groups}\n\nThe request:\n"
                                            f"{question}\n\nReturn the JSON."},
            ], role="classify")
        except Exception as exc:  # noqa: BLE001 - the meeting stays, as before this check (Stop is a BaseException)
            print(f"[lab] comparison check failed: {type(exc).__name__}: {exc}")
            return True
        obj = safe_json_loads(raw)
        if obj is None:
            m = re.search(r"\{.*\}", str(raw or ""), re.DOTALL)
            obj = safe_json_loads(m.group(0)) if m else None
        if not obj or str(obj.get("asks", "")).strip().lower() != "describe":
            return True
        try:
            confidence = float(obj.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return confidence < _DESCRIBE_CONFIDENCE

    def _study_context(self, question: str) -> str:
        """What the study IS, for the roles that run before planning — team formation and the
        design meeting. The question alone is not enough to describe a study.

        Measured on the production bundles (2026-08-11): of the 11 team runs that had a real
        assignment to make, **4 were launched with a question like "complete the research" or
        "finish the research"** — continuation phrases, which is a normal way to use this console.
        The PI then assembled a team out of nothing and produced generic titles ("Biostatistician",
        "Computational Statistician"), and in every one of those runs a single member executed all
        the work. Runs WITH a real question averaged 2-3 members sharing it 62/38.

        So the fix is not to balance the assignment — that is already reasonable — it is to stop
        forming the team blind. The dataset profile and the selected research protocol are both
        resolved before this point (Axis B runs before Axis A), and together they describe the
        study even when the question is one word."""
        parts = [f"Research question:\n{question}"]
        if len(question.strip()) < 40:
            parts.append(
                "NOTE: that question is a continuation phrase, not a description of the study. "
                "Base your answer on the dataset and protocol below, NOT on the question's wording.")
        profile = self._dataset_context()
        if profile:
            parts.append(profile)
        labels = [s.label for s in self._skills if getattr(s, "label", None)]
        if labels:
            parts.append("Research protocol(s) selected for this study: " + ", ".join(labels))
        return "\n\n".join(parts)

    def _form_team(self, question: str, emit: EventFn) -> tuple[Specialist, ...]:
        """The PI dynamically assembles a complementary expert team for THIS study (each
        expert becomes an independent-context persona). Falls back to the fixed roster.

        Sees the DATASET and the selected protocol, not just the question — see
        :meth:`_study_context` for the production measurement that motivated it."""
        raw = self._complete([
            {"role": "system", "content": _TEAM_FORM_SYSTEM},
            {"role": "user", "content": self._study_context(question)
                                        + "\n\nAssemble the team (JSON array)."},
        ])
        team = tuple(_parse_team(raw, self.config.team_size)) or self.config.specialists
        emit({"type": "team_formed", "members": [{"title": e.name, "expertise": e.persona} for e in team]})
        return team

    def _name_literature_tool(self, prompt: str) -> str:
        """``prompt`` naming the literature tool this session has.

        The PI's instructions say `deep_literature`. A session without it (no HPC3 literature line:
        ``registry.session_catalog``) still has `literature_search`, and a PI told to reserve a step
        for a tool that is not in its list plans that tool anyway."""
        names = {t.name for t in getattr(self.scientist, "catalog", None) or ()}
        if "deep_literature" in names:
            return prompt
        if "search_corpus" in names:
            return prompt.replace("`deep_literature`", "`search_corpus`")
        if "literature_search" not in names:
            return prompt
        return prompt.replace("`deep_literature`", "`literature_search`")

    def _usable_tool_names(self) -> "frozenset[str] | None":
        """Tools that are in the catalog AND can actually run here, for pipeline routing.

        A tool stays in the catalog when its backend is absent (``scgpt_annotate`` with no GPU
        session reports not-enabled rather than vanishing), which is right for the roster and wrong
        for choosing a protocol: the run announced a scGPT pipeline whose defining tool could not
        execute. None when there is no catalog to read, which skips the check rather than declaring
        everything unavailable — a router that silently rejects every protocol is worse than one
        that cannot see the gaps.
        """
        catalog = getattr(self.scientist, "catalog", None)
        if not catalog:
            return None
        return frozenset(t.name for t in catalog if getattr(t, "enabled", True))

    def _record_design_citations(self, member: str, steps: "list[dict[str, Any]]") -> None:
        """Keep the DOI/PMID-backed papers an expert pulled up during the design meeting.

        These do not belong in ``## References`` — that list asserts a paper supports a FINDING, and
        is built only from Critic-accepted analysis steps. But throwing them away is how a run that
        ran eleven real literature lookups while designing itself ends up showing none of them.
        De-duplicated by DOI (else PMID) across every expert and round.
        """
        seen = {c.get("doi") or c.get("pmid") for c in self._design_citations}
        found: list[dict[str, Any]] = []
        for step in steps or ():
            if not isinstance(step, dict) or not step.get("ok"):
                continue
            if canonical_tool_name(step.get("tool")) not in _LITERATURE_TOOLS:
                continue
            result = step.get("result") if isinstance(step.get("result"), dict) else {}
            for citation in (result.get("results") or result.get("citations") or ()):
                if not isinstance(citation, dict):
                    continue
                doi = str(citation.get("doi") or "").strip().lower()
                pmid = str(citation.get("pmid") or "").strip()
                key = doi or pmid
                if not key or key in seen:
                    continue
                seen.add(key)
                found.append({
                    "title": str(citation.get("title") or "").strip(),
                    "doi": doi, "pmid": pmid,
                    "year": citation.get("year"),
                    "journal": str(citation.get("journal") or "").strip(),
                    "consulted_by": member,
                })
        if found:
            with self._design_lock:
                self._design_citations.extend(found)

    def _expert_turns(self, msg_lists: "list[list[dict[str, Any]]]", emit: EventFn,
                      names: "list[str] | None" = None) -> list[str]:
        """One turn per expert, concurrently — with READ-ONLY tools when they are available.

        A meeting expert used to be able only to talk: it received a digest of a finding and had no
        way to check it. Giving it ``inspect_dataset`` / ``literature_search`` / ``deep_literature``
        (a whitelist — see ``_MEETING_TOOLS``; nothing that writes) turns a persona into something
        that can go and LOOK, which is the second half of making the disagreement real: one expert
        can come back with evidence the others simply do not have.

        Falls back to plain completions when no tool-capable Scientist is wired (offline tests), so
        the meeting always works."""
        tools = [t for t in getattr(self.scientist, "catalog", []) if canonical_tool_name(t.name) in _MEETING_TOOLS]
        if not tools or not self.config.meeting_tools:
            return self._complete_concurrent(msg_lists)

        from dataclasses import replace as _dc_replace

        from ..research_harness import HarnessConfig, ResearchHarness
        try:
            # A meeting turn is a LOOKUP, not an analysis: cap the tool budget hard so an expert
            # cannot turn a meeting into a second execution phase.
            cfg = _dc_replace(self.config.scientist, max_steps=self.config.meeting_tool_calls)
        except TypeError:
            cfg = HarnessConfig()

        def _relay(member: str) -> "EventFn":
            """Forward ONLY an expert's tool traffic, renamed so it cannot be mistaken for the
            main run's. A meeting expert may call ``deep_literature``, which submits a Slurm job
            and takes minutes; with the harness's events dropped on the floor those minutes
            rendered as a dead screen between two rounds of contributions and read as a hang.
            Everything else the harness emits (model_call, finish, early_stop, context_*) stays
            swallowed — it belongs to a lookup, not to the study."""
            def _on(ev: "dict[str, Any]") -> None:
                et = ev.get("type")
                if et == "tool_start":
                    emit({"type": "expert_tool", "member": member,
                          "tool": ev.get("tool"), "state": "start"})
                elif et == "tool_result":
                    emit({"type": "expert_tool", "member": member, "tool": ev.get("tool"),
                          "state": "done", "detail": str(ev.get("summary") or "")[:200],
                          "timing_outcome": ev.get("timing_outcome", "unknown")})
                elif et == "tool_error":
                    emit({"type": "expert_tool", "member": member, "tool": ev.get("tool"),
                          "state": "error", "detail": str(ev.get("error") or "")[:200],
                          "timing_outcome": "failed"})
            return _on

        def _one(item: "tuple[str, list[dict[str, Any]]]") -> str:
            self._check_cancelled()
            member, messages = item
            brief = messages[0]["content"] + "\n\n" + messages[1]["content"]
            try:
                harness = ResearchHarness(catalog=list(tools), config=cfg,
                                          chat_fn=getattr(self.scientist, "_chat_fn", None))
                chat = harness._resolve_chat(self.ctx)

                def _chat(messages, tools):
                    self._check_cancelled()
                    reply = chat(messages, tools)
                    self._check_cancelled()
                    return reply

                harness._chat_fn = _chat
                res = harness.run(brief, self.ctx, on_event=_relay(member),
                                  should_cancel=getattr(self, "_should_cancel", None))
                self._check_cancelled()
                self._record_design_citations(member, res.steps)
                answer = (res.final_answer or "").strip()
                # An expert owes the room an OPINION. When the harness ran out of tool turns
                # without one, its deterministic digest closes the STEP but must never be spoken
                # as the expert's contribution — hand the lookups back as notes and let the
                # persona speak. (Before ``answer_synthesized`` existed the digest was non-empty,
                # so this fallback silently stopped firing and meetings printed tool logs.)
                if answer and res.answer_synthesized:
                    notes = list(messages) + [{
                        "role": "user",
                        "content": ("Notes from your own lookups (you ran out of lookup turns "
                                    "before writing anything):\n" + answer +
                                    "\n\nNow give your input in your own words."),
                    }]
                    return self._complete(notes)
                return answer or self._complete(messages)
            except Exception as exc:  # noqa: BLE001 - an expert that cannot use tools still speaks
                emit({"type": "expert_tools_failed", "error": f"{type(exc).__name__}: {exc}"})
                return self._complete(messages)

        from ..loop_utils import ContextThreadPoolExecutor
        workers = max(1, min(len(msg_lists), self.config.max_meeting_concurrency))
        labels = list(names or [])
        items = [(labels[i] if i < len(labels) else f"expert {i + 1}", m)
                 for i, m in enumerate(msg_lists)]
        with ContextThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_one, items))

    def _team_meeting(self, question: str, topic: str, experts: tuple[Specialist, ...],
                      kind: str, emit: EventFn,
                      rounds_ctx: "list[LabRound] | None" = None) -> str:
        previous = getattr(self, "_meeting_active", False)
        self._meeting_active = True
        try:
            self._check_cancelled()
            return self._team_meeting_inner(question, topic, experts, kind, emit, rounds_ctx)
        except _RunCancelled:
            emit({"type": "team_meeting_cancelled", "kind": kind})
            return ""
        finally:
            self._meeting_active = previous

    def _team_meeting_inner(self, question: str, topic: str, experts: tuple[Specialist, ...],
                            kind: str, emit: EventFn,
                            rounds_ctx: "list[LabRound] | None" = None) -> str:
        """One Virtual-Lab team meeting (collaborative, score-driven, up to ``meeting_rounds``).

        Round 1: each expert gives an INDEPENDENT take, issued CONCURRENTLY so vLLM batches them
        on the single A100 (N expert calls cost ~1 round-trip). Each later round: experts BUILD
        ON the PI's shared synthesis AND the Critic's score-driven feedback — extend/correct/
        challenge it — still issued concurrently (they share the synthesis, not each other's raw
        turns). The Critic scores the team's readiness each round; the meeting ends EARLY once the
        score clears ``meeting_accept_score`` (cheap on easy topics, deeper on contested ones).
        Returns the final PI synthesis."""
        emit({"type": "team_meeting_start", "kind": kind, "members": [e.name for e in experts]})
        rounds = max(1, self.config.meeting_rounds)
        synthesis, feedback = "", ""
        for rnd in range(rounds):
            cancel = getattr(self, "_should_cancel", None)
            if cancel is not None and cancel():
                emit({"type": "team_meeting_cancelled", "kind": kind, "round": rnd + 1})
                break
            # Round > 0: experts collaborate by building on the PI's shared synthesis + the
            # Critic's score-driven feedback (the "challenge" is conditioned on the ACTUAL
            # critique, not a blanket disagree). They still never see each other's raw turns,
            # so the requests stay independent and batchable.
            collab = ("\n\nThe team's shared synthesis from the prior round — BUILD ON IT: extend, "
                      f"correct, or challenge it from your expertise; do not merely restate it.\n{synthesis}{feedback}"
                      if synthesis else "")
            # INFORMATION ASYMMETRY: each expert holds a different slice of the accepted findings
            # (see _assign_evidence). This is what makes the disagreement worth having.
            evidence = _assign_evidence(experts, rounds_ctx or [])
            msg_lists = [[
                {"role": "system", "content": (
                    f"You are {e.name}, an expert team member. {e.persona} Contribute concise, "
                    "specific, technically-grounded input on the meeting topic from YOUR expertise. "
                    "Be honest about uncertainty and disagree when the evidence warrants. You hold "
                    "only PART of the picture — say so rather than papering over what you cannot see.")},
                # The study context, not just the question: a design meeting held over "complete
                # the research" is a meeting about nothing, and 4 of 11 production team runs were
                # launched exactly that way.
                {"role": "user", "content": f"{self._study_context(question)}\n\n"
                                            + (evidence[i] + "\n\n" if evidence[i] else "")
                                            + f"Meeting topic:\n{topic}{collab}\n\nYour input:"},
            ] for i, e in enumerate(experts)]
            texts = self._expert_turns(msg_lists, emit,               # CONCURRENT → batched on the GPU
                                       [e.name for e in experts])
            self._check_cancelled()
            contributions = [(e.name, (t or "").strip()) for e, t in zip(experts, texts)]
            for name, text in contributions:
                emit({"type": "expert_contribution", "kind": kind, "round": rnd + 1, "member": name, "text": text})
            joined = "\n\n".join(f"{name}:\n{text}" for name, text in contributions)
            score, critique = self._meeting_critic(topic, joined, kind, rnd, emit)
            synthesis = self._complete([
                {"role": "system", "content": _MEETING_SYNTH_SYSTEM},
                {"role": "user", "content": (f"Research question:\n{question}\n\nTopic:\n{topic}\n\n"
                                             f"Team contributions:\n{joined}\n\nCritic (score {score:.2f}):\n{critique}\n\n"
                                             "Write the PI synthesis now.")},
            ]).strip()
            emit({"type": "meeting_synthesis", "kind": kind, "round": rnd + 1, "text": synthesis})
            if rnd < rounds - 1:                                       # decide the next round from the score
                if score >= self.config.meeting_accept_score:
                    emit({"type": "meeting_converged", "kind": kind, "round": rnd + 1, "score": score})
                    break
                feedback = _round_feedback(score, critique)
        return synthesis

    def _meeting_critic(self, topic: str, joined: str, kind: str, rnd: int,
                        emit: EventFn) -> tuple[float, str]:
        """The meeting Critic scores the team's current thinking (0-1) and says what to fix.
        The score drives the next round's feedback + the early-stop. Defaults to 0.5 if the
        model didn't return parseable JSON (errs toward more deliberation, not premature stop)."""
        parsed = _parse_verdict(self._complete([
            {"role": "system", "content": _MEETING_CRITIC_SYSTEM},
            {"role": "user", "content": f"Topic:\n{topic}\n\nExpert contributions:\n{joined}"},
        ], role="critic")) or {}
        try:
            score = float(parsed.get("score", 0.5))
        except (TypeError, ValueError):
            score = 0.5
        critique = str(parsed.get("critique") or "").strip() or "(no specific critique)"
        emit({"type": "meeting_critic", "kind": kind, "round": rnd + 1, "score": score, "text": critique})
        return score, critique

    def _author_step(self, question: str, agenda: "list[str]", brief: str,
                     fallback: str) -> str:
        """Ask the PI to write ONE missing step (see ``_PLAN_AUTHOR_STEP_SYSTEM``); ``fallback`` is
        used only when no model is wired or the reply is unusable — never as the first choice."""
        if self._complete_fn is None:
            return fallback
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(agenda)) or "(empty)"
        parts = [f"Research question:\n{question}\n"]
        dataset_ctx = self._dataset_context()
        if dataset_ctx:
            parts.append(dataset_ctx + "\n")
        parts.append(f"Current plan:\n{numbered}\n")
        parts.append(f"The missing step:\n{brief}\n\nWrite that one step now.")
        try:
            raw = (self._complete([{"role": "system", "content": _PLAN_AUTHOR_STEP_SYSTEM},
                                   {"role": "user", "content": "\n".join(parts)}], role="plan") or "").strip()
        except Exception:  # noqa: BLE001 - authoring is best-effort; the guard still holds
            return fallback
        # Tolerate a leading list marker or a JSON-ish wrapper from a chatty model.
        text = raw.strip().strip("`").strip()
        text = _strip_step_ordinal(re.sub(r"^\s*[-*]\s+", "", text))
        if text.startswith("{"):
            obj = safe_json_loads(text)
            if isinstance(obj, dict):
                text = _strip_step_ordinal(
                    str(obj.get("step") or obj.get("text") or obj.get("new_text") or ""))
        # A usable step is a sentence, not a fragment or a refusal.
        if len(text) < 40 or "\n" in text.strip() and len(text.splitlines()) > 3:
            return fallback
        return text.splitlines()[0].strip() if "\n" in text else text

    def _default_contrast_step(self) -> str:
        """A stratified condition-vs-control `run_de` step written from the dataset profile, or ""
        when the profile shows no condition column to contrast. Used ONLY to repair a plan that
        schedules enrichment with nothing producing a DE table."""
        dr = (self.ctx.decisions or {}).get("dataset_result") or {}
        cats = dr.get("obs_categoricals") if isinstance(dr, dict) else None
        if not isinstance(cats, dict):
            return ""
        cond = ct = None
        cond_levels: list[str] = []
        for col, info in cats.items():
            if not isinstance(info, dict):
                continue
            n = info.get("n")
            if _looks_like_celltype_col(col):
                if ct is None or (isinstance(n, int) and n <= 20):
                    ct = ct or col
            elif isinstance(n, int) and 2 <= n <= 3 and not _looks_like_qc_col(col):
                cond, cond_levels = col, [str(v) for v in (info.get("values") or [])]
        if not cond:
            return ""
        ref = next((v for v in cond_levels if v.upper() in ("WT", "CONTROL", "CTRL", "CTL",
                                                             "NORMAL", "HEALTHY", "VEHICLE")),
                   cond_levels[0] if cond_levels else "<control level>")
        strat = (f", separately within each `{ct}` cell type (stratify_by='{ct}')" if ct else "")
        return (f"**Condition contrast** — Compare the levels of `{cond}` against the reference "
                f"'{ref}' with `run_de` (Wilcoxon rank-sum, reference='{ref}'{strat}), testing "
                f"only genes detected in >=10% of either arm's cells and calling a gene changed at "
                f"adjusted p < 0.05 with |log2FC| >= 0.25. This produces the ranked DE tables the "
                f"enrichment step below reads.")

    def _design_facts(self) -> "DesignFacts":
        """The replication structure, for the hypothesis gate — NOT for planning (the profile text
        already carries the planning version).

        Deliberately conservative: it answers "is there provably no within-arm replication?" and
        otherwise says it does not know, because an unknown design must never block a hypothesis.
        The rule is level counts on the non-annotation, non-QC obs columns: if the richest of them
        has at most two levels, the most it can encode is the CONDITION itself, so nothing separates
        samples within an arm and there is one replicate per arm. On the Ddx41 object that is
        `orig.ident` (1 level), `sampleid` (2), `DF.classifications` (1) -> one per arm, which is
        exactly the design that made a between-arm p-value unable to distinguish anything."""
        dr = (self.ctx.decisions or {}).get("dataset_result")
        cats = dr.get("obs_categoricals") if isinstance(dr, dict) else None
        if not isinstance(cats, dict) or not cats:
            return DesignFacts()
        levels = [info["n"] for col, info in cats.items()
                  if isinstance(info, dict) and isinstance(info.get("n"), int)
                  and not _looks_like_celltype_col(col) and not _looks_like_qc_col(col)]
        if not levels:
            return DesignFacts()
        return DesignFacts(replicates_per_arm=1) if max(levels) <= 2 else DesignFacts()

    def _dataset_context(self) -> str:
        """A compact, factual profile of the loaded dataset for the PI's planner: shape + the
        obs/metadata columns (with category values when low-cardinality). Lets the PI plan around
        the ACTUAL experimental design (a condition/group column) and REUSE existing label columns,
        instead of defaulting to a generic descriptive atlas. ``''`` when no dataset is loaded."""
        dr = (self.ctx.decisions or {}).get("dataset_result")
        if not isinstance(dr, dict):
            return self._bound_files_context()
        head: list[str] = []
        if dr.get("cells") and dr.get("genes"):
            head.append(f"{dr['cells']} cells x {dr['genes']} genes")
        if dr.get("dataset_kind"):
            head.append(str(dr["dataset_kind"]))
        lines = ["Dataset profile" + (f": {', '.join(head)}." if head else ":")]
        # What the uploaded FOLDER is, when the gateway recognised it (a Cell Ranger delivery, say),
        # and the QC route that implies. Decided from the file layout before planning, so the plan
        # starts on the right branch instead of being corrected after a wrong QC step has run.
        if dr.get("input_layout_hint"):
            lines.append("⚠ " + str(dr["input_layout_hint"]))
        handed = handed_on_note(dr)
        if handed:
            lines.append(handed)
        if dr.get("tissue_hint"):           # an .h5ad's; a 10x delivery's is in its layout hint
            lines.append("⚠ " + str(dr["tissue_hint"]).strip())

        cats = dr.get("obs_categoricals") or {}
        if isinstance(cats, dict) and cats:
            lines.append("Metadata (obs) columns with categories:")
            for col, info in cats.items():
                info = info if isinstance(info, dict) else {}
                n = info.get("n")
                vals = info.get("values") or []
                if vals:
                    shown = vals[:12]
                    more = f", … (+{n - len(shown)} more)" if isinstance(n, int) and n > len(shown) else ""
                    lines.append(f"  - {col}: {n} categories [{', '.join(map(str, shown))}{more}]")
                else:
                    lines.append(f"  - {col}: {n} categories")
        other = [k for k in (dr.get("obs_keys") or []) if k not in cats and not str(k).startswith("_")]
        if other:
            lines.append("Other obs columns (numeric / high-cardinality): " + ", ".join(map(str, other)))
        # SPECIES, read off the gene identifiers. Without it a DDX41 plan on a mouse retina called
        # the species "unknown" and wrote every marker in human upper case (GFAP, SAG) — symbols
        # an exact-match lookup on this matrix does not find.
        gs = dr.get("gene_symbols") if isinstance(dr.get("gene_symbols"), dict) else None
        if gs and gs.get("species_hint"):
            ex = [str(g) for g in (gs.get("examples") or [])]
            if gs.get("identifiers") == "symbols":
                lines.append(
                    f"Gene identifiers: {gs.get('symbol_case')} symbols ({gs.get('evidence')}), so "
                    f"the species is {gs['species_hint']}. Write every gene symbol in a step in "
                    "this form" + (f" ({', '.join(ex)})" if ex else "") + ", and choose markers "
                    f"and gene sets for {gs['species_hint']}.")
            else:
                lines.append(f"Gene identifiers: Ensembl IDs ({gs.get('evidence')}), so the "
                             f"species is {gs['species_hint']}.")

        # NON-single-cell modalities. The block above renders cells/genes/obs, which a VCF has none
        # of — so a variant study's whole profile was the single line "Dataset profile:
        # vcf_variants.", while its dataset_result carried the sample count, the sample names, the
        # variant scale and the routing note. That is the ONLY description of the data the PI gets
        # when it plans, and 3 of the 4 production runs launched with a bare "complete the research"
        # were VCFs — planned, and teamed, against essentially nothing.
        if dr.get("n_samples") or dr.get("n_variants_sampled"):
            n_s, n_v = dr.get("n_samples"), dr.get("n_variants_sampled")
            bits: list[str] = []
            if n_s:
                names = [str(s) for s in (dr.get("samples") or [])][:6]
                bits.append(f"{n_s} sample(s)" + (f" [{', '.join(names)}]" if names else ""))
            if n_v:
                more = "+" if dr.get("variant_count_truncated") else ""
                bits.append(f"{n_v}{more} variants (counted from a bounded sample of the file)")
            if bits:
                lines.append("Variant data: " + "; ".join(bits))
        if dr.get("note"):
            lines.append(f"Note from the file inspection: {dr['note']}")
        # REPLICATION, stated as a number. Which test is legal for a condition contrast is decided
        # entirely by how many SAMPLES back each arm, and the profile never said — so a plan could
        # propose aggregating counts "per donor" against a dataset whose only library column held
        # one value, and nothing in what the planner was given contradicted it. Named columns with
        # their level counts, so "there is no replication here" is a fact on the page rather than
        # something the reader has to notice is absent.
        design_cols = {c: info for c, info in cats.items()
                       if isinstance(info, dict) and isinstance(info.get("n"), int)
                       and not _looks_like_celltype_col(c) and not _looks_like_qc_col(c)
                       and info["n"] <= 30}
        if design_cols:
            bits = [f"{c} ({info['n']} level{'s' if info['n'] != 1 else ''})"
                    for c, info in design_cols.items()]
            single = [c for c, info in design_cols.items() if info["n"] == 1]
            note = ("REPLICATION — non-annotation metadata columns and their level counts: "
                    + ", ".join(bits) + ". The unit of replication for a CONDITION contrast is the "
                    "SAMPLE (donor / animal / library), never the cell: identify which of these is "
                    "the sample column and which is the condition before choosing a test, and state "
                    "the count (\"n = 3 donors per arm\"), not the cell count.")
            if single:
                note += (f" Note that {', '.join(single)} hold(s) a SINGLE value, so "
                         "it cannot supply replication — if no column separates samples within an "
                         "arm, there is no biological replication in this dataset at all, no valid "
                         "p-value for the condition exists, and any comparison must be planned and "
                         "reported as DESCRIPTIVE (effect sizes and rankings only). Do not plan a "
                         "pseudobulk / per-donor aggregation step against data that has no donors.")
            lines.append(note)
        # PER-ARM DESIGN TABLE — cells per arm, cells per label per arm, and the arms' median depth
        # / detected genes / mito%. A two-arm study where one arm is sequenced 1.6x deeper (the
        # DDX41 object: 3,078 vs 1,916 median counts) shows "up-regulation" of ribosomal /
        # translation genes in EVERY cell type — a depth artifact a plan must anticipate (depth-
        # aware normalisation, or a per-cell-type depth-matched check) and a write-up must not
        # narrate as biology. This is the cheapest check there is, and nothing ever ran it.
        dba = dr.get("design_by_arm") if isinstance(dr.get("design_by_arm"), dict) else None
        if dba:
            arms = dba.get("cells_by_arm") or {}
            parts = ["DESIGN BY ARM — cells per arm: "
                     + ", ".join(f"{a}: {n:,}" for a, n in arms.items()) + "."]
            qc = dba.get("qc_median_by_arm") or {}
            if qc:
                parts.append("Per-arm medians of the QC columns already in the file: "
                             + "; ".join(f"{col} " + ", ".join(f"{a}={v}" for a, v in per.items())
                                         for col, per in qc.items()) + ".")
            lab = dba.get("cells_by_label_and_arm") or {}
            if lab:
                small = [label for label, per in lab.items() if min(per.values()) < 30]
                # IN THE FILE, before QC. A plan that quoted these as the tested counts ("Microglia
                # 60 / 24") handed pre-QC numbers down to the report (run f3731e0b7136 tested 57 / 21).
                parts.append(f"Cells per `{dba.get('label_column')}` per arm in the uploaded file, "
                             "BEFORE QC (QC removes cells, so a step's tool reports the counts it "
                             "actually tested; quote those, not these, for tested groups): "
                             + "; ".join(f"{label} " + "/".join(str(per[a]) for a in arms)
                                         for label, per in lab.items())
                             + f" (order {'/'.join(arms)})."
                             + (f" {len(small)} label(s) have <30 cells in an arm and cannot be "
                                f"tested per cell type: {', '.join(small)}." if small else ""))
            # The direction bookkeeping lives in run_depth_matched_de, not in the plan. This text
            # once told the PI that genes DOWN in the deeper arm were "the most credible set", and
            # the PI turned it into a step of its own that demoted EVERY up gene and filtered the
            # down genes on rho — the opposite of what the tool reports per direction, and a rule
            # that throws away a real effect running with the gradient (Gfap +6 in DDX41 Muller
            # glia). So: say what the tool already returns, and that no step should redo it.
            if dba.get("depth_imbalance"):
                parts.append("⚠ DEPTH IMBALANCE: " + str(dba["depth_imbalance"]) + " Plan for it "
                             "with ONE `run_depth_matched_de` step after the contrast, and treat a "
                             "same-direction shift across all cell types (especially ribosomal / "
                             "translation genes) as technical until that check says otherwise. "
                             "The two DIRECTIONS are not symmetric, and the tool already does the "
                             "bookkeeping per cell type: its summary table labels each direction "
                             "`with_depth` (UP in the deeper arm — what depth can manufacture) or "
                             "`against_depth`. With the gradient, its `depth_robust` genes kept "
                             "their sign and most of their Wilcoxon z after down-sampling and are "
                             "the credible ones; a real effect can run with the gradient, so do "
                             "NOT demote every up gene wholesale. Against the gradient, depth "
                             "cannot have manufactured the genes, but the check cannot confirm "
                             "them either (verdict `against_depth_untestable`, no rho rule "
                             "applies): they are unrefuted candidates, not a validated set. Do "
                             "not plan a separate step to re-derive directions, credible sets or "
                             "a rho filter from these tables — read them from the tool's output "
                             "where they are needed. Never write 'all shifts are depth artefacts' "
                             "without saying, per cell type, which direction ran with the "
                             "gradient and which against it.")
            if dba.get("snrna_hint"):
                parts.append("⚠ PROTOCOL: " + str(dba["snrna_hint"]))
            lines.append(" ".join(parts))
        # Loudly flag PRE-EXISTING cell-type annotation columns. The most common failure is the PI
        # de-novo clustering (leiden) and running DE/enrichment on numeric cluster labels while the
        # data already carries expert cell-type labels — the marker/enrichment output is then
        # biologically uninterpretable. Steer marker/enrichment analysis onto the annotation column.
        anno = [c for c in ({**cats, **{k: {} for k in (dr.get("obs_keys") or [])}}) if _looks_like_celltype_col(c)]
        # A "contrast" is a 2+-category obs column that is NOT a cell-type annotation — a real
        # experimental variable (condition, genotype, treatment, 2+ sample groups). Differential
        # expression and pathway enrichment only MEAN something against such a contrast; a known cell
        # type's own one-vs-rest identity markers do not constitute one.
        contrast_cols = [
            c for c, info in cats.items()
            if isinstance(info, dict) and isinstance(info.get("n"), int) and info["n"] >= 2
            and not _looks_like_celltype_col(c)
        ]
        if anno:
            def _label(c: str) -> str:
                info = cats.get(c) if isinstance(cats.get(c), dict) else {}
                n = info.get("n")
                return f"{c} ({n} labels)" if isinstance(n, int) else str(c)
            annotated = "⚑ This dataset is ALREADY annotated with cell types: " + ", ".join(_label(c) for c in anno)
            if contrast_cols:
                # BOTH slots, not one. This line used to offer only `groupby`, which is the same
                # slot a condition contrast needs — so when the question named a condition column
                # ("run_de with groupby=sampleid") the advice became unsatisfiable and the model
                # dropped the labels entirely rather than reaching for a parameter nothing had
                # mentioned. A condition contrast keeps the condition in `groupby` and puts the
                # labels in `stratify_by`; only a MARKER analysis puts the labels in `groupby`.
                # Stratify on the COARSEST label column. A fine-grained annotation (87 subtypes)
                # splits the arms into strata too thin to test and buries the result in skips; the
                # major-class column is the one a per-cell-type contrast is actually run on.
                def _levels(c: str) -> int:
                    info = cats.get(c) if isinstance(cats.get(c), dict) else {}
                    n = info.get("n")
                    return n if isinstance(n, int) else 10 ** 6
                coarsest = min(anno, key=_levels)
                lines.append(
                    annotated
                    + ". Use these labels — never de-novo leiden cluster numbers, which are not "
                    "interpretable on their own — in whichever slot the analysis calls for: for a "
                    f"CONDITION contrast keep the condition column in `groupby` and pass "
                    f"stratify_by=\"{coarsest}\" so the comparison runs separately within each cell "
                    "type (pooling cell types makes a shift in composition look like differential "
                    "expression); for MARKER analysis pass the labels as `groupby` itself (e.g. "
                    f"groupby=\"{coarsest}\"). Clustering + UMAP is still fine for showing structure."
                )
            else:
                # Already annotated AND no experimental contrast (single sample/condition): there is no
                # differential question. Pathway/GO enrichment on a known cell type's own identity markers
                # is circular — it only restates the cell type's definition (rod markers → phototransduction)
                # and yields no finding. Steer the plan away from enrichment / discovery-DE and cap it at
                # QC + annotation validation + a descriptive summary. This is the single-annotated-sample
                # case a reviewer flags as meaningless enrichment.
                lines.append(
                    annotated + ", and there is NO experimental contrast — every non-annotation metadata "
                    "column holds a single value (one sample / one condition). No differential comparison is "
                    "possible, so do NOT plan pathway/GO enrichment or discovery-style differential "
                    "expression: enriching a known cell type's identity markers is circular and yields no "
                    "finding without a condition to compare against. Keep the plan to QC, VALIDATING the "
                    "existing annotation (canonical-marker check + UMAP), and a concise descriptive summary. "
                    "UMAP for visualization is fine, but de-novo re-clustering to re-derive the cell types is "
                    "redundant here."
                )
        bound = self._bound_files_context()
        if bound:
            lines.append(bound)
        return "\n".join(lines)

    def _bound_files_context(self) -> str:
        """The attached files (below), what their Croissant metadata says (``_metadata_context``),
        the primary's DATASET.md (``_description_context``), and the attached datasets the run
        cannot use (``_unusable_inputs_context``)."""
        parts = [self._attached_files_context(), self._metadata_context(),
                 self._description_context(), self._unusable_inputs_context()]
        return "\n".join(p for p in parts if p)

    def _description_context(self) -> str:
        """The primary dataset's DATASET.md for the planner, '' when it has none. Its statements
        read from the data are settled, so a plan need not spend a step finding them out again."""
        dd = (self.ctx.decisions or {}).get("dataset_description")
        if not isinstance(dd, dict) or not dd.get("markdown"):
            return ""
        whose = ("written by the PI and edited by its owner (the owner's text wins over your own "
                 "reading, unless the data contradicts it: then say so in the plan)"
                 if dd.get("state") == "owner" else
                 "written by the PI the first time a run read this dataset")
        text = str(dd["markdown"])
        if len(text) > _DATASET_MD_CHARS:
            text = text[:_DATASET_MD_CHARS] + "\n[... cut; the full description is in data/DATASET.md]"
        return (f"DATASET DESCRIPTION (DATASET.md, {whose}). What it marks as read from the data is "
                "established: do not plan a step whose only purpose is to find it out again (the "
                "shape, the columns and their levels, the species, the files). What it marks as "
                "inferred is a reading to confirm where it matters.\n" + text)

    def _describe_dataset(self, emit: EventFn) -> None:
        """Read the primary dataset's DATASET.md, or write it the first time a run reads the dataset.

        The gateway puts the dataset's kept description, or the facts to write one from, in
        ``decisions["dataset_description"]`` (only for one of the account's datasets). A kept
        description that still matches the data is read as it is: the PI does not describe the
        dataset again, and an owner's edit is never overwritten. Otherwise the PI makes one model
        call over the run-start profile; every number in the result is read from the data
        (``metadata/description.py``), and the gateway keeps it with the dataset
        (``dataset_described``). The description goes into the run's bundle as data/DATASET.md.
        Never raises: the plan does not depend on it."""
        from ...metadata import description as dmd
        decisions = self.ctx.decisions if self.ctx.decisions is not None else {}
        dd = decisions.get("dataset_description")
        if not isinstance(dd, dict) or not dd.get("uuid"):
            return
        name = str(dd.get("name") or "dataset")
        try:
            kept = dd.get("state") in ("pi", "owner") and dd.get("markdown")
            if kept and (dd.get("state") == "owner" or not dd.get("stale")):
                emit({"type": "dataset_description_used", "uuid": dd["uuid"], "name": name,
                      "state": dd.get("state")})
                self._write_dataset_md(str(dd["markdown"]))
                return
            dr = decisions.get("dataset_result")
            if not isinstance(dr, dict) or self._complete_fn is None:
                return
            facts = dd.get("facts") if isinstance(dd.get("facts"), dict) else {}
            user_fields = dd.get("user_fields") if isinstance(dd.get("user_fields"), dict) else {}
            profile = dmd.profile_snapshot(dr)
            emit({"type": "dataset_describing", "name": name})
            raw = self._complete(dmd.describe_messages(name, profile, facts, user_fields), role="plan")
            inferred = dmd.parse_inferred(raw or "")
            written_at = _dt_now_iso()
            run_id = str(dd.get("run_id") or "")
            model = str(getattr(self.ctx, "model", "") or "")
            desc = dmd.build(name=name, profile=profile, facts=facts, user_fields=user_fields,
                             inferred=inferred, model=model, written_at=written_at, run_id=run_id)
            markdown = dmd.render(desc)
            emit({"type": "dataset_described", "uuid": dd["uuid"], "name": name, "profile": profile,
                  "inferred": inferred, "model": model, "written_at": written_at, "run_id": run_id,
                  "dropped": len(desc.get("dropped") or []), "markdown": markdown})
            if dd.get("state") != "owner":                 # the owner's edit stays what is read
                dd.update(state="pi", markdown=markdown, stale=False)
            self._write_dataset_md(str(dd.get("markdown") or markdown))
        except Exception as exc:  # noqa: BLE001 - the description is an extra; the run goes on
            emit({"type": "dataset_describe_failed", "error": f"{type(exc).__name__}: {exc}"[:200]})

    def _write_dataset_md(self, markdown: str) -> None:
        """The description the run read, in its bundle (data/DATASET.md), for the report to cite."""
        if self.ctx.workspace is None or not markdown:
            return
        try:
            data = Path(self.ctx.workspace) / "artifacts" / "data"
            data.mkdir(parents=True, exist_ok=True)
            (data / "DATASET.md").write_text(markdown, encoding="utf-8")
        except OSError:
            pass

    def _unusable_inputs_context(self) -> str:
        """The attached datasets the run start could not use, each with the reason, or ``''``. Told
        to the planner so the plan names them instead of quietly planning around them: a plan for
        "these two samples" was scoped to one, with the second folder taken for a stray file
        (RCHENLAB/AiScientist issue 22)."""
        rows = [r for r in ((self.ctx.decisions or {}).get("unusable_inputs") or [])
                if isinstance(r, dict) and r.get("name")]
        if not rows:
            return ""
        lines = ["⚠ ATTACHED BUT NOT USABLE (say so in the plan, by name and with the reason; do not "
                 "drop them silently or plan as if they were not attached):"]
        lines += [f"  - {r['name']}: {r.get('reason') or 'not usable'}" for r in rows[:12]]
        return "\n".join(lines)

    def _metadata_context(self) -> str:
        """What the bound datasets' Croissant metadata says (RCHENLAB/AiScientist issue 9): the
        owner's description, organism / tissue / assay terms, source accessions and citation. Each
        dataset is marked as reviewed by its owner or drafted automatically, so the planner treats
        a drafted organism as a guess to check against the data, not as a fact. ``''`` without any."""
        notes = [n for n in ((self.ctx.decisions or {}).get("dataset_metadata") or []) if isinstance(n, dict)]
        if not notes:
            return ""
        lines = ["Dataset metadata (Croissant; cite inputs by name and UUID, and accession when given):"]
        for n in notes[:8]:
            status = "reviewed by its owner" if n.get("reviewed") else "drafted automatically, not reviewed"
            bits = [f"  - {n.get('name')} (UUID {n.get('uuid')}; {status})"]
            if n.get("description"):
                bits.append(f"    description: {' '.join(str(n['description']).split())[:400]}")
            terms = [*(n.get("about") or []), *(n.get("assay") or [])]
            if terms:
                src = "owner" if n.get("terms_source") == "owner" else "drafted — verify against the data"
                bits.append(f"    organism/tissue/assay: {', '.join(map(str, terms))} ({src})")
            if n.get("accessions"):
                bits.append(f"    accessions: {', '.join(map(str, n['accessions']))}")
            if n.get("citation"):
                bits.append(f"    cite as: {str(n['citation'])[:300]}")
            if n.get("record_sets"):
                bits.append(f"    tables: {', '.join(map(str, n['record_sets']))}")
            lines.extend(bits)
        return "\n".join(lines)

    def _attached_files_context(self) -> str:
        """The run's OTHER attached files (feature ②) with the role the user gave each, or ``''``
        for a single-file run. The profile above describes only the primary; without this the
        planner never learned that a second upload — a reference atlas, say — existed at all."""
        shared = self._shared_inputs_context()
        records = [r for r in ((self.ctx.decisions or {}).get("datasets") or [])
                   if isinstance(r, dict) and r.get("name")]
        if len(records) <= 1:
            return shared
        layout = (self.ctx.decisions or {}).get("cellranger_layout")
        together = {str(d) for d in ((layout or {}).get("datasets") or [])} if isinstance(layout, dict) else set()
        lines = ["Attached files (the profile above describes the PRIMARY only; run_code reads the "
                 "others through AISCIENTIST_INPUTS, and the other tools read only the primary"
                 + ("; `run_cellqc` reads the Cell Ranger libraries of every attached folder "
                    "marked SAMPLE FOLDER, with the primary's" if len(together) > 1 else "") + "):"]
        for r in records:
            role = str(r.get("role") or "").strip()
            name = str(r.get("name") or "").rstrip("/")
            label = ("PRIMARY" if r.get("primary") else
                     "SAMPLE FOLDER" if name in together else
                     role.upper() if role else
                     "extra folder" if r.get("kind") == "folder" else "extra file")
            desc = r.get("description") if isinstance(r.get("description"), dict) else {}
            gist = str(desc.get("one_line_summary") or desc.get("file_kind") or "").strip()
            lines.append(f"  - {r['name']} — {label}" + (f": {gist}" if gist else ""))
        refs = [str(r["name"]) for r in records
                if not r.get("primary") and str(r.get("role") or "").strip().lower() == "reference"]
        if refs:
            lines.append(
                f"REFERENCE — the user marked {', '.join(refs)} as reference data (e.g. a cell-type "
                "atlas): plan to use it as comparison evidence for the primary dataset (through "
                "run_code, AISCIENTIST_REFERENCE), never as the study's own data — do not QC, "
                "cluster or test it as if it were.")
        return "\n".join(lines + ([shared] if shared else []))

    def _shared_inputs_context(self) -> str:
        """The shared datasets attached to the run (issue 100): the version each means for this run and,
        for every file, its tissue, modality, cells, genes and obs columns, so the plan picks the right
        file and sizes its memory. ``''`` when none is attached."""
        rows = [r for r in ((self.ctx.decisions or {}).get("shared_inputs") or []) if isinstance(r, dict)]
        if not rows:
            return ""
        lines = ["Shared datasets (the lab's, read in place, never copied into the account's storage):"]
        big = False
        for r in rows:
            requested = str(r.get("requested") or "latest")
            lines.append(f"  - {r.get('title') or r.get('dataset')} {r.get('version')}"
                         + (" (latest at the start of this run)" if requested == "latest" else " (pinned)"))
            for f in (r.get("files") or [])[:40]:
                fac = f.get("facets") if isinstance(f.get("facets"), dict) else {}
                bits = [str(fac[k]) for k in ("tissue", "modality") if fac.get(k)]
                if fac.get("n_obs") is not None:
                    bits.append(f"{int(fac['n_obs']):,} cells")
                if fac.get("n_vars") is not None:
                    bits.append(f"{int(fac['n_vars']):,} genes")
                cols = fac.get("obs_columns")
                if isinstance(cols, list) and cols:
                    bits.append("obs: " + ", ".join(str(c) for c in cols[:12]) + (" …" if len(cols) > 12 else ""))
                lines.append(f"      {f.get('name')}" + (f" — {'; '.join(bits)}" if bits else ""))
                nm = str(f.get("name") or "").lower()
                big = big or ("hrca" in nm and "snrna" in nm)
            if len(r.get("files") or []) > 40:
                lines.append(f"      … and {len(r['files']) - 40} more files")
        if big:
            lines.append("Memory: loading all of HRCA snRNA (3.18M nuclei) into memory peaks at about "
                         "218 GiB. Subset first or read it backed, and size the job's memory to match.")
        return "\n".join(lines)

    def _answer_plan_question(self, question: str, agenda: list[str], asked: str) -> str:
        """Answer a reviewer's question ABOUT the plan, changing nothing.

        Given the same material the PI planned from — the plan, the research-path guidance and the
        dataset profile — so the answer is grounded in this study rather than in generalities. Any
        failure returns a short honest string: a question that cannot be answered must not take the
        plan down with it."""
        parts = [f"Research question:\n{question}\n",
                 "The plan you proposed:\n"
                 + "\n".join(f"{i}. {s}" for i, s in enumerate(agenda, 1)) + "\n"]
        if self._plan_doc is not None:
            parts.append("The current plan document and its settings:\n" + self._plan_doc.to_yaml())
        if self._guidance:
            parts.append(f"The research-path guidance you followed:\n{self._guidance}\n")
        ctx = self._dataset_context()
        if ctx:
            parts.append(ctx + "\n")
        parts.append(f"The researcher asks:\n{asked}")
        try:
            out = (self._complete([{"role": "system", "content": _PLAN_QA_SYSTEM},
                                   {"role": "user", "content": "\n".join(parts)}], role="plan") or "").strip()
        except Exception as exc:  # noqa: BLE001 - never lose the plan over a question
            return f"(could not answer just now: {type(exc).__name__})"
        return out or "(no answer came back — ask again, or tell me what to change instead.)"

    # -- the plan as a document (config.plan_yaml; agents/plan_doc.py) -----------------------------

    def _present_plan_document(self, question: str, agenda: "list[str]", emit: EventFn) -> None:
        """Bring the plan document up to date with ``agenda`` and announce it (``plan_doc``).

        A step whose text did not change keeps its id and settings, so the binding model call
        below is made only for steps that are new since the last card — once for a first draft,
        and for the one rewritten step after a revision."""
        if not self.config.plan_yaml:
            return
        doc, fresh = sync_plan(self._plan_doc, list(agenda), request=question,
                               tool_of=_primary_tool, runs_on_of=_tool_runs_on)
        if fresh:
            doc = self._bind_plan_settings(doc, fresh, emit)
            doc = self._assign_plan_skills(doc, fresh, emit)
        self._plan_doc = doc
        emit({"type": "plan_doc", "yaml": doc.to_yaml(), "steps": self._public_plan_steps(doc)})

    def _expected_outputs(self, step: str) -> "tuple[list[skill_outputs.DeclaredOutput], list[str]]":
        """What ``step`` should leave under the artifacts directory, and where that comes from: the
        outputs its assigned skill declares, and those an active pipeline declares for the tool the
        step runs. ``([], [])`` for a step with neither, which is then not checked."""
        plan = self._approved_plan or self._plan_doc
        found = plan.step_for(step) if plan is not None else None
        expected: list[skill_outputs.DeclaredOutput] = []
        sources: list[str] = []
        skill = get_skill(found.skill) if found is not None and found.skill else None
        if skill is not None and skill.outputs:
            expected.extend(skill.outputs)
            sources.append(f"skill {skill.name} {skill.version or 'local'}".strip())
        tool = found.tool if found is not None else _primary_tool(step)
        for pipeline in self._skills:
            mine = [o for o in skill_outputs.for_tool(pipeline.outputs, tool)
                    if all(o.path != e.path for e in expected)]
            if mine:
                expected.extend(mine)
                sources.append(f"pipeline {pipeline.key}")
        return expected, sources

    def _public_plan_steps(self, doc: PlanDoc) -> "list[dict[str, Any]]":
        """The plan document's steps for the console, each with the outputs it is expected to leave
        (its skill's, and its tool's under an active pipeline), so the researcher sees what a step
        will give them before approving the plan."""
        steps = doc.to_public()
        for public, step in zip(steps, doc.steps):
            expected, _ = self._expected_outputs(step.text)
            if expected:
                public["outputs"] = [o.to_public() for o in expected]
            # The QC cutoffs the step's tool takes, with their value, unit and whether the researcher
            # changed them from the default — shown on the card, set by a message (issue 61).
            schema = _tool_schemas(list(getattr(self.scientist, "catalog", []))).get(str(step.tool or ""))
            rows = cutoff_rows(step.settings, schema or {}) if schema else []
            if rows:
                public["cutoffs"] = rows
        return steps

    def _assign_plan_skills(self, doc: PlanDoc, fresh: "list[int]", emit: EventFn) -> PlanDoc:
        """Name the skill each new code step uses, so a vetted skill is used without the researcher
        having to know it exists.

        Only steps that write code are matched (no tool, or a code tool): a pipeline tool is the
        step, and a literature step searches papers. A step whose text names a skill (an old name of
        a migrated skill too) gets it as written; the rest go to one model call that reads the
        skills' descriptions, with the skills the research path names for this study as hints. Every
        name is resolved against the library and an unknown one is dropped. The model's match goes to
        one step per skill, the first in plan order: a later step that uses what that step produced
        does not run the skill again, unless its own text names it. No model, or a reply that
        does not parse, leaves the steps as they are: the plan is never held up by this. The skill is
        shown on the plan card and the researcher can take it off."""
        refresh_skills()
        hidden = superseded_names(ATOMIC_SKILLS)
        lib = {n: s for n, s in list(ATOMIC_SKILLS.items()) if n not in hidden}
        steps = list(doc.steps)
        candidates = [i for i in fresh if 0 <= i < len(steps) and not steps[i].skill
                      and (steps[i].tool is None or steps[i].tool in CODE_TOOLS)
                      and not _is_literature_step(steps[i].text)]
        if not lib or not candidates:
            return doc
        assigned: dict[int, str] = {}
        for i in candidates:
            named = skills_named_in(steps[i].text, lib)
            if named:
                assigned[i] = named[0]
        named_in_text = set(assigned)
        ask = [i for i in candidates if i not in assigned]
        if ask and self._complete_fn is not None:
            emit({"type": "plan_skill_matching", "steps": len(ask)})
            hints = skills_named_in(self._guidance or "", lib)
            listing = "\n\n".join(f"{i + 1}. {steps[i].text}" for i in ask)
            catalog = "\n".join(
                f"- {s.name}: " + " ".join((s.summary or "").split())[:600] for s in lib.values())
            prompt = (f"Steps:\n\n{listing}\n\nSkills:\n{catalog}\n\n"
                      + (f"The research path for this study names: {', '.join(hints)}.\n\n" if hints else "")
                      + "Return the JSON array now.")
            try:
                raw = self._complete([{"role": "system", "content": _PLAN_SKILLS_SYSTEM},
                                      {"role": "user", "content": prompt}], role="plan")
                parsed = safe_json_loads(raw)
                if not isinstance(parsed, list):
                    m = re.search(r"\[.*\]", raw or "", re.DOTALL)
                    parsed = json.loads(m.group(0)) if m else []
                for item in parsed if isinstance(parsed, list) else []:
                    if not isinstance(item, dict) or not item.get("skill"):
                        continue
                    try:
                        idx = int(item.get("step", 0)) - 1
                    except (TypeError, ValueError):
                        continue
                    hit = get_skill(str(item["skill"]).strip(), lib)
                    if idx in ask and hit is not None:
                        assigned[idx] = hit.name
            except Exception as exc:  # noqa: BLE001 - skills are an addition; the plan stands without them
                emit({"type": "plan_skill_match_failed", "error": f"{type(exc).__name__}: {exc}"[:200]})
        # One step per skill. In run 74873590ca7b the model gave the scoring skill to the step that
        # scored and to the two after it, which only compared and plotted the scores: each ran under
        # the skill, and the figures step's Critic asked why it had not scored again. So a model's
        # match is dropped when another step of the plan already has that skill (one matched earlier
        # in plan order, or one the plan had before). A step whose own text names a skill keeps it.
        held = {s.skill: j for j, s in enumerate(steps) if s.skill}
        repeats: list[dict[str, Any]] = []
        for i in sorted(assigned):
            name = assigned[i]
            if i not in named_in_text and name in held:
                repeats.append({"step": i + 1, "skill": name, "kept_on": held[name] + 1})
                del assigned[i]
            else:
                held.setdefault(name, i)
        if repeats:
            emit({"type": "plan_skill_repeats", "dropped": repeats})   # feed only: why a step has none
        if not assigned:
            return doc
        for i, name in assigned.items():
            s = steps[i]
            steps[i] = PlanStep(id=s.id, text=s.text, tool=s.tool, settings=s.settings,
                                runs_on=s.runs_on, skill=name)
        emit({"type": "plan_skills", "assigned": [
            {"step": i + 1, "id": steps[i].id, "skill": name,
             "version": lib[name].version or "local", "release": lib[name].registry_release or ""}
            for i, name in sorted(assigned.items())]})
        return PlanDoc(request=doc.request, steps=tuple(steps))

    def _bind_plan_settings(self, doc: PlanDoc, fresh: "list[int]", emit: EventFn) -> PlanDoc:
        """Fill in ``settings`` for the steps at ``fresh``: the tool arguments their text states.

        Two sources propose, one rule admits. A call written in the step
        (``run_de(groupby="sampleid")``) is read directly; the model is asked for the rest
        (``_PLAN_BIND_SYSTEM``). Either way ``bind_settings`` keeps an argument only when the tool
        declares it and the step's own text states the value. No model, or a reply that does not
        parse, leaves the call-syntax settings — the plan is never held up by this."""
        schemas = _tool_schemas(list(getattr(self.scientist, "catalog", [])))
        steps = list(doc.steps)
        # A code step has no settings: the Scientist writes its code, and a plan that pinned an argument
        # of run_code (a snippet, or the skill it uses — `skill:` says that) would hand the step a value
        # nobody approved as one. The same rule as a revision (_revise_plan_document).
        askable = [i for i in fresh if steps[i].tool and steps[i].tool not in CODE_TOOLS
                   and (schemas.get(steps[i].tool) or isinstance(schemas.get(steps[i].tool), PlanParameters))]
        if not askable:
            return doc
        proposed: dict[int, dict[str, Any]] = {
            i: call_settings(steps[i].text, str(steps[i].tool),
                             json_literals=isinstance(schemas[steps[i].tool], PlanParameters)) for i in askable}
        if self._complete_fn is not None:
            emit({"type": "plan_binding", "steps": len(askable)})   # a model call: say what the wait is
            listing = "\n\n".join(
                f"{i + 1}. {steps[i].text}\n   Tool `{steps[i].tool}` parameters: "
                + "; ".join(_describe_param(k, v) for k, v in schemas[str(steps[i].tool)].items()
                            if isinstance(v, dict))
                for i in askable)
            try:
                raw = self._complete([
                    {"role": "system", "content": _PLAN_BIND_SYSTEM},
                    {"role": "user", "content": f"Steps:\n\n{listing}\n\nReturn the JSON array now."},
                ], role="plan")
                parsed = safe_json_loads(raw)
                if not isinstance(parsed, list):
                    m = re.search(r"\[.*\]", raw or "", re.DOTALL)
                    parsed = json.loads(m.group(0)) if m else []
                for item in parsed if isinstance(parsed, list) else []:
                    if not isinstance(item, dict) or not isinstance(item.get("settings"), dict):
                        continue
                    try:
                        idx = int(item.get("step", 0)) - 1
                    except (TypeError, ValueError):
                        continue
                    if idx in proposed:
                        proposed[idx] = {**proposed[idx], **item["settings"]}
            except Exception as exc:  # noqa: BLE001 - settings are an addition; the plan stands without them
                emit({"type": "plan_bind_failed", "error": f"{type(exc).__name__}: {exc}"[:200]})
        dropped: list[dict[str, Any]] = []
        for i in askable:
            settings, refused = bind_settings(steps[i].text, schemas[str(steps[i].tool)], proposed[i])
            steps[i] = PlanStep(id=steps[i].id, text=steps[i].text, tool=steps[i].tool,
                                settings=settings, runs_on=steps[i].runs_on, skill=steps[i].skill)
            dropped += [{"step": i + 1, "tool": steps[i].tool, **d} for d in refused]
        if dropped:
            # Feed only: what the binder proposed and the step's text did not support.
            emit({"type": "plan_bind_dropped", "dropped": dropped})
        return PlanDoc(request=doc.request, steps=tuple(steps))

    def _apply_plan_edit(self, question: str, text: str, emit: EventFn, *,
                         with_reply: bool = False) -> "tuple[list[str], list[dict[str, Any]]] | None":
        """Adopt the plan document the researcher edited: ``(new agenda, changes)``, or ``None`` if
        refused.

        Refused — with every reason, and the plan left exactly as it was — when the YAML does not
        parse, a step names a tool this deployment does not have, or a setting is not one the tool
        takes. A value nobody can run is better stopped here than discovered as a failed job.

        ``with_reply``: the edit came with a reply in words, which is made on the edited plan. A
        refusal then also says the reply was not sent, and an edit that changes nothing is no
        refusal (the reply goes ahead on the plan as it is)."""
        def refuse(problems: "list[str]") -> None:
            if with_reply:
                problems = [*problems, "Your message was not sent to the PI either: fix or reset "
                                       "this edit, then send it again."]
            emit({"type": "plan_edit_rejected", "problems": problems})

        if not self.config.plan_yaml or self._plan_doc is None:
            refuse(["Editing the plan as a document is not enabled on this server."])
            return None
        try:
            edited = parse_plan_yaml(text, request=question)
        except PlanError as exc:
            refuse(list(exc.problems))
            return None
        # A step the researcher added without naming its tool still says which one in its text.
        known = {s.id for s in self._plan_doc.steps}
        edited = PlanDoc(request=question, steps=tuple(
            PlanStep(id=s.id, text=s.text, tool=_primary_tool(s.text), settings=s.settings, skill=s.skill)
            if s.id not in known and not s.tool else s for s in edited.steps))
        edited, findings = check_plan(
            edited, _tool_schemas(list(getattr(self.scientist, "catalog", []))),
            is_known=lambda name: get_skill(name) is not None, skill_name=_skill_name)
        if findings:
            refuse([_PLAN_FINDING_WORDS.get(f["kind"], "{kind}").format(**f) + "." for f in findings])
            return None
        doc, changes, notes = apply_edit(self._plan_doc, edited)
        if not changes:
            if with_reply:
                return self._plan_doc.agenda(), []
            refuse(["The edited plan is the same as the current one."])
            return None
        self._plan_doc = doc
        emit({"type": "plan_edited", "changes": changes, "notes": notes,
              "steps": sorted({c["step"] for c in changes if c["kind"] != "removed"})})
        return doc.agenda(), changes

    def _cutoff_request(self, request: str) -> "tuple[dict[str, Any], list[str], bool]":
        """``(cutoffs, problems, only)`` for a reply that names CellQC cutoffs (issue 61).

        ``cutoffs`` are the settings read for certain ("raise the mitochondrial limit to 20") that a
        step of this plan takes; they are set by code on every such step, whatever else the reply
        asks, so the number typed is the number that runs. ``only`` is true when the reply asks for
        nothing else: it is then applied without the model. A cutoff no step takes ("minimum genes"
        on a ``run_scanpy_qc`` plan, whose setting is ``min_genes``) is left to the model with the
        rest of the reply. ``problems`` are explained back and nothing changes: a value out of range,
        a cutoff named without its value or by how much to change it, a number that contradicts
        "raise" or "lower", or a cutoff asked for without saying which."""
        reading = read_cutoffs(request)
        if not reading.changes and not reading.problems:
            return {}, [], False
        doc = self._plan_doc
        if doc is None or not self.config.plan_yaml:
            return {}, [], False
        schemas = _tool_schemas(list(getattr(self.scientist, "catalog", [])))
        takers = {name: [s for s in doc.steps if s.tool and name in (schemas.get(s.tool) or {})]
                  for name in CUTOFF_SETTINGS}
        taken = [name for name, steps in takers.items() if steps]
        if not taken:
            return {}, [], False    # no step takes a CellQC cutoff: the words are for the other ways to revise
        problems: list[str] = []
        for setting, why in reading.problems:
            if setting is None:
                problems.append(why + " This plan's QC step takes " + ", ".join(
                    f"{n} ({CUTOFF_SETTINGS[n].label})" for n in taken) + ".")
            elif setting in taken:
                problems.append(why)
        cutoffs = {name: value for name, value in reading.changes.items() if name in taken}
        for name, value in cutoffs.items():
            spec, step = CUTOFF_SETTINGS[name], takers[name][0]
            prop = (schemas.get(step.tool) or {}).get(name)
            current = step.settings.get(name, prop.get("default", spec.fallback) if isinstance(prop, dict)
                                        else spec.fallback)
            way = reading.directions.get(name)
            if isinstance(current, (int, float)) and (
                    (way == "up" and value < current) or (way == "down" and value > current)):
                unit = "%" if spec.unit == "%" else f" {spec.unit}"
                problems.append(
                    f"You asked to {'raise' if way == 'up' else 'lower'} {name}, but {value:g}{unit} is "
                    f"{'below' if way == 'up' else 'above'} the plan's {current:g}{unit}. Say the value to use.")
        if problems:
            return {}, problems, False
        return cutoffs, [], reading.only and set(reading.changes) <= set(cutoffs)

    def _cutoff_ops(self, cutoffs: "dict[str, Any]") -> "list[dict[str, Any]]":
        """Operations that set ``cutoffs`` on every step of the plan whose tool takes them, leaving out
        a step that already has the value."""
        doc = self._plan_doc
        if doc is None or not cutoffs:
            return []
        schemas = _tool_schemas(list(getattr(self.scientist, "catalog", [])))
        ops: list[dict[str, Any]] = []
        for step in doc.steps:
            props = schemas.get(step.tool or "") or {}
            change = {name: value for name, value in cutoffs.items()
                      if name in props and step.settings.get(name) != value}
            if change:
                ops.append({"op": "change", "step": step.id, "settings": change})
        return ops

    def _pin_cutoffs(self, ops: "list[dict[str, Any]]", pinned: "dict[str, Any]") -> "list[dict[str, Any]]":
        """The model's operations with the stated cutoffs taking precedence: a value it gave for one
        of them is dropped, and the stated value is set on every step that takes it."""
        out: list[dict[str, Any]] = []
        for op in ops:
            settings = op.get("settings") if isinstance(op, dict) else None
            if isinstance(settings, dict) and set(settings) & set(pinned):
                op = {**op, "settings": {k: v for k, v in settings.items() if k not in pinned}}
                if str(op.get("op") or "").lower() == "change" and not op["settings"] \
                        and not (set(op) - {"op", "step", "settings"}):
                    continue    # it only set a pinned cutoff
            out.append(op)
        return out + self._cutoff_ops(pinned)

    def _revise_plan_document(self, question: str, request: str, emit: EventFn,
                              ops: "list[dict[str, Any]] | None" = None,
                              pinned: "dict[str, Any] | None" = None) -> "list[str] | None":
        """Make the change the researcher asked for in words ON the plan document; the new agenda,
        or ``None`` to let the caller revise the old way (single-step patch, then redraft).

        The model is shown the document and returns operations on it (``_PLAN_REVISE_SYSTEM``); this
        code applies them and then treats the result exactly like a hand edit — checked against the
        tool schemas, adopted through ``apply_edit`` — so a step no operation names is the same
        object afterwards, also when a step is added, removed or moved. ``None`` whenever that
        cannot be done cleanly (no document, no model, a reply that is not operations, operations
        that do not apply or name a tool or setting that does not exist, or no change at all): the
        plan is then never worse off than it was before this path existed.

        ``ops`` given are applied without the model. ``pinned`` settings (CellQC cutoffs the reply
        stated) win over the model's: they are set on every step that takes them, whatever value the
        operations gave."""
        if not self.config.plan_yaml or self._plan_doc is None or not request.strip() \
                or (self._complete_fn is None and not ops):
            return None
        before = self._plan_doc
        schemas = _tool_schemas(list(getattr(self.scientist, "catalog", [])))
        tools = "\n".join(
            f"- {name}: " + (", ".join(k for k, v in props.items() if isinstance(v, dict)) or "(no settings)")
            for name, props in schemas.items() if name != "finish")
        if not ops:
            try:
                raw = self._complete([
                    {"role": "system", "content": _PLAN_REVISE_SYSTEM},
                    {"role": "user", "content": f"Plan:\n```yaml\n{before.to_yaml().strip()}\n```\n\n"
                                                f"Tools (name: the settings it takes):\n{tools}\n\n"
                                                f"Requested change:\n{request}"},
                ], role="plan")
            except Exception as exc:  # noqa: BLE001 - a failed revision call is not a failed revision
                emit({"type": "plan_revise_skipped", "why": f"{type(exc).__name__}: {exc}"[:200]})
                return None
            ops = parse_plan_ops(raw)
        if ops is None:
            emit({"type": "plan_revise_skipped", "why": "the reply held no operations"})
            return None
        if pinned:
            if not ops:
                # The reply asked for more than its cutoffs and the model named nothing to change:
                # setting only the cutoffs would drop the rest without a word. The caller revises
                # the old way and sets the cutoffs on the plan that gives.
                emit({"type": "plan_revise_skipped", "why": "the reply held no operations"})
                return None
            ops = self._pin_cutoffs(ops, pinned)
        try:
            edited = apply_ops(before, ops)
        except PlanError as exc:
            emit({"type": "plan_revise_skipped", "why": "; ".join(exc.problems)[:300]})
            return None
        # What the operations said about settings. A step the model reworded or added WITHOUT
        # giving settings gets them the way a drafted step does: the binder reads its new sentence,
        # so "cluster at resolution 0.8" written into the prose does not leave 0.5 pinned under it.
        # A step it gave settings for keeps those, and keeps an older setting only while the new
        # sentence still states its value.
        old_by_id = {s.id: s for s in before.steps}
        numbered = {str(i): s.id for i, s in enumerate(before.steps, 1)}
        explicit: dict[str, set[str]] = {}
        for o in ops:
            if str(o.get("op") or "").lower() == "change" and isinstance(o.get("settings"), dict):
                ref = str(o.get("step") or "").strip()
                sid = ref if ref in old_by_id else numbered.get(ref.lstrip("#"), ref)
                explicit.setdefault(sid, set()).update(str(k) for k in o["settings"])
        steps: list[PlanStep] = []
        rebind: list[int] = []
        for i, step in enumerate(edited.steps):
            old = old_by_id.get(step.id)
            tool = step.tool or (_primary_tool(step.text) if old is None else None)
            settings = dict(step.settings)
            mine = explicit.get(step.id, set())
            if old is not None and tool != old.tool and tool and schemas.get(tool) is not None:
                # Another tool: the old tool's arguments mean nothing to it.
                settings = {k: v for k, v in settings.items() if k in mine or k in schemas[tool]}
            reworded = old is not None and step.text != old.text
            if tool in CODE_TOOLS:
                # The Scientist writes a code step's code; a plan that pins a snippet as its "setting"
                # would hand it a program nobody reviewed as one (seen on the served model, 2026-10-14).
                settings = {}
            elif (reworded and not mine) or (old is None and not settings):
                rebind.append(i)
            elif reworded and tool and schemas.get(tool):
                carried = {k: v for k, v in settings.items() if k not in mine}
                stated_before, _ = bind_settings(old.text, schemas[tool], carried)
                stated_now, _ = bind_settings(step.text, schemas[tool], carried)
                settings = {k: v for k, v in settings.items()
                            if not (k in stated_before and k not in stated_now)}
            steps.append(step if (tool == step.tool and settings == step.settings) else PlanStep(
                id=step.id, text=step.text, tool=tool, settings=settings, runs_on=step.runs_on,
                skill=step.skill))
        edited, findings = check_plan(PlanDoc(request=question, steps=tuple(steps)), schemas,
                                      is_known=lambda name: get_skill(name) is not None,
                                      skill_name=_skill_name)
        if findings:
            emit({"type": "plan_revise_skipped", "why": "; ".join(
                _PLAN_FINDING_WORDS.get(f["kind"], "{kind}").format(**f) for f in findings)[:300]})
            return None
        doc, changes, notes = apply_edit(before, edited)
        if not changes:
            emit({"type": "plan_revise_skipped", "why": "the operations changed nothing"})
            return None
        if rebind:
            doc = self._bind_plan_settings(doc, rebind, emit)
            # What apply_edit said about those steps' old settings no longer holds: they were read again.
            ids = {doc.steps[i].id for i in rebind}
            notes = [n for n in notes if n.get("id") not in ids]
            # …and what reading them changed is part of what the revision did.
            for i in rebind:
                final = doc.steps[i]
                was = old_by_id[final.id].settings if final.id in old_by_id else {}
                for key in sorted(set(was) | set(final.settings)):
                    if was.get(key) != final.settings.get(key) and not any(
                            c.get("kind") == "setting" and c.get("id") == final.id and c.get("param") == key
                            for c in changes):
                        changes.append({"kind": "setting", "step": i + 1, "id": final.id, "param": key,
                                        "before": was.get(key), "after": final.settings.get(key)})
        self._plan_doc = PlanDoc(request=doc.request, steps=tuple(
            s if not s.tool or s.runs_on else PlanStep(id=s.id, text=s.text, tool=s.tool, settings=s.settings,
                                                       runs_on=_tool_runs_on(s.tool), skill=s.skill)
            for s in doc.steps))
        emit({"type": "plan_edited", "by": "pi", "request": request[:300], "changes": changes, "notes": notes,
              "steps": sorted({c["step"] for c in changes if c["kind"] != "removed"})})
        return self._plan_doc.agenda()

    def _approve_plan_document(self, emit: EventFn, *, reviewed: bool = True) -> None:
        """Freeze the plan document as the one the run is pinned to and checked against."""
        if not self.config.plan_yaml or self._plan_doc is None:
            return
        self._approved_plan = self._plan_doc
        emit({"type": "plan_approved", "yaml": self._approved_plan.to_yaml(), "reviewed": reviewed,
              "pinned": sum(len(s.settings) for s in self._approved_plan.steps)})

    def _assigned_skill(self, step: str) -> "Skill | None":
        """The skill the plan assigned to ``step`` (``skill:`` in the plan document), if it is still in
        the library; ``None`` otherwise. The approved plan when there is one, else the draft."""
        plan = self._approved_plan or self._plan_doc
        found = plan.step_for(step) if plan is not None else None
        if found is None or not found.skill:
            return None
        return get_skill(found.skill)

    def _step_skills(self, step: str) -> "list[Skill]":
        """Every skill ``step`` uses: the one the plan assigned (``skill:``), any its text names, and
        the one its tool's TOOL.md names (run_cellqc runs the CellQC skill). Each skill once."""
        found: "list[Skill]" = []
        assigned = self._assigned_skill(step)
        if assigned is not None:
            found.append(assigned)
        found += [s for s in map(get_skill, skills_named_in(step)) if s is not None]
        tool = _primary_tool(step)
        by_tool = _tool_skill(tool) if tool else None
        if by_tool is not None:
            found.append(by_tool)
        return list({s.name: s for s in found}.values())

    def _explores_after(self, step: str) -> bool:
        """Whether a skill ``step`` uses asks for exploration after it (``exploration: "true"`` in its
        SKILL.md ``metadata``). Most skills need none, CellQC's and the RAG skills among them, so it is
        the skill's choice, not the platform's default (RCHENLAB/AiScientist#102)."""
        return any(declares(s, "exploration") for s in self._step_skills(step))

    def _pinned_settings(self, step: str) -> "dict[str, dict[str, Any]]":
        """``{tool: settings}`` the approved plan fixes for this step ({} when it fixes none)."""
        if self._approved_plan is None:
            return {}
        found = self._approved_plan.step_for(step)
        if found is None or not found.tool:
            return {}
        if not found.settings and self._scope.source != "explicit_mcp":
            return {}
        return {found.tool: dict(found.settings)}

    def _check_plan_tooling(self, agenda: "list[str]", emit: EventFn,
                            already_reported: "set[str]") -> "list[str]":
        """Correct unambiguous tool-name typos in ``agenda`` and announce what is left.

        ``already_reported`` carries finding signatures across review rounds so a warning the
        reviewer has already read is not repeated on every redraft of the same plan."""
        fixed, findings = check_plan_tooling(list(agenda), list(getattr(self.scientist, "catalog", [])))
        fresh = [f for f in findings
                 if f"{f['kind']}|{f['tool']}|{f['detail']}" not in already_reported]
        for f in fresh:
            already_reported.add(f"{f['kind']}|{f['tool']}|{f['detail']}")
        if fresh:
            emit({"type": "plan_tooling", "findings": fresh})
        return fixed

    def _patch_plan(self, agenda: list[str], request: str,
                    emit: EventFn) -> "list[str] | None":
        """Revise ONE step of ``agenda`` in place, or ``None`` to let the caller redraft.

        The model chooses which step and writes its replacement; THIS code applies it, so every
        step the user did not name is the same object it was — collateral damage is structurally
        impossible rather than discouraged by a prompt. See ``_PLAN_PATCH_SYSTEM`` for the measured
        reason this exists."""
        if not agenda or not request.strip() or self._complete_fn is None:
            return None
        numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(agenda))
        try:
            raw = self._complete([
                {"role": "system", "content": _PLAN_PATCH_SYSTEM},
                {"role": "user", "content": f"Plan:\n{numbered}\n\nRequested change:\n{request}"},
            ], role="plan")
        except Exception:  # noqa: BLE001 - a failed patch is not a failed revision; redraft instead
            return None
        patched = _parse_plan_patch(raw, len(agenda))
        if patched is None:
            return None
        idx, text = patched
        if _norm_step(text) == _norm_step(agenda[idx]):
            return None                      # nothing actually changed — let the redraft try
        out = [*agenda[:idx], text, *agenda[idx + 1:]]
        # Surface the edit as a DIFF. The whole failure this replaces was invisible: the plan came
        # back different and the UI showed no before/after, so a researcher could not tell that
        # asking for a clustering resolution had cost them the differential-expression step.
        emit({"type": "plan_patched", "step": idx + 1,
              "before": agenda[idx], "after": text, "request": request[:300]})
        # A threshold this edit moved, still stated at its OLD value by a step further down that
        # consumes this one's output. See ``stale_downstream_settings``.
        was, now = _settings_in(agenda[idx]), _settings_in(text)
        for param, old_value in was.items():
            new_value = now.get(param)
            if new_value is None:
                continue
            stale = stale_downstream_settings(out, idx, param, old_value, new_value)
            if stale:
                emit({"type": "plan_downstream_stale", "param": param, "changed_step": idx + 1,
                      "before": old_value, "after": new_value, "steps": stale})
        return out

    def _save_plan_reply(self, raw: "str | None") -> None:
        """Keep the PI's planning reply with the run (``process/plan_replies/reply-NN.txt``), so a
        plan that could not be read can be looked at afterwards. Never fails the planning."""
        workspace = getattr(self.ctx, "workspace", None)
        if not workspace:
            return
        try:
            folder = Path(workspace) / "artifacts" / "process" / "plan_replies"
            folder.mkdir(parents=True, exist_ok=True)
            n = len(list(folder.glob("reply-*.txt"))) + 1
            (folder / f"reply-{n:02d}.txt").write_text(raw or "", encoding="utf-8")
        except OSError:
            pass

    def _pi_plan(
        self,
        question: str,
        emit: EventFn,
        *,
        feedback: str = "",
        prior_agenda: list[str] | None = None,
        allow_clarify: bool = False,
        latest_feedback: str = "",
    ) -> tuple[str, Any]:
        """Draft (or re-draft) the plan. Returns ``("agenda", [steps])``; when ``allow_clarify``
        and the request is genuinely ambiguous, ``("clarify", [{question, options}])``; and
        ``("failed", message)`` when the PI's reply cannot be read as a plan even after it was asked
        once more (the caller runs nothing). ``feedback`` (the user's natural-language notes on the
        previous draft) + ``prior_agenda`` drive a revision instead of a fresh plan.

        A revision tries a SINGLE-STEP PATCH first (``_patch_plan``) and only redrafts the whole
        plan when the request cannot be expressed that way. ``latest_feedback`` is the note the
        user just typed; the patch is judged against that alone, because ``feedback`` accumulates
        every earlier note and re-applying those to an agenda that already carries them would
        undo the user's own earlier revisions."""
        emit({"type": "planning"})   # heartbeat: drafting takes 20-60s and used to render nothing
        if not feedback and not latest_feedback and not prior_agenda:
            from ..mcp_workflow import explicit_workflow, validated_agenda
            explicit = explicit_workflow(question)
            if explicit:
                try:
                    agenda = validated_agenda(explicit, list(self.scientist.catalog))
                except ValueError as exc:
                    return "failed", str(exc)
                self.config = replace(self.config, plan_yaml=True)
                self._plan_doc = PlanDoc(request=question, steps=tuple(
                    PlanStep(id=f"s{i}", text=text, tool=call.name, settings=copy.deepcopy(call.arguments),
                             runs_on=_tool_runs_on(call.name))
                    for i, (call, text) in enumerate(zip(explicit, agenda), 1)))
                self._self_sourced = "The researcher supplied the ordered tool calls and all stated settings."
                self._save_plan_reply(json.dumps({"source": "explicit_mcp", "agenda": agenda}))
                emit({"type": "pi_agenda", "agenda": agenda, "source": "explicit_mcp"})
                return "agenda", agenda

        # A small edit to an existing plan is a PATCH, not a re-plan. Only when the model says the
        # request needs more than one step changed do we fall through to the full redraft below.
        request = (latest_feedback or feedback).strip()
        # Which of the two revision paths ran. The single-step patch cannot damage a step it did not
        # name, so its result needs no collateral diff; the whole-plan redraft below can, and the
        # caller diffs it. Recorded here because only this function knows which one it took.
        self._last_plan_was_patch = False
        if prior_agenda and request:
            patched = self._patch_plan(list(prior_agenda), request, emit)
            if patched is not None:
                self._last_plan_was_patch = True
                emit({"type": "pi_agenda", "agenda": patched})
                return "agenda", patched

        tools_desc = "\n".join(
            f"- {t.name}: {_planner_tool_description(t)}"
            for t in self.scientist.catalog if t.name != "finish"
        ) + "\n" + _REPORT_TOOL_LINE
        # A pre-selected research path STEERS the PI here (it does not bypass it): the PI
        # still drafts the agenda — adapting the guidance to this dataset — and plan mode
        # still lets the user shape the result, in natural language, before any tool runs.
        parts = [f"Research question:\n{question}\n"]
        if self._scope.limited:
            parts.append(self._scope.prompt_block() + "\n")
        if self.config.prior_work:
            parts.append(self.config.prior_work + "\n")
        if self._guidance:
            # The guidance lays out a whole pipeline. It used to be handed over as "follow this",
            # so every plan carried every stage of it whatever the request asked for.
            parts.append("Follow this research-path guidance when planning. It describes the WHOLE "
                         "pipeline for this kind of study: plan all of its stages only for a "
                         "whole-analysis request; for a request that names the analyses it wants, "
                         "use it for HOW to do the stages you plan.\n" + self._guidance + "\n")
        dataset_ctx = self._dataset_context()
        if dataset_ctx:
            parts.append(dataset_ctx + "\n")
        parts.append(f"The scientist can run ONLY these tools (plan steps achievable with them):\n{tools_desc}\n")
        # Read for every plan, so each request planned in full is matched against the skills as they
        # are now (including any induced since the last run), not only the one preset pipeline.
        skills_block = plan_skills_block(f"{question}\n{dataset_ctx or ''}")
        if skills_block:
            parts.append(skills_block + "\n")
        if prior_agenda:
            prev = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(prior_agenda))
            parts.append(f"Your previous draft plan was:\n{prev}\n")
        if feedback:
            parts.append(
                "The user reviewed that plan and asked for these changes — revise the plan "
                f"to satisfy them (keep what they did not object to):\n{feedback}\n"
            )
        if allow_clarify:
            parts.append(
                "If the request is genuinely ambiguous in a way that changes the plan, you MAY "
                "return a clarify object instead (see your instructions). Otherwise return the agenda."
            )
        else:
            parts.append("Return the ordered agenda now.")

        messages = [
            {"role": "system", "content": self._name_literature_tool(_PI_SYSTEM)},
            {"role": "user", "content": "\n".join(parts)},
        ]
        raw = self._complete(messages, role="plan")
        self._save_plan_reply(raw)
        parsed = _parse_plan(raw, self.config.max_steps, allow_clarify)
        if parsed is None:
            # The reply is not a plan: prose around it, a JSON object that does not parse, or a reply
            # cut off at the role's output ceiling. Ask once more, with the reply in front of the PI,
            # for the plan in its format. (Prod run 3ed7676de128: Haiku's 8,936-token reply, well
            # under its ceiling, did not parse.)
            emit({"type": "plan_reformat", "chars": len(raw or "")})
            again = [*messages, *([{"role": "assistant", "content": raw}] if (raw or "").strip() else []),
                     {"role": "user", "content": _PLAN_REFORMAT}]
            try:
                retry = self._complete(again, role="plan")
            except Exception as exc:  # noqa: BLE001 - the failure below says the plan could not be read
                emit({"type": "plan_reformat_failed", "error": f"{type(exc).__name__}: {exc}"[:200]})
                retry = ""
            self._save_plan_reply(retry)
            parsed = _parse_plan(retry, self.config.max_steps, allow_clarify)
            if parsed is not None:
                raw = retry
        if parsed is None:
            # No plan. The question used to go in as the only step of a fallback plan, which the
            # reviewer could run as if the PI had written it (run 3ed7676de128 did, and no skill or
            # research path reached the step). A run with no plan runs nothing.
            emit({"type": "plan_unparsed", "chars": len(raw or ""),
                  "preview": " ".join((raw or "").split())[:200]})
            return "failed", _PLAN_FAILED_MESSAGE
        kind, payload = parsed
        if kind == "clarify":
            emit({"type": "pi_clarify", "questions": payload})
        else:
            # How the PI read the request, and what it left for a later message. A revision that
            # does not restate them keeps the first draft's reading.
            breadth, later = _parse_plan_extras(raw)
            if breadth:
                self._request_breadth = breadth
            if later or not feedback:
                self._later = later
            # Split off the PI's SELF-SOURCED disclosure — what it decided on its own knowledge
            # rather than from the guidance or the profile. It is a statement, not work: leaving it
            # in the agenda would hand it to the Scientist as a step to execute. Kept on the lab so
            # the plan the user reviews, plan.md and the technical report can all carry it, because
            # "which of these choices did the protocol actually specify" is otherwise unanswerable
            # once the numbers are in a Methods section.
            # The reply's `self_sourced` field; a reply in the old form carries it as a prefixed
            # agenda string instead, which is split off either way so it never runs as a step.
            payload, legacy = _split_self_sourced(list(payload))
            disclosure = _parse_self_sourced(raw) or legacy
            if disclosure:
                self._self_sourced = disclosure
                emit({"type": "plan_self_sourced", "text": disclosure})
            payload, echoed = _drop_question_echo(list(payload), question)
            if echoed:
                emit({"type": "steps_pruned", "reason": "question_echo", "steps": echoed})
            #after PI generate agenda, check if the literature is inside
            _agenda_now = list(payload)
            # Only the user's own words ask for literature. The protocol text mentions it in every
            # pipeline, so reading the guidance here forced a literature step into every plan
            # (prod run 1e887ed2bf8e: "Run CellQC … quality control report" planned CellQC and a
            # literature step). It is a separate step, run on request.
            payload = _ensure_literature_agenda(
                _agenda_now,
                question=question,
                guidance=None,
                feedback=feedback,
                max_steps=self.config.max_steps,
                # The limit the first message set holds until a review note asks for literature:
                # the scope is read once, and a note typed at the plan is the newer instruction.
                has_literature_tool=(self._scope.allows_literature()
                                     or _requests_literature(feedback)) and any(
                    t.name in ("deep_literature", "literature_search")
                    for t in self.scientist.catalog),
                author=lambda fb: self._author_step(
                    question, _agenda_now,
                    brief=self._name_literature_tool(
                        "A literature step near the end: search the published literature with "
                        "`deep_literature` for the key genes, cell types and pathways this "
                        "study will surface, and attach DOI-backed citations that ground the "
                        "biological interpretation. Name the biological subject of THIS "
                        "question; do not restate the analysis steps."),
                    fallback=fb),
                prior_agenda=prior_agenda,
                latest_feedback=request,
            )
            # The user's own limit, applied to the draft. Not when they are revising the plan in
            # chat: what they ask for there is the newer instruction, and the PI has both.
            if not feedback:
                payload = self._apply_scope(list(payload), emit)
            payload = self._ensure_report_step(list(payload), question, feedback=feedback,
                                               note=request, prior_agenda=prior_agenda, emit=emit)
            emit({"type": "pi_agenda", "agenda": payload})
            if self._later:
                emit({"type": "plan_later", "later": list(self._later),
                      "breadth": self._request_breadth})
        return kind, payload

    def _ensure_report_step(self, agenda: "list[str]", question: str, *, feedback: str = "",
                            note: str = "", prior_agenda: "list[str] | None" = None,
                            emit: EventFn) -> "list[str]":
        """The plan has one ``write_report`` step, last, exactly when a written report is asked for
        (RCHENLAB/AiScientist#102): by the request, or by a skill a step uses (``writes-report:
        "true"`` in its SKILL.md metadata). A note on a plan under review decides when it mentions
        the report ("add a written report", "drop the report step"); a note that does not keeps the
        plan as it was. The model's own report step is not one asked for."""
        steps = [s for s in agenda if not _is_report_step(s)]
        drafted = [s for s in agenda if _is_report_step(s)]
        declared: "list[str]" = []
        if feedback:
            if rules_out_report(note):
                want = False
            elif wants_written_report(note):
                want = True
            else:
                want = any(_is_report_step(s) for s in (prior_agenda or []))
        else:
            declared = sorted({sk.name for s in steps for sk in self._step_skills(s)
                               if declares(sk, "writes-report")})
            want = wants_written_report(question) or bool(declared)
        if not want:
            if drafted:
                emit({"type": "steps_pruned", "reason": "report_not_asked", "dropped": drafted})
            return steps
        if not drafted:
            emit({"type": "report_step_added", "skills": declared})
        return steps + [drafted[0] if drafted else REPORT_STEP]

    @staticmethod
    def _keep_report_step(before: "list[str]", after: "list[str]") -> "list[str]":
        """The plan review judges the analysis; whether there is a report step was decided before it
        (``_ensure_report_step``), so the reviewed plan keeps the draft's report step and gets no
        other."""
        report = [s for s in before if _is_report_step(s)]
        return [s for s in after if not _is_report_step(s)] + report[:1]

    def _apply_scope(self, agenda: list[str], emit: EventFn) -> list[str]:
        """Drop the steps the request ruled out (:class:`~aiscientist.agents.scope.RunScope`). The
        report step is not analysis the limit is about ("only QC, and write a report"): it stays,
        last, as ``_ensure_report_step`` decided."""
        if not self._scope.limited:
            return agenda
        report = [s for s in agenda if _is_report_step(s)]
        kept, dropped = self._apply_scope_to([s for s in agenda if not _is_report_step(s)], emit)
        return kept + report

    def _apply_scope_to(self, agenda: list[str], emit: EventFn) -> "tuple[list[str], list[str]]":
        kept, dropped = self._scope.filter(agenda, _primary_tool)
        if dropped:
            emit({"type": "steps_pruned", "reason": "user_scope", "dropped": dropped})
        elif len(kept) == len(agenda) and not any(self._scope.allows(_primary_tool(s)) for s in agenda):
            # Every step falls outside the limit: it could not be matched to this plan, so the plan
            # is shown whole rather than empty, and says so.
            emit({"type": "scope_unmatched", "text": self._scope.describe()})
        return kept, dropped

    def _accepted_findings_block(self, rounds: list[LabRound]) -> str:
        """Accepted upstream findings, each with the artifact paths that back it, framed for
        READ-ONLY downstream verification: the next step treats them as claims to VERIFY
        (re-ground by reading the cited artifact) rather than ground truth, and is forbidden
        from modifying any upstream artifact/checkpoint — self-repair is NOT enabled, so a
        downstream step can never tamper with intermediate results. ``''`` when none accepted."""
        lines: list[str] = []
        # Rounds already folded into a digest are represented by that digest, NOT by their full
        # text — this is what stops the carried prefix growing without bound across a long run.
        # Their evidence pointers were re-attached into the digest, so nothing loses provenance.
        if self._compacted_block:
            lines.append(self._compacted_block)
        for i, r in enumerate(rounds):
            if r.verdict.verdict != "accept" or i in self._compacted_indices:
                continue
            answer = r.scientist_result.get("final_answer") or "(no textual answer)"
            ev: list[str] = []
            for s in r.scientist_result.get("steps", []):
                for p in evidence_pointers(s.get("result")):
                    if p not in ev:
                        ev.append(p)
            line = f"- {r.step}: {answer}"
            if ev:
                # One artifact per line (NOT comma-joined): a long "a, b, c, d, e" line trips the
                # data-boundary guard's CSV-row heuristic and gets the whole brief blocked.
                line += "\n  evidence (verify against these; read-only):"
                line += "".join(f"\n    - {p}" for p in ev)
            lines.append(line)
        if not lines:
            return ""
        return (
            "Accepted findings so far — TREAT THESE AS CLAIMS TO VERIFY (not ground truth). Each "
            "lists the artifacts that back it. If THIS step relies on a prior finding, first VERIFY "
            "it by READING the cited artifact (open it read-only via run_code or a read tool). If the "
            "artifact does not support the claim, do NOT build on it — report the discrepancy instead. "
            "READ-ONLY: never modify or overwrite an upstream artifact or checkpoint.\n"
            "DO NOT RE-RUN an upstream analysis stage that already succeeded (QC, clustering, DE, "
            "enrichment) — its checkpoint under AISCIENTIST_WORK already exists; reuse it. Re-running an "
            "upstream stage (especially with DIFFERENT parameters, e.g. a new mito threshold) "
            "overwrites its checkpoint and DESYNCHRONIZES the already-exported tables/figures from it, "
            "corrupting the run. If this step needs a VARIANT of an upstream result (a subset, a "
            "relabelling, a different filter), write it with run_code under a NEW name in "
            "AISCIENTIST_WORK and hand it to the next tool as `input` — never over a checkpoint. "
            "Execute ONLY this step's work.\n" + "\n".join(lines)
        )

    def _context_pressure(self, rounds: list[LabRound]) -> "Any":
        """Measure the CARRIED history — the findings block rebuilt into every step's brief — against
        its share of the served window. Pure measurement; no model call."""
        from .. import context_budget as cb
        hc = self.scientist.config
        return cb.assess(self._accepted_findings_block(rounds), hc.max_model_len,
                         reserve=hc.output_reserve_tokens + hc.context_safety_margin)

    def _maybe_compact(self, rounds: list[LabRound], emit: EventFn, *, steps_done: int = 0,
                       milestone: str = "", manual: bool = False,
                       decision_review: "Callable[[Any], dict[str, Any]] | None" = None) -> str:
        """Decide — IN CODE — whether the run should compact its carried history, optionally ask the
        human, and do it. Returns the action taken: ``""`` (nothing), ``"compacted"``, ``"declined"``
        or ``"abort"``.

        The whole point of this method is that no part of the DECISION is delegated to a model. The
        payload is measured against the served window, the thresholds are constants, and which
        rounds fold is arithmetic. The model is used for exactly one thing: writing the digest prose,
        with the evidence pointers re-attached afterwards from the original rounds so provenance
        cannot be lost no matter what it writes.
        """
        from .. import context_budget as cb
        if not self.config.context_management:
            return ""
        pressure = self._context_pressure(rounds)
        trigger = cb.should_compact(pressure, steps_done=steps_done,
                                    max_steps=self.config.max_steps, milestone=milestone,
                                    manual=manual)
        if trigger is None:
            return ""
        emit({"type": "context_pressure", "kind": trigger.kind, "reason": trigger.reason,
              "tokens": pressure.tokens, "budget": pressure.budget,
              "ratio": round(pressure.ratio, 3), "urgent": trigger.urgent})

        # Human in the loop — but never for an URGENT compaction: at critical pressure the next
        # step may not fit at all, so asking permission to avoid overflowing is theatre.
        if decision_review is not None and not trigger.urgent:
            from types import SimpleNamespace
            go, skip, stop = "Compact now and continue", "Keep going without compacting", "Stop and write the report"
            goal = (f"{trigger.reason.capitalize()}. Compacting rewrites the earlier accepted steps "
                    "into one summary — the numbers and every artifact path are kept, the narration "
                    "is dropped — so later steps keep working with room to spare.")
            decision = decision_review(SimpleNamespace(
                id="context_compact", goal=goal, options=(go, skip, stop), decision=True)) or {}
            choice = str(decision.get("choice", "")).strip().lower()
            if str(decision.get("action", "")).lower() == "cancel" or choice == stop.lower():
                return "abort"
            if choice == skip.lower():
                emit({"type": "context_compact", "action": "declined"})
                return "declined"
        elif not trigger.urgent and trigger.kind in ("steps", "milestone"):
            # Headless, and not actually short of room: a milestone alone is not a reason to spend
            # a compaction call. Surface it and move on.
            return ""

        accepted = [i for i, r in enumerate(rounds) if r.verdict.verdict == "accept"]
        # An OPEN hypothesis still points at the step that raised it; folding that step's evidence
        # away would make the hypothesis unadjudicatable later.
        pinned = {i for i, r in enumerate(rounds)
                  if any(h.origin_step == r.step for h in self._ledger.open_items())}
        fold_positions = cb.fold_rounds(len(rounds), keep_recent=self.config.compact_keep_recent,
                                        pinned=pinned)
        fold = [i for i in fold_positions if i in accepted]
        if len(fold) < 2:
            emit({"type": "context_compact", "action": "nothing_to_fold"})
            return ""

        parts, evidence = [], []
        for i in fold:
            r = rounds[i]
            parts.append(f"- {r.step}: {r.scientist_result.get('final_answer') or '(no answer)'}")
            for s in r.scientist_result.get("steps", []):
                for p in evidence_pointers(s.get("result")):
                    if p not in evidence:
                        evidence.append(p)
        digest = cb.make_digest("\n".join(parts), self._complete)
        if not digest:
            # Degrading to "we did not save room" is always safer than degrading to "we lost the
            # findings", so a failed digest leaves the uncompacted text in place.
            emit({"type": "context_compact", "action": "failed"})
            return ""

        before = self._context_pressure(rounds).tokens
        self._compacted_indices |= set(fold)
        self._compacted_block = cb.compact_block(digest, evidence, len(fold))
        after = self._context_pressure(rounds).tokens
        emit({"type": "context_compact", "action": "compacted", "folded_steps": len(fold),
              "evidence_kept": len(evidence), "tokens_before": before, "tokens_after": after})
        return "compacted"

    def _plan_literature_queries(self, question: str, step: str,
                                 rounds: list[LabRound]) -> list[str]:
        """LLM-vetted, multi-angle literature queries: the model reads the ACCEPTED findings and
        writes several DISTINCT Europe PMC queries (per cell class / per pathway / disease
        mechanism). Falls back to the single deterministic findings query when the LLM is
        unavailable or returns nothing usable — so an offline/degraded run still cites."""
        # Everything that reads the accepted findings sits INSIDE the try: a shape this code did
        # not expect (a dict-valued pathway term) raised out of the digest, the caller's own
        # except then substituted [step] as the query, and Europe PMC was searched for the
        # literal text of the step brief. Degrade to the deterministic fallback, never to that.
        try:
            fallback = [_literature_query(question, step, rounds)]
        except Exception:  # noqa: BLE001
            fallback = [_literature_step_text(question).removeprefix("Literature search for ")]
        try:
            digest = _literature_findings_digest(rounds)
        except Exception:  # noqa: BLE001
            digest = ""
        try:
            user = (
                f"Research question: {question}\n"
                f"Literature-grounding step: {step}\n\n"
                + (f"Accepted findings (per cell class):\n{digest}\n" if digest
                   else "No structured findings were captured; base the queries on the question "
                        "and the step topic.\n")
                + "\nWrite the JSON array of distinct keyword queries now."
            )
            raw = self._complete([
                {"role": "system", "content": _LIT_QUERY_SYSTEM},
                {"role": "user", "content": user},
            ])
            queries = _parse_query_list(raw, self.config.max_literature_queries)
        except Exception:  # noqa: BLE001 - query planning must never break the run
            queries = []
        return queries or fallback

    def _deep_literature_question(self, question: str, step: str, rounds: list[LabRound]) -> str:
        """ONE natural-language question for deep_literature, written from the step brief and the
        accepted findings; the step brief itself (plain) when the model gives nothing usable, and the
        old keyword query only as a last resort."""
        try:
            digest = _literature_findings_digest(rounds)
        except Exception:  # noqa: BLE001
            digest = ""
        types = _annotated_cell_types(rounds)
        try:
            raw = self._complete([
                {"role": "system", "content": _DEEP_LIT_QUESTION_SYSTEM},
                {"role": "user", "content": (
                    f"Study request: {question}\n"
                    f"Literature step brief: {step}\n"
                    + (f"Cell types the analysis found: {', '.join(types)}\n" if types else "")
                    + (f"Accepted findings (per cluster):\n{digest}\n" if digest else "")
                    + "\nWrite the question now.")},
            ])
        except Exception:  # noqa: BLE001 - question writing must never break the run
            raw = ""
        q = re.sub(r"\s+", " ", str(raw or "")).strip().strip('"').strip()
        if len(q.split()) >= 6 and not q.startswith(("{", "[")) and len(q) <= 600:
            return q
        return (_deep_literature_fallback_question(step)
                or _literature_query(question, step, rounds) or step or question)

    def _run_literature_step(self, question: str, step: str, rounds: list[LabRound],
                             lit_tool: Any, specialist: Specialist,
                             emit: EventFn) -> HarnessResult:
        """Run the literature-grounding step: plan SEVERAL angle-specific queries via the LLM, search
        Europe PMC for each, then merge + de-duplicate citations (by DOI, else PMID, else title) into
        one accepted answer the Critic and the final References section reuse."""
        emit({"type": "scientist_start", "step": step, "specialist": specialist.name})
        try:
            queries = self._plan_literature_queries(question, step, rounds)
        except Exception:  # noqa: BLE001 - never search for the literal step brief
            queries = [_literature_step_text(question).removeprefix("Literature search for ")]
        # Split a modest citation budget across the queries so the merged set stays focused.
        per_limit = max(4, 12 // max(1, len(queries)))
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        tool_calls: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for q in queries:
            args = {"query": q, "limit": per_limit}
            emit({"type": "tool_start", "tool": "literature_search", "args": args})
            try:
                output = lit_tool.executor(args, self.ctx)
            except Exception as exc:  # noqa: BLE001 - one bad query never fails the whole step
                error_text = f"{type(exc).__name__}: {exc}"
                emit({"type": "tool_error", "tool": "literature_search", "error": error_text})
                errors.append({"tool": "literature_search", "error": error_text})
                tool_calls.append({"tool": "literature_search", "args": args, "ok": False,
                                   "error": error_text})
                continue
            res = output.get("results") if isinstance(output, dict) else []
            ok = isinstance(output, dict) and output.get("status") == "ok"
            emit({"type": "tool_result", "tool": "literature_search",
                  "summary": f"{'ok' if ok else 'error'}: {len(res or [])}",
                  "timing_outcome": tool_result_outcome(output) if isinstance(output, dict) else "failed"})
            tool_calls.append({"tool": "literature_search", "args": args, "ok": bool(ok),
                               "summary": f"{len(res or [])} result(s)", "result": output})
            for c in (res or []):
                if not isinstance(c, dict):
                    continue
                key = ((c.get("doi") or "").strip().lower()
                       or (c.get("pmid") or "").strip()
                       or (c.get("title") or "").strip().lower())
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(c)
        ok_citations = [c for c in merged if c.get("doi") or c.get("pmid")]
        combined = {"status": "ok" if ok_citations else "incomplete",
                    "queries": queries, "results": merged}
        answer = _literature_answer(queries, combined)
        status = "ok" if ok_citations else "incomplete"
        stop_reason = "literature_search_multi_query" if ok_citations else "no_literature_citations"
        return HarnessResult(
            status, stop_reason, answer, tool_calls,
            [] if ok_citations else (errors or [{"tool": "literature_search",
                                                 "error": "no DOI/PMID citations"}]),
        )

    def _run_deep_literature_step(self, question: str, step: str, rounds: list[LabRound],
                                  deep_tool: Any, specialist: Specialist,
                                  emit: EventFn) -> HarnessResult:
        """Run the literature-grounding step via ``deep_literature`` (PaperQA over the pre-built
        PubMedBERT corpus index on HPC3): ONE focused question -> a grounded, CITED answer drawn from
        the curated corpus, whose citations the Critic accepts and the final ``## References`` section
        reuses. Replaces the Europe PMC multi-query path so literature grounding stays inside the lab's
        own corpus instead of pulling arbitrary online preprints. deep_literature does its OWN
        retrieval, so there is no multi-query planning here — one focused corpus question suffices."""
        emit({"type": "scientist_start", "step": step, "specialist": specialist.name})
        q = self._deep_literature_question(question, step, rounds)
        args = {"question": q}
        emit({"type": "tool_start", "tool": "deep_literature", "args": args})
        try:
            output = deep_tool.executor(args, self.ctx)
        except Exception as exc:  # noqa: BLE001 - one literature failure never fails the whole run
            error_text = f"{type(exc).__name__}: {exc}"
            emit({"type": "tool_error", "tool": "deep_literature", "error": error_text})
            return HarnessResult(
                "incomplete", "deep_literature_error",
                "The deep_literature (indexed corpus) step failed; state this limitation in the "
                "report rather than inventing references.",
                [{"tool": "deep_literature", "args": args, "ok": False, "error": error_text}],
                [{"tool": "deep_literature", "error": error_text}],
            )
        ok = isinstance(output, dict) and output.get("status") == "ok"
        contexts = (output.get("contexts") if isinstance(output, dict) else None) or []
        state = output.get("status") if isinstance(output, dict) else "error"
        # A call that did not answer says why. Run BioAdmin/1e887ed2bf8e logged
        # "dependency_missing: 0 context(s)" and no more: the fallback below replaces this step's
        # tool calls, so the result's `dependency` and `note` were written nowhere in the run.
        why = ""
        if not ok and isinstance(output, dict):
            why = " ".join(str(output.get(k) or "") for k in ("dependency", "error", "note")).strip()
        emit({"type": "tool_result", "tool": "deep_literature",
              "summary": f"{'ok' if ok else state}: {len(contexts)} context(s)"
                         + (f" ({why[:240]})" if why else ""),
              "timing_outcome": tool_result_outcome(output) if isinstance(output, dict) else "failed"})
        tool_calls = [{"tool": "deep_literature", "args": args, "ok": bool(ok),
                       "summary": f"{len(contexts)} context(s)", "result": output}]
        # Build the accepted answer: the grounded cited answer, then a numbered list of the corpus
        # citations (de-duplicated) that the report's References section reuses.
        answer_text = ""
        cites: list[str] = []
        if isinstance(output, dict):
            answer_text = (output.get("formatted_answer") or output.get("answer") or "").strip()
            seen: set[str] = set()
            for c in contexts:
                cit = (c.get("citation") or "").strip() if isinstance(c, dict) else ""
                if cit and cit.lower() not in seen:
                    seen.add(cit.lower())
                    cites.append(cit)
        # ZERO contexts means PaperQA could not ground anything — its answer_text is then the
        # literal "I cannot answer this question due to having no papers", which is NOT a corpus
        # answer. Returning "ok" here blocked the Europe PMC fallback in production (run
        # 97dfc89dc5aa: the retigene corpus holds no DDX41 papers → status ok, contexts=[], and
        # References shipped empty although the broader keyword search existed as the fallback).
        # "I cannot answer" WITH a context is no answer either: run f3b8268c4fd4 got one tangential
        # passage, returned "ok", and the Europe PMC fallback never ran.
        bare = str((output.get("answer") if isinstance(output, dict) else "") or answer_text)
        if ok and cites and not _CANNOT_ANSWER.search(bare):
            lines = [answer_text] if answer_text else []
            if cites:
                lines += ["", "Citations from the indexed corpus (use ONLY these in the References "
                              "section — do NOT add any others):"]
                lines += [f"{i}. {c}" for i, c in enumerate(cites[:MAX_LISTED_CITATIONS], 1)]
            return HarnessResult("ok", "deep_literature_corpus", "\n".join(lines), tool_calls, [])
        note = (output.get("error") or output.get("note")) if isinstance(output, dict) else None
        return HarnessResult(
            "incomplete", "no_literature_citations",
            "The indexed corpus returned no grounded answer or citations for this question. State "
            "this limitation in the report rather than inventing references.",
            tool_calls, [{"tool": "deep_literature", "error": note or "no corpus answer"}],
        )

    def _run_search_corpus_step(self, question: str, step: str, rounds: list[LabRound],
                                corpus_tool: Any, specialist: Specialist,
                                emit: EventFn, pinned: "dict[str, Any] | None" = None) -> HarnessResult:
        """The literature-grounding step in a session with no served model (the user's own API key):
        the session's model writes 2-5 requests, ONE ``search_corpus`` call returns the passages, the
        model writes the answer from those passages, and :func:`corpus_answer.verify_answer` removes
        every citation and quotation the passages do not support. An answer left with no citation is
        no answer: the step is incomplete and the Europe PMC fallback runs.

        ``pinned``: the ``search_corpus`` settings the researcher approved with the plan. They go to
        the call as written, like every other tool's; approved ``queries`` replace the model's own."""
        from .. import corpus_answer as ca

        emit({"type": "scientist_start", "step": step, "specialist": specialist.name})
        pinned = dict(pinned or {})
        approved = pinned.get("queries")
        if isinstance(approved, str):
            approved = [approved]
        queries = [q.strip() for q in approved or [] if isinstance(q, str) and q.strip()][:ca.MAX_QUESTIONS]
        if not queries:
            try:
                digest = _literature_findings_digest(rounds)
            except Exception:  # noqa: BLE001
                digest = ""
            try:
                raw = self._complete([
                    {"role": "system", "content": ca.QUESTIONS_SYSTEM},
                    {"role": "user", "content": (
                        f"Research question: {question}\nLiterature step: {step}\n"
                        + (f"Accepted findings:\n{digest}\n" if digest else "")
                        + "\nWrite the JSON array now.")},
                ])
                queries = ca.parse_questions(raw)
            except Exception:  # noqa: BLE001 - request writing must never break the run
                queries = []
        queries = queries or [self._deep_literature_question(question, step, rounds)]
        args = {**pinned, "queries": queries}
        emit({"type": "tool_start", "tool": "search_corpus", "args": args})
        try:
            output = corpus_tool.executor(args, self.ctx)
        except Exception as exc:  # noqa: BLE001 - one literature failure never fails the whole run
            error_text = f"{type(exc).__name__}: {exc}"
            emit({"type": "tool_error", "tool": "search_corpus", "error": error_text})
            return HarnessResult(
                "incomplete", "search_corpus_error",
                "The search_corpus (indexed corpus) step failed; state this limitation in the report "
                "rather than inventing references.",
                [{"tool": "search_corpus", "args": args, "ok": False, "error": error_text}],
                [{"tool": "search_corpus", "error": error_text}])
        ok = isinstance(output, dict) and output.get("status") == "ok"
        contexts = [c for c in ((output.get("contexts") if isinstance(output, dict) else None) or [])
                    if isinstance(c, dict)]
        why = ""
        if not ok and isinstance(output, dict):
            why = " ".join(str(output.get(k) or "") for k in ("dependency", "error", "note")).strip()
        emit({"type": "tool_result", "tool": "search_corpus",
              "summary": f"{'ok' if ok else (output or {}).get('status', 'error') if isinstance(output, dict) else 'error'}"
                         f": {len(contexts)} passage(s)" + (f" ({why[:240]})" if why else ""),
              "timing_outcome": tool_result_outcome(output) if isinstance(output, dict) else "failed"})
        tool_calls = [{"tool": "search_corpus", "args": args, "ok": bool(ok),
                       "summary": f"{len(contexts)} passage(s)", "result": output}]
        incomplete = HarnessResult(
            "incomplete", "no_literature_citations",
            "The indexed corpus returned no grounded answer or citations for this step. State this "
            "limitation in the report rather than inventing references.",
            tool_calls, [{"tool": "search_corpus", "error": why or "no corpus answer"}])
        if not ok or not contexts:
            return incomplete
        try:
            written = self._complete([
                {"role": "system", "content": ca.ANSWER_SYSTEM},
                {"role": "user", "content": (
                    f"Research question: {question}\nLiterature step: {step}\n\n"
                    f"Passages:\n{ca.passages_block(contexts)}\n\nWrite the answer now.")},
            ])
        except Exception:  # noqa: BLE001
            written = ""
        verified = ca.verify_answer(str(written or ""), contexts)
        if verified.removed:
            emit({"type": "literature_check", "tool": "search_corpus", "removed": verified.removed})
        if not verified.cited or _CANNOT_ANSWER.search(verified.text):
            incomplete.errors = [{"tool": "search_corpus",
                                  "error": "no answer cited a returned passage" if written
                                  else "the answer could not be written"}]
            return incomplete
        # References come from the passages the checked answer cites, and only those.
        used = [c for c in contexts if str(c.get("pmid")) in set(verified.cited)]
        tool_calls[0]["result"] = {**output, "contexts": used, "answer": verified.text,
                                   "removed_by_check": verified.removed}
        cites = list(dict.fromkeys((c.get("citation") or "").strip() for c in used
                                   if (c.get("citation") or "").strip()))
        lines = [verified.text, "", "Citations from the indexed corpus (use ONLY these in the References "
                                    "section — do NOT add any others):"]
        lines += [f"{i}. {c}" for i, c in enumerate(cites[:MAX_LISTED_CITATIONS], 1)]
        if verified.removed:
            lines += ["", f"(Platform note: {len(verified.removed)} citation(s) or quotation(s) the "
                          "passages did not support were removed from the answer.)"]
        return HarnessResult("ok", "search_corpus_corpus", "\n".join(lines), tool_calls, [])

    def _scientist(self, question: str, step: str, specialist: Specialist, critique: str,
                   rounds: list[LabRound], emit: EventFn,
                   should_cancel: "Callable[[], bool] | None" = None,
                   user_notes: str = "", memory: str = "",
                   extra_steps: int = 0, plan_step: "str | None" = None) -> HarnessResult:
        """Run the Scientist on ``step``. ``plan_step``: the step's text in the approved plan, when
        ``step`` is a brief built from it (a DAG node's), so the approved settings can be found."""
        started = getattr(self, "_step_started", None)    # a test may call this on a stand-in
        if started is not None:
            started.setdefault(plan_step if plan_step is not None else step, time.time())
        # Literature grounding: prefer deep_literature (PaperQA over the lab's indexed corpus on
        # HPC3); fall back to literature_search (Europe PMC) ONLY when the corpus tool isn't wired
        # (dev / no-HPC), so behaviour never regresses where deep_literature can't run.
        primary = _primary_tool(plan_step if plan_step is not None else step)
        native_tool = canonical_tool_name(primary) != str(primary or "")
        # An explicitly approved MCP call keeps its own schema, dispatch and provenance.
        # The historical forced-literature path below selects only legacy tools.
        if _is_literature_step(step) and not native_tool:
            deep_tool = next((t for t in self.scientist.catalog if t.name == "deep_literature"), None)
            lit_tool = next((t for t in self.scientist.catalog if t.name == "literature_search"), None)
            corpus_tool = next((t for t in self.scientist.catalog if t.name == "search_corpus"), None)
            if deep_tool is None and corpus_tool is not None:
                # No served model in this session: the corpus is searched without one and the
                # session's model writes the answer. Same fallback as deep_literature below.
                pinned = self._pinned_settings(plan_step if plan_step is not None else step)
                result = self._run_search_corpus_step(question, step, rounds, corpus_tool, specialist, emit,
                                                      pinned=pinned.get("search_corpus"))
                if result.status == "ok" or lit_tool is None:
                    return result
                emit({"type": "literature_fallback", "from": "search_corpus",
                      "to": "literature_search", "reason": getattr(result, "stop_reason", "")})
                fallback = self._run_literature_step(question, step, rounds, lit_tool, specialist, emit)
                fallback.final_answer = (
                    "(Platform note: the RetiGene corpus, search_corpus, gave no answer that rested on its "
                    "passages; the platform then searched Europe PMC with literature_search. Judge the "
                    "citations below on their relevance to the step's question, not on which of the "
                    "two tools found them.)\n\n" + (fallback.final_answer or ""))
                return fallback
            if deep_tool is not None:
                result = self._run_deep_literature_step(question, step, rounds, deep_tool, specialist, emit)
                # deep_literature grounds against the curated corpus on HPC3. When it can't run
                # (HPC/GPU session down -> executor unavailable -> in-process read of /dfs3b fails,
                # or no corpus answer), fall back to Europe PMC literature_search so the report still
                # gets real citations instead of an empty References section (restores the pre-corpus
                # behaviour on degraded runs). A corpus success is always preferred over the fallback.
                if result.status == "ok" or lit_tool is None:
                    return result
                emit({"type": "literature_fallback", "from": "deep_literature",
                      "to": "literature_search", "reason": getattr(result, "stop_reason", "")})
                fallback = self._run_literature_step(question, step, rounds, lit_tool, specialist, emit)
                # Run 7f2955e8e: the Critic revised the fallback (0.35) because the step "called for
                # deep_literature" and keyword searches ran instead — so the report had no
                # references although Europe PMC had returned 11 papers. The switch is the
                # platform's, not the Scientist's: say so where the Critic reads it.
                fallback.final_answer = (
                    "(Platform note: the RetiGene corpus, deep_literature, returned no usable answer "
                    f"to \"{(result.steps[0].get('args') or {}).get('question', '') if result.steps else ''}\"; "
                    "the platform then searched Europe PMC with literature_search, as it always does in "
                    "that case. Judge the citations below on their relevance to the step's question, "
                    "not on which of the two tools found them.)\n\n" + (fallback.final_answer or ""))
                return fallback
            if lit_tool is not None:
                result = self._run_literature_step(question, step, rounds, lit_tool, specialist, emit)
                # The session has no `deep_literature` (registry.session_catalog: no HPC3
                # literature line), but a preset or a plan drafted earlier can still name it. Same
                # note as above, for the same reason: the tool was the platform's choice.
                if "deep_literature" in _declared_tools(step):
                    result.final_answer = (
                        "(Platform note: `deep_literature`, the RetiGene corpus, is not available in "
                        "this session, so the platform searched Europe PMC with literature_search. "
                        "Judge the citations below on their relevance to the step's question, not on "
                        "which of the two tools found them.)\n\n" + (result.final_answer or ""))
                return result

        # Adopt the specialist persona for this step (multi-role, Virtual-Lab style).
        parts = [f"You are the {specialist.name}. {specialist.persona}",
                 f"Research question: {question}", f"Step to execute now: {step}"]
        # Axis C — this expert's PRIVATE evolving memory (advisory lessons from past runs). Placed
        # near the top so it steers the approach, but framed as advisory so it never overrides data.
        if memory:
            parts.append(memory)
        # Downstream verification (read-only): each accepted upstream finding is surfaced WITH
        # the artifact paths that back it and framed as a CLAIM TO VERIFY, not ground truth — a
        # step that depends on a prior finding re-grounds by READING the cited artifact first.
        # Self-repair is deliberately NOT enabled: the brief forbids modifying any upstream
        # artifact/checkpoint, so a downstream step can never tamper with intermediate results.
        accepted_block = self._accepted_findings_block(rounds)
        if accepted_block:
            parts.append(accepted_block)
        # Mid-run guidance the user injected while the run executes — standing instructions
        # that apply to this and every remaining step (distinct from a Critic revision).
        if user_notes:
            parts.append("The user added guidance while this run is executing — follow it for "
                         "this and all remaining steps:\n" + user_notes)
        if critique:
            parts.append("A reviewer asked you to REVISE your previous attempt. Address this critique:\n" + critique)
        # Restate the roster in the brief (the function-calling API already carries the full
        # schemas, but naming the tools here steers the model toward the curated analysis line
        # instead of reinventing it in run_code).
        tool_names = ", ".join(t.name for t in self.scientist.catalog if t.name != "finish")
        parts.append(
            "Tools you can call: " + tool_names + ". Prefer the purpose-built analysis tools for "
            "standard steps; use run_code ONLY for analysis none of the tools cover — and never to "
            "write a report or package/zip outputs (the bundle is automatic, and a written report is "
            "a `write_report` step of its own). Data you "
            "prepared with run_code goes to a tool through its `input` parameter (a file in "
            "AISCIENTIST_WORK) — do not re-implement the tool's analysis in code to use it."
        )
        # Atomic SKILLS by PROGRESSIVE DISCLOSURE: the brief lists EVERY skill's name + description
        # (no cap — the agent reads the whole list before it chooses), never the bodies or code;
        # read_skill_reference fetches those on demand ONLY when a step needs analysis the tools
        # don't cover. Re-scanned first, so a skill folder dropped in since the last step is listed.
        refresh_skills()
        n_skills = len(ATOMIC_SKILLS)
        # A skill is a folder whose SKILL.md is the only required file: a short skill keeps its code
        # or shell commands inline there, a longer one bundles files (reference.py, scripts/...).
        has_shell = any(t.name == "run_shell" for t in self.scientist.catalog)
        _run = ("run Python with run_code and shell commands with run_shell" if has_shell else
                "run it with run_code (shell commands too, through subprocess inside the snippet)")
        _fetch = ("call `read_skill_reference(name)` for its SKILL.md — the guidance, which may itself "
                  "hold the code or commands — and its list of bundled files; fetch a bundled file "
                  "with `read_skill_reference(name, file=...)`. Adapt it to THIS dataset, then "
                  f"{_run} (read checkpoints from AISCIENTIST_WORK, write under "
                  "AISCIENTIST_ARTIFACTS). If a tool already covers the step, use the tool instead")
        # The skill the approved plan assigned to this step is HANDED OVER — its guidance and code are
        # in the brief — so using it is not left to the model's choice. Run 3ed7676de128 wrote its own
        # signature scoring while the vetted skill sat one read away; 4 of 47 prod runs had ever read
        # a skill. Otherwise a skill the step's text names is pointed at, as before.
        assigned = self._assigned_skill(plan_step if plan_step is not None else step)
        named = skills_named_in(step)
        if assigned is not None:
            parts.append(_assigned_skill_block(assigned))
        elif named:
            parts.append(("The plan assigns skill " if len(named) == 1 else "The plan assigns skills ")
                         + ", ".join(f"`{n}`" for n in named) + f" to this step: {_fetch}.")
        if n_skills:
            parts.append(
                f"Atomic skills ({n_skills}) — VETTED, adaptable guidance and code (not auto-run), "
                "listed by name and description; their bodies are NOT in your context. If — and only "
                f"if — THIS step needs analysis the tools above do not cover, {_fetch}. "
                "`search_skills(query)` ranks them by keyword:\n" + skill_manifest()
            )
        pinned = self._pinned_settings(plan_step if plan_step is not None else step)
        exact = self._scope.source == "explicit_mcp" and bool(pinned)
        if pinned:
            parts.append(
                "Settings the researcher approved with the plan: " + "; ".join(
                    f"`{tool}` — " + ", ".join(f"{k}={json.dumps(v)}" for k, v in settings.items())
                    for tool, settings in pinned.items())
                + (". These are the complete approved arguments; omitted fields keep server defaults. "
                   "No other scientific tool may run in this step. " if exact else
                   ". These values are applied to the call whatever you send; every other argument is yours to choose. ")
                + "If a result shows one of them is wrong for this data, say so in "
                "your answer — changing an approved value is the researcher's decision.")
        parts.append("Execute THIS step with the tools, then call `finish` with what you found for this step.")
        brief = "\n\n".join(parts)
        emit({"type": "scientist_start", "step": step, "specialist": specialist.name})
        # Only the USER-provided spans are untrusted for raw-data sniffing — the question and
        # any mid-run notes. The rest of the brief (persona, PI step text, prior-finding
        # artifact paths, tool roster, reference-template manifest) is system-built and trusted
        # by construction, so it is never mistaken for a raw expression dump.
        untrusted_text = "\n".join(t for t in (question, user_notes) if t)
        # Passed only when there is something to pin, so a scientist that predates the argument
        # (a test double, an older harness) is called exactly as before.
        pin_kwargs: dict[str, Any] = {"pinned_args": pinned} if pinned else {}
        if exact:
            pin_kwargs.update(exact_args=True, planned_tools=list(pinned))
        if "planned_tools" in inspect.signature(self.scientist.run).parameters:
            approved_step = plan_step if plan_step is not None else step
            planned = set(_declared_tools(approved_step))
            if self._approved_plan is not None:
                found = self._approved_plan.step_for(approved_step)
                if found is not None and found.tool:
                    planned.add(found.tool)
            # Only a tool whose manifest says one successful call is its whole step (run_cellqc)
            # ends the step on that call. Any other step keeps its own answer, which the Critic
            # and the report read, and may call its tool again or follow it with run_code.
            if len(planned) == 1 and _tool_ends_step(next(iter(planned))):
                pin_kwargs["planned_tools"] = sorted(planned)
        # The step runs inside its skill's scope: the run's manifest records the skill, and every
        # run_code in this step carries it (its declared access) without the model having to name
        # it. The skill's version is counted once for the step, by the code that ran its method, and
        # a revision of the step is the same step. Nothing of it outlives the step, in sequence or
        # in parallel.
        from ..skills_registry import record_use, step_scope
        with step_scope(assigned, step=plan_step if plan_step is not None else step):
            if assigned is not None:
                record_use(self.ctx, assigned)
                emit({"type": "step_skill", "step": step, "skill": assigned.name,
                      "version": assigned.version or "local", "release": assigned.registry_release or ""})
            result = self.scientist.run(brief, self.ctx, on_event=emit, should_cancel=should_cancel,
                                        untrusted_text=untrusted_text, extra_steps=extra_steps,
                                        **pin_kwargs)
            # Named-tool-not-called nudge. A step whose text names the tool that IS the step
            # (`run_de`, `run_composition`) sometimes ends with the model writing final text after a
            # round of reconnaissance — inspect, list_dir, read_tool_source — without ever calling it
            # ("stop_reason: model_final_text", zero evidence). The Critic bounces that, correctly,
            # but at the price of a whole extra round. One deterministic nudge, once, is cheaper: if
            # the step names catalog tools, none of them ran, and the model stopped on its own, tell
            # it exactly that and let it finish the step. ``done_early`` counts as stopping on its own
            # when the named tool never ran: what "succeeded" was a side probe (run f3b8268c4fd4's ORA
            # step: one exploratory run_code, two failed ones, run_enrichment never called). Never
            # applied to a step that stopped for a cancel, an error budget or a step limit — those are
            # not "forgot to call it".
            named = {n for n in _BACKTICK_TOOL_RE.findall(step or "")}
            catalog = {t.name for t in getattr(self.scientist, "catalog", [])}
            named &= catalog
            called = {str(st.get("tool")) for st in (result.steps or [])}
            if (named and not (named & called)
                    and getattr(result, "stop_reason", "") in ("model_final_text", "done_early")
                    and not (should_cancel is not None and should_cancel())):
                emit({"type": "tool_nudge", "step": step, "tools": sorted(named)})
                nudge = ("\n\nNOTE: your last attempt ended without calling "
                         + ", ".join(f"`{n}`" for n in sorted(named))
                         + " — the tool this step is about. The reconnaissance is done; call it now with "
                         "the settings the step specifies, then report what it returned.")
                result = self.scientist.run(brief + nudge, self.ctx, on_event=emit,
                                            should_cancel=should_cancel, untrusted_text=untrusted_text,
                                            extra_steps=extra_steps, **pin_kwargs)
        return result

    def _count_problems(self, step: str, result: HarnessResult) -> list[str]:
        """The per-arm cell counts this step's answer states that its own tool results — or the
        dataset profile — contradict, one line per group. Empty when there is nothing to check."""
        if result.answer_synthesized or not result.final_answer:
            return []           # a deterministic digest of the tool results cannot misquote them
        if _is_literature_step(step):
            return []           # it reports other studies' cells; this dataset is no ceiling on them
        decisions = self.ctx.decisions or {}
        # The profile describes the PRIMARY file. With several bound, a step may have analysed or
        # merged another one, and a count above the primary's would contradict nothing.
        profile = (decisions.get("dataset_result")
                   if len(decisions.get("datasets") or []) <= 1 else None)
        return describe_count_mismatches(
            find_count_mismatches(result.final_answer, result.steps, profile))

    def _critic(self, question: str, step: str, result: HarnessResult, emit: EventFn) -> CriticVerdict:
        # Forward the tools' ACTUAL structured returns (size-bounded), not a hand-picked
        # field list — so the Critic can ground its verdict on what was really produced
        # (artifact paths, counts, status) and new tools/artifacts need NO change here.
        # Each tool result also carries ``evidence``: the concrete on-disk artifact paths it
        # wrote (extracted deterministically, no LLM), binding a claim to WHAT backs it; the
        # step-level ``evidence`` unions them so the Critic sees the full evidence set. This
        # is surfaced for grounding only — the deterministic accept-floor below is unchanged.
        tool_results: list[dict[str, Any]] = []
        all_evidence: list[str] = []
        for s in result.steps:
            if s.get("tool") == "finish":
                continue
            ev = evidence_pointers(s.get("result"))
            tool_results.append({
                "tool": s.get("tool"),
                "ok": s.get("ok"),
                "result": result_digest(s.get("result")),
                "evidence": ev,
            })
            for p in ev:
                if p not in all_evidence:
                    all_evidence.append(p)
        # Deterministic check BEFORE the model sees it: do the claimed artifacts exist? A tool that
        # names a file it never wrote is the cheapest member of the failure class that matters here
        # — it SUCCEEDS, so nothing upstream objects, and only the filesystem can contradict it.
        # Reported, not judged: a verdict is not flipped on this alone, because an artifact can be
        # mirrored back from HPC3 on its own schedule and rejecting real work over a timing race
        # would be a worse failure than the one being caught.
        present, missing = resolve_evidence(all_evidence, self.ctx.workspace)
        if missing:
            emit({"type": "evidence_missing", "step": step, "paths": missing})
        # The outputs the step's skill (or its tool, under an active pipeline) DECLARES, checked on
        # disk: present, written by this step, not empty, a figure not blank. Unlike the evidence
        # above this is judged: a required output that is not there keeps the step from being
        # accepted (the floor below), because the declaration is what the researcher approved.
        outputs_record, out_missing = self._check_step_outputs(step, emit)
        # Tool SELF-DIAGNOSIS. A tool knows what its own unhealthy output looks like far better than
        # a reviewer re-deriving it from raw counts — e.g. run_scanpy_qc knows that zero matched
        # mitochondrial genes means its max_pct_mt filter was a no-op, which no count in the result
        # reveals. Hoisted to the TOP of the payload because a warning buried inside a nested tool
        # result is a warning nobody reads.
        tool_warnings: list[str] = []
        for s in result.steps:
            res = s.get("result")
            if isinstance(res, dict):
                # Accept BOTH spellings. The convention is a `warnings` LIST, but tools written
                # against the older shape set a singular `warning` string — and reading only the
                # plural silently dropped exactly the loudest one in the codebase:
                # annotate_variants' "TRUNCATED: only the first 500 variants were annotated ...
                # all counts describe a 500-variant slice, not the whole file". It sat inside the
                # nested result, which is the "warning nobody reads" this block exists to prevent.
                raw = res.get("warnings") or []
                if isinstance(raw, str):
                    raw = [raw]
                single = res.get("warning")
                if isinstance(single, str) and single.strip():
                    raw = [*raw, single]
                for w in raw:
                    line = f"{s.get('tool')}: {w}"
                    if line not in tool_warnings:
                        tool_warnings.append(line)
        if tool_warnings:
            emit({"type": "tool_warnings", "step": step, "warnings": tool_warnings})
        # Does the ANSWER restate its own results correctly? Run 8847d521ba32's DE answer swapped the
        # two arms of every skipped cell type and invented a split for the tested ones, while run_de's
        # result held the right numbers — and the Critic's digest of that result stopped at its 30th
        # key, before `skipped_groups`, so the model could not have compared them. Checked in code
        # (step_numbers.py), then both put in front of the model and enforced after it.
        count_problems = self._count_problems(step, result)
        if count_problems:
            emit({"type": "numbers_contradicted", "step": step, "problems": count_problems})
        payload = {
            "research_question": question,
            "step": step,
            **({"REQUIRED_OUTPUTS_MISSING": out_missing,
                "outputs_note": (
                    "Checked on disk, not by a model: the skill or pipeline this step follows "
                    "declares these outputs as required, and the step did not leave them (not "
                    "written by this step, empty, or a blank image). The step is not complete "
                    "without them; name them in the critique.")} if out_missing else {}),
            **({"DECLARED_OUTPUTS": outputs_record["checked"]} if outputs_record else {}),
            **({"ANSWER_CONTRADICTS_TOOL_RESULTS": count_problems,
                "contradiction_note": (
                    "Checked in code, not by a model: each line is a count the scientist's final "
                    "answer states that this step's own tool results (or the dataset itself) "
                    "contradict. An answer that misstates its own results is not acceptable as "
                    "written; name these in the critique.")} if count_problems else {}),
            # Ahead of the results on purpose: these are the tool's own statements about what it got
            # WRONG or could not do, and they must not be read after a wall of successful-looking
            # numbers.
            **({"TOOL_SELF_REPORTED_PROBLEMS": tool_warnings,
                "tool_warning_note": (
                    "Each line is a tool reporting a defect in ITS OWN output. Treat every one as a "
                    "fact about this step. A claim these warnings contradict is unsupported, no "
                    "matter how complete the numbers below look.")} if tool_warnings else {}),
            "scientist_status": result.status,
            "scientist_final_answer": result.final_answer,
            "tool_results": tool_results,
            "evidence": present,
            "tool_errors": result.errors,
        }
        if missing:
            payload["evidence_MISSING_from_disk"] = missing
            payload["evidence_note"] = (
                "The tool claimed these artifacts but they are NOT on disk. Do NOT treat them as "
                "backing for any statement; a claim whose only support is a missing file is "
                "unsupported.")
        # The skill the approved plan assigned. Its guidance and code were in the scientist's brief;
        # whether the step used them is judged from the code it ran, which the results above omit.
        # Every script in full up to a budget, and past it the lines that call the skill's own
        # functions: run 74873590ca7b's scoring script was cut at a fixed 2,000 characters, 167
        # before its `sc.tl.score_genes(` call, and the Critic could only ask whether it was there.
        assigned = self._assigned_skill(step)
        if assigned is not None:
            functions = skill_functions(assigned)
            scripts, not_shown = fit_scripts(result.steps, functions, succeeded=step_succeeded)
            payload["ASSIGNED_SKILL"] = {
                "name": assigned.name, "version": assigned.version or "local",
                "what_it_does": " ".join((assigned.summary or "").split())[:800],
                "skill_functions": list(functions[:30]),
                "code_it_ran": scripts,
                **({"scripts_not_shown": not_shown} if not_shown else {}),
            }
            payload["assigned_skill_note"] = (
                "The approved plan assigned this skill to the step, and its guidance and code were in "
                "the scientist's brief. `code_it_ran` is the code of the step's run_code and "
                "run_in_environment calls (`tool_call` is its place in `tool_results`), in full "
                "unless `shown` is \"excerpt\". An excerpt of a long script keeps the lines that call "
                "the skill's own functions (`skill_functions`, found in the skill's code), the lines "
                "around them and its first and last lines, and marks each gap "
                "`# [... N lines left out ...]`; `calls_skill_functions` lists, found in code, the "
                "ones each script calls (`skill_functions` is empty when the skill's method is not "
                "a Python call). Check from this code that the step used the skill's method. A step "
                "that wrote a method of its own instead, and does not say in its answer why the "
                "skill did not fit this data, has not done what was approved: ask it to revise with "
                "the skill. A step that only reads, compares or plots what an earlier step made with "
                "the skill does not have to run the skill again.")
        messages = [
            {"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ]
        for attempt in range(2):
            raw = self._complete(messages, role="critic")
            parsed = _parse_verdict(raw or "") or {}
            verdict = str(parsed.get("verdict") or "").strip().lower()
            critique = str(parsed.get("critique") or "").strip()
            if verdict == "accept" or (verdict in ("revise", "reject") and critique):
                break
            if attempt == 0:
                emit({"type": "critic_retry", "step": step,
                      "reason": "missing verdict or revision critique"})
                messages += [
                    {"role": "assistant", "content": raw or ""},
                    {"role": "user", "content": "Your review had no usable verdict or revision "
                     "critique. Review the SAME result again. Reply with the required JSON; a "
                     "revise verdict must explain what needs fixing."},
                ]
        else:
            # Advance with the existing result; a failed reviewer has supplied no reason to rerun.
            # The floors below still hold: an empty step or contradicted counts are never kept.
            if any(step_succeeded(s) for s in result.steps) and not count_problems and not out_missing:
                v = CriticVerdict("accept", 0.0,
                                  "Step not reviewed: the Critic returned no usable review twice; "
                                  "the existing result was kept without rerunning the step.", False,
                                  outputs_record)
                emit({"type": "step_not_reviewed", "step": step, "reason": v.critique})
                emit({"type": "critic", "step": step, "verdict": v.verdict, "score": v.score,
                      "critique": v.critique, "reviewed": False})
                return v
            verdict, critique = "accept", ""   # the floors below turn this into a grounded revise
        if verdict == "reject":
            verdict = "revise"
        try:
            score = float(parsed.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0

        # Deterministic floor: a run that produced NOTHING usable can never be accepted
        # (no rubber-stamping an empty/failed step). But a run that DID produce a real
        # artifact — at least one tool returned a success status — may be accepted even if
        # the harness loop ended ``incomplete`` or without a textual ``final_answer``: the
        # artifact is the result, not the prose. (Previously this keyed on
        # status/errors/final_answer and wrongly buried steps whose tool succeeded but
        # whose loop ran out of turns — e.g. scgpt_annotate that wrote predictions.csv.)
        produced_artifact = any(step_succeeded(s) for s in result.steps)
        if not produced_artifact and verdict == "accept":
            critique = (critique + " [auto-guard: no successful tool output to ground acceptance]").strip()
            verdict = "revise"
            score = min(score, 0.5)
        # Second floor: an answer whose counts its own tools contradict is never accepted as written
        # — the accepted answer is what the next steps are briefed with and what the report writer
        # copies. The critique carries the right values, so the retry restates them, not re-derives.
        if count_problems:
            critique = (critique + " [auto-guard: the answer's counts contradict this step's own "
                        "results — " + " ".join(count_problems) + " Restate every per-arm count "
                        "exactly as given here; do not re-derive, re-type or swap them.]").strip()
            verdict = "revise"
            score = min(score, 0.5)
        # Third floor: a required declared output is not there. Never accepted, whatever the model
        # said; after the revision budget the step ends unaccepted and the report's Diagnostics
        # name the missing files.
        if out_missing:
            critique = (critique + " [auto-guard: required outputs missing — " + " ".join(out_missing)
                        + " Write each at its declared path under $AISCIENTIST_ARTIFACTS. If this "
                        "data cannot give one, say so in the answer: the step then ends without "
                        "being accepted, and the report says why.]").strip()
            verdict = "revise"
            score = min(score, 0.5)

        v = CriticVerdict(verdict, max(0.0, min(1.0, score)), critique, outputs=outputs_record)
        emit({"type": "critic", "step": step, "verdict": v.verdict, "score": v.score,
              "critique": v.critique})
        return v

    def _check_step_outputs(self, step: str, emit: EventFn
                            ) -> "tuple[dict[str, Any] | None, list[str]]":
        """Check the outputs ``step`` declares (``_expected_outputs``) under the run's artifacts, record
        the result for the Results panel, and announce it (``step_outputs``). Returns the record for
        the verdict and one line per required output that is not there; ``(None, [])`` when the step
        declares none or the run has no workspace."""
        if _is_literature_step(step) or self.ctx.workspace is None:
            return None, []
        expected, sources = self._expected_outputs(step)
        if not expected:
            return None, []
        art = Path(self.ctx.workspace) / "artifacts"
        results = skill_outputs.check(expected, art, since=self._step_started.get(step))
        missing = skill_outputs.problem_lines(results)
        record = {"sources": sources, "checked": [r.to_public() for r in results],
                  "missing_required": missing}
        plan = self._approved_plan or self._plan_doc
        index = next((i for i, s in enumerate(plan.steps) if s.text == step), None) if plan else None
        title = plan.steps[index].title if plan is not None and index is not None else split_step(step)[0]
        skill_outputs.record_step(art, step=step, title=title, index=index,
                                  source=", ".join(sources), results=results)
        emit({"type": "step_outputs", "step": step, "sources": sources,
              "outputs": record["checked"], "missing_required": missing})
        return record, missing

    def _plan_state(self, question: str, agenda: list[str], step_idx: int, rounds: list[LabRound],
                    *, pruned: "frozenset[int] | set[int]" = frozenset()) -> dict[str, Any]:
        """Compact WHOLE-PLAN + accepted-findings context shared by the pre-flight gate and the
        post-step review, so both reason about a step IN THE CONTEXT OF THE PLAN, not in isolation —
        the view the per-step Critic structurally never had (see the meeting-protocol doc)."""
        plan = []
        for i, s in enumerate(agenda):
            state = ("pruned" if i in pruned else
                     "done" if i < step_idx else "current" if i == step_idx else "remaining")
            plan.append({"i": i + 1, "step": s, "state": state})
        accepted = []
        for r in rounds:
            if r.verdict.verdict != "accept":
                continue
            arts = [result_digest(st.get("result")) for st in r.scientist_result.get("steps", [])
                    if step_succeeded(st)]
            accepted.append({"step": r.step,
                             "answer": (r.scientist_result.get("final_answer") or "")[:300],
                             "artifacts": arts[:6]})
        return {"research_question": question, "plan": plan, "accepted_findings": accepted,
                "dataset_profile": self._dataset_context()}

    def _preflight_gate(self, question: str, step: str, agenda: list[str], step_idx: int,
                        rounds: list[LabRound], emit: EventFn,
                        *, pruned: "frozenset[int] | set[int]" = frozenset()) -> PreflightDecision:
        """PI↔Critic PRE-FLIGHT gate for ONE step, before the Scientist runs. Deterministic floor first
        (never asks a model — Qwen3.6 ignores prompt-only steering); then a Critic challenge on
        necessity/redundancy/precondition/altitude; then, ONLY if the Critic objects, the PI adjudicates
        (it owns the plan, final call). No-op returning ``proceed`` unless ``config.step_meetings``."""
        if not self.config.step_meetings:
            return PreflightDecision("proceed", by="off")
        # Deterministic floor — enrichment on an annotated, no-contrast dataset is circular. Same rule
        # the plan-time prune applies; belt-and-suspenders for any enrichment step that reached here.
        dr = (self.ctx.decisions or {}).get("dataset_result")
        if _is_enrichment_step(step) and _annotated_without_contrast(dr):
            reason = "enrichment on an annotated dataset with no experimental contrast is circular"
            emit({"type": "preflight", "step": step, "action": "skip", "by": "guard", "reason": reason})
            return PreflightDecision("skip", reason, by="guard")
        payload = self._plan_state(question, agenda, step_idx, rounds, pruned=pruned)
        critic = _parse_verdict(self._complete([
            {"role": "system", "content": _PREFLIGHT_GATE_SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ], role="critic")) or {}
        c_action = str(critic.get("action", "proceed")).strip().lower()
        if c_action not in ("proceed", "amend", "skip"):
            c_action = "proceed"
        if c_action == "proceed":
            emit({"type": "preflight", "step": step, "action": "proceed", "by": "critic"})
            return PreflightDecision("proceed", by="critic")
        # The Critic objected → the PI adjudicates (final call).
        pi_payload = {**payload, "critic_objection": {
            "action": c_action, "reason": str(critic.get("reason", "")),
            "amendment": str(critic.get("amendment", ""))}}
        pi = _parse_verdict(self._complete([
            {"role": "system", "content": _PREFLIGHT_PI_SYSTEM},
            {"role": "user", "content": json.dumps(pi_payload)},
        ], role="critic")) or {}
        action = str(pi.get("action", "proceed")).strip().lower()
        if action not in ("proceed", "amend", "skip"):
            action = "proceed"
        reason = str(pi.get("reason", "") or critic.get("reason", ""))
        amendment = str(pi.get("amendment", "")
                        or (critic.get("amendment", "") if action == "amend" else ""))
        emit({"type": "preflight", "step": step, "action": action, "by": "pi", "reason": reason})
        return PreflightDecision(action, reason, amendment, by="pi")

    def _poststep_review(self, question: str, step: str, verdict: CriticVerdict, result: HarnessResult,
                         agenda: list[str], step_idx: int, rounds: list[LabRound], emit: EventFn,
                         *, pruned: "frozenset[int] | set[int]" = frozenset()) -> dict[str, Any]:
        """PI POST-STEP review after the Critic ACCEPTS a step: did it change the picture, and is any
        REMAINING step now moot? Returns ``{"contribution": str, "prune": [step_text, ...]}`` — the
        prune list is restricted to actual remaining step texts. Emit + return only; the caller enacts
        the prune (the linear loop does; the DAG path calls it emit-only). No-op unless step_meetings."""
        remaining = [agenda[j] for j in range(step_idx + 1, len(agenda)) if j not in pruned]
        if not self.config.step_meetings or not remaining:
            return {"contribution": "", "prune": []}
        payload = self._plan_state(question, agenda, step_idx, rounds, pruned=pruned)
        payload["completed_step"] = {
            "step": step, "critic_verdict": verdict.verdict, "critic_score": verdict.score,
            "answer": (result.final_answer or "")[:400],
            "artifacts": [result_digest(s.get("result")) for s in result.steps if step_succeeded(s)][:8]}
        payload["remaining_steps"] = remaining
        rev = _parse_verdict(self._complete([
            {"role": "system", "content": _POSTSTEP_PI_SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ], role="critic")) or {}
        contribution = str(rev.get("contribution", "")).strip().lower()
        prune = [s for s in (rev.get("prune") or []) if isinstance(s, str) and s in remaining]
        if contribution or prune:
            emit({"type": "poststep_review", "step": step, "contribution": contribution,
                  "prune": prune, "reason": str(rev.get("reason", ""))})
        return {"contribution": contribution, "prune": prune}

    def _explore_after_step(self, question: str, step: str, verdict: CriticVerdict,
                            result: dict[str, Any], plan_steps: list[str], rounds: list[LabRound],
                            emit: EventFn) -> list[str]:
        """The EXPLORATION turn: did this accepted step open a research path the plan does not cover?

        Returns the step texts to APPEND to the plan (``[]`` — the common case — means nothing new).
        Side effect: the run's :class:`~aiscientist.agents.hypotheses.HypothesisLedger` gains any new
        falsifiable hypothesis and any adjudication of an open one, so a hypothesis raised at step 3
        can be closed out by step 7 instead of dangling.

        This is the one method that lets the plan GROW, so every guard lives here and is
        DETERMINISTIC — the model proposes, the code decides what survives:

        * off unless ``config.hypothesis_driven`` (the operator's switch);
        * off after a step unless a skill that step uses declares ``exploration`` (``_explores_after``):
          the skill decides, so a step with no skill, or with one that needs none, is never explored;
        * a proposed step is dropped unless it names a hypothesis we actually hold (no orphan work);
        * dropped if it duplicates an existing plan step (normalized) — the model's most common
          failure is restating a planned step as a discovery;
        * dropped if it is report/export busywork (the same class of step the PI is told to never plan);
        * capped by ``max_new_steps`` for the run AND by ``max_steps`` for total plan length.

        ``result`` is the Scientist's result as a DICT (``HarnessResult.to_dict()`` / a LabRound's
        ``scientist_result``) so the linear loop and the DAG scheduler — which hold it in different
        forms — call this identically.

        Never raises: any parse/LLM failure degrades to "nothing new", i.e. today's behaviour.
        """
        if not self.config.hypothesis_driven or self._scope.limited:
            return []   # a run the user limited does not grow new steps
        if not self._explores_after(step):
            return []   # no skill of this step asks for exploration (RCHENLAB/AiScientist#102)
        room = min(self.config.max_new_steps - self._new_steps_added,
                   self.config.max_steps - len(plan_steps))
        if room <= 0:
            return []
        # DETERMINISTIC PRE-FILTER — skip the call when there is provably nothing to be surprised by.
        # On the lab's own GPUs inference is free in cash, so the cost of exploration is LATENCY and
        # queue time, not tokens; the way to make autonomy cheap is therefore to not issue calls that
        # can only come back empty. Both cases below are exactly that:
        #   * a literature step's result is a deterministic Europe PMC query — it cannot contradict
        #     the plan's premise, and the linear loop already special-cases these (never revise-loop);
        #   * a step that produced NO artifact and only a token of prose has no finding in it to be
        #     surprised by, so the model would be reading an empty payload.
        answer = (result.get("final_answer") or "").strip()
        artifacts = [s for s in result.get("steps", []) if step_succeeded(s)]
        if _is_literature_step(step) or (not artifacts and len(answer) < 80):
            return []
        tools = ", ".join(t.name for t in self.scientist.catalog if t.name != "finish")
        payload = {
            "research_question": question,
            "completed_step": {
                "step": step, "critic_verdict": verdict.verdict, "critic_score": verdict.score,
                "answer": (result.get("final_answer") or "")[:1200],
                "artifacts": [result_digest(s.get("result")) for s in result.get("steps", [])
                              if step_succeeded(s)][:8]},
            "current_plan": list(plan_steps),
            "accepted_findings": [
                {"step": r.step, "answer": (r.scientist_result.get("final_answer") or "")[:300]}
                for r in rounds if r.verdict.verdict == "accept"][-8:],
            "open_hypotheses": [h.to_dict() for h in self._ledger.open_items()],
            "dataset_profile": self._dataset_context(),
            "tools_available": tools,
            "new_steps_you_may_add": room,
        }
        try:
            raw = self._complete([
                {"role": "system", "content": _EXPLORE_SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ])
        except Exception:  # noqa: BLE001 - exploration is best-effort; a failure must not kill the run
            return []
        rev = _parse_verdict(raw) or {}

        # 1. Adjudicate the OPEN hypotheses this result bears on (closes the loop).
        for item in (rev.get("resolve") or []):
            if not isinstance(item, dict):
                continue
            # `favoured` is the contest form; `status` is still accepted so an older model reply
            # (or a replayed run_state) does not silently stop closing hypotheses.
            favoured = str(item.get("favoured", "")).strip()
            closed = (self._ledger.adjudicate(str(item.get("hypothesis", "")), favoured,
                                              str(item.get("evidence", "")))
                      if favoured else
                      self._ledger.resolve(str(item.get("hypothesis", "")),
                                           str(item.get("status", "")),
                                           str(item.get("evidence", ""))))
            if closed is not None:
                emit({"type": "hypothesis_resolved", "id": closed.id, "status": closed.status,
                      "statement": closed.statement,
                      "evidence": closed.evidence[-1] if closed.evidence else ""})

        # 2. Record new falsifiable hypotheses. A statement with no prediction AND no test is not
        #    falsifiable — that is the "investigate X further" failure mode, so refuse it here rather
        #    than trusting the prompt to have prevented it.
        for item in (rev.get("hypotheses") or [])[:2]:
            if not isinstance(item, dict):
                continue
            prediction, test = str(item.get("prediction", "")), str(item.get("test", ""))
            if not (prediction.strip() or test.strip()):
                continue
            h, refused = self._ledger.admit(
                str(item.get("statement", "")), prediction=prediction, test=test,
                rival=str(item.get("rival", "")),
                discriminator=str(item.get("discriminator", "")),
                origin_step=step, design=self._design_facts())
            if h is not None:
                emit({"type": "hypothesis_formed", "id": h.id, "statement": h.statement,
                      "prediction": h.prediction, "test": h.test, "rival": h.rival,
                      "discriminator": h.discriminator, "origin_step": step,
                      "surprise": str(rev.get("surprise", ""))[:300]})
            elif refused and "already holds" not in refused:
                # Say why. A hypothesis refused in silence looks identical to one never proposed,
                # and the reason is the most useful thing the gate produces.
                emit({"type": "hypothesis_refused", "reason": refused,
                      "statement": str(item.get("statement", ""))[:300], "origin_step": step})

        # 3. Accept the steps that test a hypothesis we hold, are new, and are real analysis.
        existing = {_norm_step(s) for s in plan_steps}
        # A step the user declined, or did not answer about, is not proposed again after the next
        # result — neither word for word nor as another test of the same hypothesis.
        existing |= {_norm_step(p["step"]) for p in self._proposed_steps}
        turned_down = {p.get("hypothesis_id") for p in self._proposed_steps}
        added: list[str] = []
        for item in (rev.get("new_steps") or [])[:2]:
            if not isinstance(item, dict) or len(added) >= room:
                continue
            text = str(item.get("step", "")).strip()
            key = _norm_step(text)
            if not key or key in existing or _is_report_busywork(text):
                continue
            h = self._ledger.find(str(item.get("hypothesis", "")))
            if h is None:   # orphan step — no hypothesis behind it, so no reason to spend a step on it
                continue
            if h.id in turned_down:
                continue
            existing.add(key)
            if not self._approve_new_step(text, h, step, emit):
                turned_down.add(h.id)
                continue
            added.append(text)
            self._ledger.link_test(h.id, text)
            emit({"type": "step_added", "step": text, "hypothesis_id": h.id,
                  "statement": h.statement, "after_step": step})
        self._new_steps_added += len(added)
        return added

    def _approve_new_step(self, text: str, hypothesis: Any, after_step: str, emit: EventFn) -> bool:
        """May a step the plan did not contain be added? Only when the user says so.

        The plan the user approved is the plan. Prod run 1e887ed2bf8e ("Run CellQC … quality
        control report") added an "Ambient rod-leak audit" after CellQC without asking, spent three
        failed attempts on it, and the user had asked for one step. So: ask through the decision
        card, and add the step on an explicit yes. A no, no reply, or nobody to ask (bypass mode, a
        resumed run) leaves the step out and records it, so it can be asked for afterwards. After
        one unanswered proposal the run stops asking — nobody is there — and only records."""
        from types import SimpleNamespace
        why = str(getattr(hypothesis, "statement", "") or "")
        record = {"step": text, "why": why, "after_step": _step_title(after_step),
                  "hypothesis_id": getattr(hypothesis, "id", "")}

        def left_out(decision: str) -> bool:
            self._proposed_steps.append({**record, "decision": decision})
            emit({"type": "step_proposed", **record, "decision": decision})
            return False

        if not self.config.new_steps_need_approval:
            return True
        ask = self._decision_review
        if ask is None or self._step_asks_unanswered:
            return left_out("not_asked")
        yes, no = "Add this step", "Do not add it"
        fork = SimpleNamespace(
            id=f"add-step-{len(self._proposed_steps) + self._new_steps_added + 1}",
            kind="add_step", decision=True, options=(yes, no), timeout_s=NEW_STEP_ASK_SECONDS,
            goal=(f"The result of “{record['after_step']}” prompted a step that is NOT in your "
                  f"plan: {text} — Why: {why or 'no reason given'}. It runs only if you approve "
                  "it; without a reply it is left out and listed at the end."))
        decision = ask(fork) or {}
        if decision.get("timed_out"):
            self._step_asks_unanswered = True
            return left_out("unanswered")
        if str(decision.get("action", "")).lower() == "cancel":
            return left_out("declined")
        choice = str(decision.get("choice", "")).strip().lower()
        if choice == yes.lower() or choice.startswith(_APPROVAL_WORDS):
            emit({"type": "step_approved", **record})
            self._approved_additions.append(text)
            return True
        return left_out("declined")

    def _plan_review(self, question: str, agenda: list[str], emit: EventFn) -> list[str]:
        """PI↔Critic review of the WHOLE draft agenda, BEFORE any step runs — the plan-time complement
        to the per-step meetings. The single planning pass is one-shot with no second look; this is that
        second look, at the plan's SOURCE (an incoherent plan is caught before any compute is spent).
        The Critic flags orphan/circular/incoherent steps and proposes a revision; the PI (owns the
        plan) finalizes. ``never worse than the draft``: any empty/oversized/garbled revision falls back
        to the original. No-op returning the draft unless ``config.plan_review`` (and 2+ steps)."""
        from ..mcp_workflow import explicit_workflow
        explicit = explicit_workflow(question)
        if explicit and agenda == [call.step(i) for i, call in enumerate(explicit, 1)]:
            emit({"type": "plan_review_preserved", "source": "explicit_mcp",
                  "reason": "The researcher supplied exact calls; schema checks and human review remain."})
            return agenda
        if not self.config.plan_review or len(agenda) < 2:
            return agenda
        profile = self._dataset_context()
        # What each tool the plan names actually computes and returns. Without it the Critic was
        # asked to judge redundancy against tool outputs and "does the description match the
        # tool" while seeing only the question, the profile and the agenda — so a DDX41 plan kept
        # a run_code step that re-derived the per-direction depth verdict run_depth_matched_de
        # already returns, and a figures step re-drawing what run_de/run_enrichment write.
        contracts = plan_tool_contracts(agenda, self.scientist.catalog)
        # A limit the user set is not a gap in the plan: both reviewers are told, and the result
        # is filtered by the callers.
        scope_note: dict[str, Any] = (
            {"user_scope": self._scope.prompt_block()} if self._scope.limited else {})
        # How much the request asked for, and what this conversation already did: without them the
        # review measures the plan against the dataset and puts back what the request left out.
        if self._request_breadth:
            scope_note["request_breadth"] = self._request_breadth
        if self.config.prior_work:
            scope_note["earlier_work"] = self.config.prior_work
        raw_critic = self._complete([
            {"role": "system", "content": _PLAN_REVIEW_CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"research_question": question, "dataset_profile": profile, "draft_agenda": agenda,
                 "tools_named_in_plan": contracts, **scope_note})},
        ], role="critic")
        critic = _parse_verdict(raw_critic)
        if critic is None:
            # An unreadable review is not a clean one. It used to fall through as "no issues", so
            # a plan the Critic had objected to reached the reviewer looking approved.
            emit({"type": "plan_review_unparsed", "chars": len(raw_critic or ""),
                  "preview": " ".join((raw_critic or "").split())[:200]})
            critic = {}
        issues = [str(x).strip() for x in (critic.get("issues") or []) if str(x).strip()]
        proposed = retick_tools([str(s).strip() for s in (critic.get("revised_agenda") or [])
                                 if str(s).strip()], [t.name for t in self.scientist.catalog])
        if not issues and (not proposed or proposed == agenda):
            return agenda   # Critic found nothing wrong → no PI round, plan unchanged
        pi = _parse_verdict(self._complete([
            {"role": "system", "content": _PLAN_REVIEW_PI_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"research_question": question, "dataset_profile": profile, "draft_agenda": agenda,
                 "tools_named_in_plan": contracts, **scope_note,
                 "critic_issues": issues, "critic_revised_agenda": proposed})},
        ], role="plan")) or {}
        tool_names = [t.name for t in self.scientist.catalog]
        final = retick_tools([str(s).strip() for s in (pi.get("final_agenda") or [])
                              if str(s).strip()], tool_names)
        if not final or len(final) > self.config.max_steps:
            return agenda   # never worse than the draft
        # Deterministic floor on how much a review may remove: it may HALVE a plan, not gut one.
        # A prompt cannot be trusted to hold this line alone — measured over 12 plans on a
        # deliberately narrow question, the review cut a mean of 4.6 steps to 1.9 by reading the
        # question literally and calling the QC, composition and enrichment steps "orphans".
        # Half is where the two cases separate: dropping 2 orphans from a 4-step plan is the job
        # this review exists for, while 5 steps down to 2 is a misread of the question, and the
        # draft — which at least covered the data — is the safer of the two. Rejections are
        # reported, never silent: a review that changed nothing and one that was overruled look
        # identical from outside, and only one of them means the plan was sound.
        if len(final) < max(2, (len(agenda) + 1) // 2):
            emit({"type": "plan_review_rejected", "before": list(agenda), "proposed": final,
                  "reason": f"the review proposed dropping {len(agenda) - len(final)} of "
                            f"{len(agenda)} steps; keeping the draft"})
            return agenda
        # A MISSING PRIMARY ANALYSIS is reported, not negotiated away. Measured on drafts whose DE
        # step was missing: the Critic named the omission 10 times out of 10, and the step still
        # reached the final agenda about half the time — the loss is in the handoff (Critic prose
        # -> Critic revised_agenda -> PI), not in the detection.
        #
        # Be clear about what this block does and does not buy, because four attempts at the
        # repair rate (prompt wording, then this control flow) all measured 50-67% on n<=12, which
        # at a ~50% base rate is one distribution, not four. It does NOT reliably repair the plan.
        # What it does change is categorical: before it, every unrepaired plan shipped silently;
        # now 5 of 6 emit ``plan_review_unresolved``. The retry is kept because it is bounded to
        # one call and its fallback (the Critic's own revision) is strictly more consistent with
        # the issue raised than the PI's answer that ignored it — not because it was shown to lift
        # the rate. If you touch this, measure on n in the hundreds or do not claim a change.
        _missing = _missing_analysis_family(issues) if issues else None
        if _missing and not _names_tool(final, _missing):
            retry = _parse_verdict(self._complete([
                {"role": "system", "content": _PLAN_REVIEW_PI_SYSTEM},
                {"role": "user", "content": json.dumps(
                    {"research_question": question, "dataset_profile": profile,
                     "draft_agenda": agenda, **scope_note, "critic_issues": issues,
                     "critic_revised_agenda": proposed,
                     "unresolved": f"The Critic reported that the plan is missing an analysis "
                                   f"that none of your steps computes. Add a step that calls one "
                                   f"of {list(_missing)}, keeping every other step you already "
                                   f"decided on."})},
            ], role="plan")) or {}
            second = retick_tools([str(x).strip() for x in (retry.get("final_agenda") or [])
                                   if str(x).strip()], tool_names)
            if second and len(second) <= self.config.max_steps and _names_tool(second, _missing):
                final = second
            elif _names_tool(proposed, _missing) and len(proposed) <= self.config.max_steps:
                final = proposed
            else:
                emit({"type": "plan_review_unresolved", "issues": issues, "agenda": list(final),
                      "reason": "the Critic reported a missing primary analysis and neither the "
                                "PI nor the Critic's revision supplied one"})
        if final != agenda:
            emit({"type": "plan_review", "issues": issues, "before": list(agenda), "after": final,
                  "reason": str(pi.get("reason", ""))})
        return final

    def _annotation_label_col(self) -> "str | None":
        """The dataset's existing cell-type label column, if any — the anchor for the labels-vs-
        recluster fork. ``None`` when no dataset is loaded or none of its obs columns look like a
        cell-type annotation."""
        dr = (self.ctx.decisions or {}).get("dataset_result")
        if not isinstance(dr, dict):
            return None
        cats = dr.get("obs_categoricals") or {}
        universe = {**(cats if isinstance(cats, dict) else {}),
                    **{k: {} for k in (dr.get("obs_keys") or [])}}
        labels = [c for c in universe if _looks_like_celltype_col(c)]
        return labels[0] if labels else None

    def _label_decision(self, agenda: "list[str]") -> "tuple[str, tuple[str, ...]] | None":
        """The (goal, options) for the 'analyze by the existing labels vs re-cluster de-novo' fork —
        returned only when the dataset ALREADY carries cell-type labels AND the plan clusters de-novo
        (the choice materially changes the result and has no single right answer). Deterministic (no
        LLM), so the fork fires even when the model would not flag it; ``None`` otherwise."""
        label = self._annotation_label_col()
        if not label or not _plan_has_clustering(agenda):
            return None
        goal = (f"This dataset already carries cell-type labels (`{label}`), and the plan also clusters "
                "the cells de-novo. How should the biological analysis (differential expression / "
                "enrichment) be grouped?")
        options = (f"Use the existing '{label}' labels",
                   "Re-cluster de-novo and use the new clusters",
                   f"Both — reconcile the de-novo clusters against '{label}'")
        return goal, options

    def _synthesize(self, question: str, rounds: list[LabRound], emit: EventFn,
                    team_interpretation: str = "") -> str:
        accepted = [r for r in rounds if r.verdict.verdict == "accept"]
        qc_report = False
        if qc_only_run(rounds):
            # Quality control is all this run did (generic code that wrote a table or figure is
            # analysis: run 74873590ca7b's ISG scoring ran in run_code after run_scanpy_qc, and was
            # written up as a QC report with no claim audit). Unless the request asks for a written report, it
            # is answered in the chat with the QC tool's own numbers, which the gateway builds from
            # the run's files. Run 6275df1213ab (CellQC alone) had the writer produce Summary,
            # Methods, Results, Limitations and Conclusion around one table, after a claim audit that
            # asked of each QC count whether it claimed causation in a two-arm comparison.
            # Asked for = the plan kept a write_report step (#102), which it has when the request
            # asked for a written report.
            if not self._report_requested:
                emit({"type": "synthesize", "accepted": len(accepted), "qc_only": True})
                return "\n\n".join(str(r.scientist_result.get("final_answer") or "").strip()
                                   for r in (accepted or rounds[-1:])).strip()
            # A report asked for: the writer writes it, as a QC report, with no claim audit (its
            # checks are for findings about conditions; a QC run has none).
            qc_report = True

        def _step_line(r: LabRound) -> str:
            answer = r.scientist_result.get("final_answer") or "(no textual answer)"
            # Surface the REAL artifacts each accepted step produced so the report is
            # grounded in tool outputs, not only the scientist's prose — and so a step
            # that produced an artifact but little narrative is not reported as empty.
            artifacts = [
                result_digest(s.get("result"))
                for s in r.scientist_result.get("steps", [])
                if step_succeeded(s)
            ]
            arts = f"\n  artifacts: {json.dumps(artifacts)}" if artifacts else ""
            return f"- Step {r.step_index} ({r.step}): {answer}{arts}"

        summary = "\n".join(_step_line(r) for r in accepted) or "(no steps were accepted)"
        emit({"type": "synthesize", "accepted": len(accepted)})
        # Team mode threads the team's interpretation-meeting synthesis into the report so the
        # write-up reflects the multi-expert reading of the results, still grounded in the tools.
        interp = (f"\n\nTeam interpretation of the results (incorporate, but do not invent beyond "
                  f"the tool outputs):\n{team_interpretation}\n" if team_interpretation else "")
        # The dataset's own class labels (its profile's label column, whichever tool the steps used)
        # and the curated reference that may spell them out: run 74873590ca7b's writer, given none,
        # turned MG into "Microglia-associated" in a dataset where Microglia is another class.
        profile = (self.ctx.decisions or {}).get("dataset_result")
        label_column, class_labels = _class_labels(accepted, profile)
        label_reference = _label_reference(accepted, profile)
        vocab = _grounding_vocab(accepted, profile, label_reference)
        vocab_block = f"\n{vocab}\n" if vocab else ""
        # The plan's record of the choices the PI made itself (a gene set, a threshold). Run
        # 74873590ca7b's answer presented the PI's own 14-gene set as "a frozen ISG set".
        choices = own_choices.writer_note(self._self_sourced)
        choices_block = f"\n{choices}" if choices else ""
        methods = _methods_performed(accepted)
        methods_block = f"\n{methods}\n" if methods else ""
        facts = _grounding_facts(accepted)          # pin the authoritative numbers/assembly (anti-fabrication)
        facts_block = f"\n{facts}\n" if facts else ""
        # Hypothesis-driven runs: the paths the run GENERATED are a first-class result, so the report
        # must say which were tested and how they came out — including the refuted and the still-open
        # ones. Reporting only the supported ones would turn a research loop into a confirmation
        # machine. Empty (and the whole block absent) unless exploration actually produced something.
        ledger = self._ledger.render()
        ledger_block = (
            f"\n{ledger}\n\nThese hypotheses were generated DURING the run, not planned up front. "
            "Report them honestly: state which were tested and their outcome, name any that were "
            "REFUTED (a refuted hypothesis is a real result, not a failure to hide), and flag any "
            "left open as open. Do NOT present an open or refuted hypothesis as a finding.\n"
        ) if ledger else ""
        # CLAIM AUDIT, run BEFORE writing. The checks the writer should apply to its own conclusions are
        # asked one short question at a time: the model answers them correctly alone, but does not apply
        # them while writing (see agents/claim_audit.py). A failed audit never costs the report.
        audit_block = ""
        if accepted and claim_audit.enabled() and not qc_report:
            try:
                dataset_result = (self.ctx.decisions or {}).get("dataset_result")
                self._claim_audit = claim_audit.audit(lambda m: self._complete(m, role="audit"),
                                                      summary, question, dataset_result,
                                                      tested_counts=_tested_counts(accepted))
                audit_block = claim_audit.render_block(self._claim_audit)
                emit({"type": "claim_audit",
                      "claims": [{"claim": c["claim"], "status": c["status"]}
                                 for c in self._claim_audit.get("claims", [])]})
            except Exception as exc:  # noqa: BLE001 - the audit is advisory; the report still gets written
                emit({"type": "claim_audit_error", "error": f"{type(exc).__name__}: {exc}"})
        audit_prompt = f"\n{audit_block}\n" if audit_block else ""
        report = self._complete([
            {"role": "system", "content": _SYNTH_SYSTEM},
            {"role": "user", "content": (
                f"Research question:\n{question}\n\n"
                f"Accepted step results (ground the report ONLY in these):\n{summary}\n"
                f"{methods_block}"
                f"{facts_block}"
                f"{vocab_block}"
                f"{choices_block}"
                f"{ledger_block}"
                f"{audit_prompt}"
                f"{interp}\n"
                + (_QC_REPORT_NOTE if qc_report else "")
                + "Write the final report now."
            )},
        ], role="writer")
        # Guarantee layer: deterministically CORRECT any fabricated assembly / non-PASS claim that slipped
        # past the grounding prompt, and surface each correction as a diagnostic (not into the manuscript).
        facts_dict, _dists = _collect_facts(accepted)
        report, fact_issues = verify_report_facts(
            report, facts_dict, _uncovered_groups(accepted), tested_counts=_tested_counts(accepted),
            pre_counts=pre_qc_counts(profile),
            marker_reference=_marker_reference(accepted),
            class_labels=class_labels, label_column=label_column, label_reference=label_reference)
        if fact_issues:
            emit({"type": "report_fact_check", "issues": fact_issues})
        # The plan's own-judgement record, verbatim, whether or not the writer followed the note.
        return own_choices.add_section(report, self._self_sourced)

    # -- internals ------------------------------------------------------------

    def _complete_concurrent(self, message_lists: list[list[dict[str, Any]]]) -> list[str]:
        """Run several independent single-shot completions CONCURRENTLY, preserving order.

        A100 throughput lever: one vLLM serves the whole team, so issuing the experts'
        requests together lets vLLM's continuous batching run them in one GPU pass instead of
        N sequential round-trips. ``_complete`` is a blocking HTTP call (it releases the GIL on
        I/O), so a bounded thread pool gives real concurrency; the cap keeps in-flight requests
        within the KV-cache budget. Falls back to sequential for 0/1 items (no thread overhead)."""
        if not message_lists:
            return []
        if len(message_lists) == 1:
            return [self._complete(message_lists[0])]
        from ..loop_utils import ContextThreadPoolExecutor
        workers = max(1, min(len(message_lists), self.config.max_meeting_concurrency))
        with ContextThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(self._complete, message_lists))   # ex.map preserves input order

    def _check_cancelled(self) -> None:
        cancel = getattr(self, "_should_cancel", None)
        if cancel is not None and cancel():
            raise _RunCancelled()

    def _complete(self, messages: list[dict[str, Any]], *, role: str = "reason") -> str:
        # Budget FIRST, then dispatch — the injected ``complete_fn`` (production wires one in
        # via the gateway's ``_lab_llm``) must receive the SAME trimmed prompt, otherwise the
        # single-shot budgeting is dead code on the only path production takes and the prompt
        # overflows the served window again. A trimmed prompt also leaves the model real
        # output room, so vLLM never computes a 0-token output budget ("requested 0 output
        # tokens"). The injected fn keeps its ``(messages) -> str`` contract.
        #
        # ``role`` is ADVISORY and travels only to an injected fn that asks for it (see
        # ``_call_with_role``). ``max_tokens`` computed above is a window-fitting number for the
        # cluster's own server; an endpoint that bills per token needs a spending ceiling instead,
        # which only the gateway knows how to set — so the role, not the number, is what crosses.
        cancellable = getattr(self, "_planning", False) or getattr(self, "_meeting_active", False)
        if cancellable:
            self._check_cancelled()
        budgeted, max_tokens = self._budget_single_shot(messages)
        if self._complete_fn is not None:
            result = _call_with_role(self._complete_fn, budgeted, role)
        else:
            from ...gateway import vllm_client  # deferred: keep agents decoupled from the gateway
            result = vllm_client.complete(
                self.ctx.tunnel_port, self.ctx.model, budgeted, max_tokens=max_tokens
            )
        if cancellable:
            self._check_cancelled()
        return result

    def _budget_single_shot(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Fit a single-shot system+user prompt into the served window AND reserve reply room.

        Unlike the Scientist loop there is no growing history here — the overflow risk is one
        oversized user payload (the Critic forwarding every step's digest, or synthesize
        bundling every accepted step's artifacts). We let the reply use whatever the window
        leaves after the prompt, but never below ``reply_reserve_tokens``; if the prompt is so
        large that less than that would remain, we TRUNCATE the largest user message so the
        reply is guaranteed its room — and so vLLM never defaults the output budget to 0 (the
        "requested 0 output tokens" failure). The window/margin come from the Scientist's
        ``HarnessConfig`` so there is one source of truth for the served context length.
        """
        hc = self.config.scientist
        margin = hc.context_safety_margin
        # Never reserve more than half the window for the reply (guards a tiny/test window).
        min_reply = min(self.config.reply_reserve_tokens, max(256, hc.max_model_len // 2))
        input_cap = hc.max_model_len - min_reply - margin
        # Prefer the EXACT server-side count (vLLM /tokenize, via the Scientist's counter)
        # so this single-shot path senses the real boundary too; fall back to the char
        # estimate when no counter is wired (offline tests / remote API).
        prompt_tokens = self._prompt_tokens(messages)
        if prompt_tokens <= input_cap:
            # Room to spare: let the reply use everything left (>= min_reply) so a long
            # manuscript is not capped short.
            return messages, max(min_reply, hc.max_model_len - prompt_tokens - margin)

        # Overflow: truncate the largest user message to fit, guaranteeing min_reply output.
        out = [dict(m) for m in messages]
        user_indices = [i for i, m in enumerate(out) if m.get("role") == "user"]
        if user_indices:
            idx = max(user_indices, key=lambda i: len(out[i].get("content") or ""))
            marker = "\n…[truncated to fit the model context window]"
            others = sum(_msg_tokens(m) for j, m in enumerate(out) if j != idx)
            allowed_chars = int(max(0, input_cap - others) * _CHARS_PER_TOKEN)
            content = out[idx].get("content") or ""
            if len(content) > allowed_chars:
                out[idx]["content"] = content[: max(0, allowed_chars - len(marker))] + marker
            # Exact tightening: if the real tokenizer still puts us over (the estimate
            # undershot dense JSON), shrink the truncated message until it truly fits.
            for _ in range(6):
                exact = self._exact_tokens(out)
                if exact is None or exact <= input_cap:
                    break
                cur = out[idx].get("content") or ""
                out[idx]["content"] = cur[: max(0, int(len(cur) * 0.85) - len(marker))] + marker
        return out, min_reply

    def _exact_tokens(self, messages: list[dict[str, Any]]) -> int | None:
        """EXACT prompt tokens from the served tokenizer (reuses the Scientist harness's
        vLLM /tokenize counter; no tools on the single-shot path), or None when unavailable."""
        counter = getattr(self.scientist, "_exact_token_count", None)
        if counter is None:
            return None
        return counter(messages, [])

    def _prompt_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Exact server count when available, else the char estimate."""
        exact = self._exact_tokens(messages)
        return exact if exact is not None else sum(_msg_tokens(m) for m in messages)
