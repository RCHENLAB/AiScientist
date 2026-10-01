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

import inspect
import json
import re
import threading
from functools import lru_cache
from pathlib import Path
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import claim_audit
from .dag import LabPlan, TaskNode, lift_agenda_to_dag, parse_dag
from .hypotheses import DesignFacts, HypothesisLedger
from .loop_utils import safe_json_loads
from .preset_pipelines import (
    PresetPipeline,
    compose_pipeline_prompts,
    drop_conflicting_pinned,
    select_pipeline,
)
from .skills import (
    MANIFEST_MAX as SKILL_MANIFEST_MAX,
    SKILLS as ATOMIC_SKILLS,
    Skill,
    get_skill,
    make_search_skills_tool,
    make_skill_reference_tool,
    skill_manifest,
)
from .step_numbers import contrast_arms, describe_count_mismatches, find_count_mismatches
from .tool_source import make_tool_source_tool
from .research_harness import (
    EventFn,
    HarnessConfig,
    HarnessContext,
    HarnessResult,
    HarnessTool,
    ResearchHarness,
    _CHARS_PER_TOKEN,
    _msg_tokens,
    evidence_pointers,
    result_digest,
    step_succeeded,
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


# complete_fn(messages) -> assistant text. Plain reasoning turns for PI/Critic.
LabRoleFn = Callable[[list[dict[str, Any]]], str]
# code_executor(code) -> result dict. Runs a CodeAct snippet (sandbox on the server).
CodeExecutor = Callable[[str], dict[str, Any]]


_PI_SYSTEM = (
    "You are the Principal Investigator of a bioinformatics lab. Given a research question AND the "
    "list of tools your scientist can actually run, break the work into AS MANY CONCRETE, ORDERED "
    "steps as the analysis genuinely needs — plan EVERY analysis the question AND the data genuinely "
    "warrant; do NOT drop a step the design needs (e.g. a comparison the design calls for, annotation "
    "validation, DE) just to keep the list short — but equally, do NOT pad the plan with a step the "
    "data cannot give meaning to (see profile rule (d) on enrichment without a contrast). There is NO "
    "small step limit — completeness beats brevity: a thorough single-cell study is commonly 6-12 "
    "steps and up to ~20 is perfectly acceptable; never compress or merge real analyses to hit a "
    "smaller number. "
    "Each step must be ACHIEVABLE WITH THE AVAILABLE TOOLS (do not plan steps the tools cannot do; if a tool is "
    "limited, scope the step to what it returns). RULES: each step is ONE tool's worth of work — do "
    "NOT combine multiple analyses into one step (e.g. don't say 'run DE AND enrichment AND make a "
    "report'). Every step must be performed by one of the listed pipeline tools — do NOT plan steps "
    "that write your own scripts to reproduce what a tool already does. If the user asks for "
    "literature context, biological interpretation, background, references/citations, or a report "
    "grounded in published biology, you MUST reserve one agenda step for `deep_literature` near "
    "the end, using focused biomedical terms from the question and findings. Keep that literature "
    "step IN ADDITION to the analysis steps — never drop a real analysis to make room for it. "
    "Do NOT add that literature step only when the user explicitly "
    "asks for no literature. Do NOT add ANY step that "
    "writes, renders, packages, zips, organizes, exports, or duplicates a report/document/archive "
    "(no .docx/.pdf/.html/.zip, no 'compile the report', no 'bundle the outputs'): the figures, "
    "tables, and the final PDF+DOCX report are assembled AUTOMATICALLY when the run finishes, so any "
    "such step is wasted, duplicate work. Plan ONLY the analysis. "
    "USE THE DATASET PROFILE when one is given (cell/gene counts + the dataset's obs/metadata "
    "columns, with category values when few): plan around the data's ACTUAL design, not a generic "
    "template. (a) If a column looks like an experimental CONDITION/group — genotype, treatment, "
    "timepoint, knockout vs wild-type, etc. — with 2+ categories (e.g. a 'sampleid' with values "
    "like KO and WT), plan a step that COMPARES the groups (differential expression / abundance "
    "between conditions); do NOT settle for a condition-agnostic descriptive atlas when the design "
    "is clearly comparative. (b) If a column already holds cell-type or cluster ANNOTATIONS "
    "(e.g. celltype, majorclass, a predicted-label column), REUSE it — validate it against markers "
    "— rather than planning fully de-novo clustering + annotation from scratch. (c) Only reference "
    "columns that actually exist in the profile; never invent metadata. (d) If the data has NO "
    "experimental contrast — one sample/condition, i.e. every non-annotation obs column holds a "
    "single value — AND already carries cell-type labels, there is no differential question to "
    "answer: plan ONLY QC, a canonical-marker/UMAP check that VALIDATES the existing annotation, and "
    "a short descriptive summary. Do NOT plan pathway/GO enrichment or discovery-style DE in that "
    "case — enriching a known cell type's own identity markers is circular (it just restates the "
    "cell type's definition), and de-novo re-clustering to re-derive existing labels is redundant. "
    "START EVERY STEP WITH A SHORT BOLD TITLE: `**<2-6 word action title>** — <the full step "
    "text>`. The title names the analysis (e.g. **QC & normalization**, **Stratified DDX41 vs WT "
    "contrast**, **Pathway enrichment per cell type**); the em-dash and the detailed prose follow "
    "on the same line. A reviewer scans the titles to see the pipeline's shape, then reads the "
    "prose of the steps they care about — a wall of ten dense paragraphs with no headings cannot "
    "be scanned at all. "
    "WRITE EACH STEP FOR A RESEARCHER TO READ, IN PLAIN SCIENTIFIC ENGLISH — a complete, self-"
    "contained sentence describing the ANALYSIS and what it reveals biologically: the action, what "
    "it is performed on, its scientific purpose, and what the reader learns or what it produces "
    "(cell populations, marker genes, enriched pathways, condition differences). "
    "NAME THE METHOD, AND SAY WHAT IT DOES. A researcher approving this plan does not read code, "
    "and this is the only place they can see what the code is about to do to their data — so a "
    "step that says 'assess differential expression' and silently means a per-cell Wilcoxon test "
    "across an experimental condition is a plan they cannot review. Brevity is NOT the goal here; "
    "a step that is short but unreviewable has failed. Every step that runs a tool must give, in "
    "prose, all four of:\n"
    "  (a) the TOOL, in backticks — `run_pseudobulk_de`;\n"
    "  (b) the OPERATION it performs on the data, concretely enough to picture — 'sums each "
    "sample's raw counts into one profile per animal, then tests those profiles against each "
    "other', not 'performs differential expression';\n"
    "  (c) the UNIT the statistics are computed over, whenever a test is involved — cells, "
    "samples, donors — because that single word is the difference between a valid p-value and a "
    "pseudoreplicated one, and it is invisible in a tool name;\n"
    "  (d) the KEY SETTINGS WITH THEIR NUMBERS and what each number does — 'keeping cells that "
    "detect at least 200 genes and whose reads are under 10% mitochondrial (a stressed or dying "
    "cell exceeds that)'. State the value you will actually use, including when it is the tool's "
    "default: a reader cannot tell a default from a choice unless the plan says the number. Do "
    "NOT invent a value to look precise — if you have no reason to change a setting, use the "
    "default and say what it is. Write the numbers into the sentence rather than as keyword "
    "arguments or code (say '10% mitochondrial', not `max_pct_mt=10`), and never put file paths "
    "in a step. "
    "The example below shows ONLY the writing STYLE and level of detail — ADAPT "
    "the real steps to THIS question and dataset (apply the profile rules above); do NOT copy it "
    "verbatim or treat it as a fixed pipeline. "
    'Reply with ONLY a JSON object {"agenda": [step strings]}, e.g. '
    '{"agenda": ['
    '"**QC & normalization** — Measure each cell\'s quality with `run_scanpy_qc` and remove the ones that cannot support an '
    'analysis: cells detecting fewer than 200 genes (empty droplets and dying cells) and cells whose '
    'reads are more than 10% mitochondrial (stressed or lysed), plus genes seen in fewer than 3 '
    'cells. Normalize and log-transform what remains so later comparisons reflect biology rather '
    'than sequencing depth, and report how many cells and genes survived.", '
    '"**Clustering & UMAP** — Group the cells into transcriptionally distinct populations with `run_clustering` and lay them '
    'out on a 2-D map, so the major cell types present in the tissue can be seen, separated, and '
    'counted; the clustering granularity is left at the tool default.", '
    '"**Marker genes per population** — Identify the genes that mark each population using `run_de` in marker mode: a Wilcoxon '
    'rank-sum test comparing every cell of one population against all remaining cells, keeping the '
    '50 strongest per population at an adjusted p-value below 0.05 (Benjamini-Hochberg). Because '
    'the comparison is between groups of cells drawn from the same sample, the cell is a legitimate '
    'unit here. This establishes the molecular identity of each cell type.", '
    # The example is copied verbatim into real plans, so a stale number here becomes a stale number
    # in production. It used to say "testing the top 100", which stayed in plan cards after
    # `run_enrichment`'s cap was removed (default `top_n_genes=0`): the plan promised an arbitrary
    # truncation the tool no longer performs, and a top-N of a per-cell-type list is not comparable
    # across cell types anyway, since each has a different number of significant genes.
    '"**Pathway enrichment** — Determine with `run_enrichment` which biological pathways and Gene Ontology terms are '
    'over-represented among each population\'s marker genes, testing EVERY gene that passes the '
    'significance thresholds (adjusted p below 0.05 and |log2 fold-change| at least 0.25) rather '
    'than an arbitrary top-N of them, against the set of '
    'genes actually measured in this experiment rather than a generic genome-wide background, which '
    'would inflate every p-value. This turns gene lists into interpretable biology.", '
    '"**Literature grounding** — Search the published literature with `deep_literature` for the key genes and pathways found '
    'above and attach real, DOI-backed citations that support the biological interpretation."'
    ']}. '
    "DECLARE WHAT THE PROTOCOL DID NOT COVER. A research protocol cannot anticipate every dataset, "
    "so going beyond it is expected and often correct — but it must be VISIBLE, because a reader "
    "otherwise cannot tell a protocol-mandated choice from one you made. After the analysis steps, "
    "add ONE final step string that begins exactly with `SELF-SOURCED:` and lists, in plain "
    "language, every decision in this plan that came from YOUR OWN judgement rather than from the "
    "guidance or the dataset profile: a threshold you chose and why that value, a method the "
    "guidance did not name, a step you added or dropped, and any place where the guidance did not "
    "fit this dataset and you departed from it — say which and why. If the question itself asked "
    "for something the guidance advises against, say THAT here explicitly: name the conflict, say "
    "which you followed, and what it costs. Write `SELF-SOURCED: none — every choice in this plan "
    "comes from the guidance and the dataset profile.` when that is genuinely true. This is a "
    "DISCLOSURE, not an analysis step: it runs nothing, and it is the last item.\n"
    "CLARIFY (optional): if — and only if — the user prompt explicitly allows you to ask, AND the "
    "request is GENUINELY ambiguous in a way that would materially change the plan (not a trivial "
    "detail), you MAY instead return "
    '{"clarify": [{"question": "<one focused question>", "options": ["<concrete choice>", "..."]}]} '
    "with 1-3 questions, each offering 2-4 concrete options. The user can always type a custom answer, "
    "so do not add an 'other' option yourself. Prefer drafting a sensible default plan over asking; "
    "never ask about formatting/packaging (those are automatic)."
)

_CRITIC_SYSTEM = (
    "You are a rigorous scientific Critic. You are given the research question, the current step, "
    "and the scientist's tool-execution results — the ACTUAL structured outputs each tool returned "
    "(``tool_results``: status, artifact paths, counts, etc.), plus any tool errors. Judge the step "
    "against those REAL outputs, not just the scientist's prose: a step that produced a valid artifact "
    "(a predictions file, figures, computed metrics) should be ACCEPTED even if the write-up is terse, "
    "and a transient error that a later retry recovered from is not a failure. Each tool result also "
    "carries ``evidence``: the on-disk artifact paths (figures, tables, result files) it actually wrote, "
    "and a top-level ``evidence`` array unions them for the whole step. Ground your verdict in that "
    "evidence — a concrete factual claim (a gene, a count, a figure) that NO evidence artifact backs is "
    "unsupported and should NOT be accepted on prose alone. When you state a COUNT (genes, cells, "
    "clusters, enriched terms), read it from an explicit count field (e.g. ``de_rows_by_group``, "
    "``n_genes_per_group``, ``n_groups``) or the cited table — NEVER from the length of a ``top_*`` or "
    "other preview list, which is a capped sample, not the total. "
    "WHERE DID THE NUMBER COME FROM? A threshold that decided this result must be traceable to "
    "one of: the tool's DECLARED default, the research protocol, or an explicit choice made for "
    "this run (the step brief and the tool results state which). A number with none of those is "
    "not wrong, but it is unaccounted for, and a result that turns on it cannot be scored above "
    "0.8 until the write-up says so — because 'we filtered at 10%' reads identically whether 10 "
    "was the field's convention or something invented in the moment. You have `read_tool_source` "
    "for exactly this: it returns a tool's body plus every default it applies, with what each one "
    "means. Use it when a threshold decided the headline number and you cannot see where it came "
    "from — not on every step, and not to second-guess a conventional value. Say in the critique "
    "which number you could not account for. "
    "The ``score`` is a GRADED 0.0–1.0 measure of how strong THIS step's result is — NOT a restatement "
    "of the verdict — so two accepted steps must differ when their evidence differs. Anchor it to these "
    "bands and do NOT default to 1.0: "
    "0.95–1.0 = goal fully met, every quantitative claim tied to an evidence artifact, nothing left to "
    "improve; "
    "0.8–0.95 = solid, usable result but a minor claim is thin/under-explained or a small sub-goal is "
    "unmet; "
    "0.6–0.8 = usable result that only partially meets the goal, or a material claim rests on prose with "
    "no backing artifact (accept, but say what is unsupported); "
    "below 0.6 = no usable result, the step goal was not met, or a claim is contradicted by the tool "
    "results — use \"revise\". Reserve a score above 0.9 for a step you cannot suggest an improvement to. "
    'Reply with ONLY a JSON object: {"verdict": "accept" | "revise", "score": <0.0-1.0>, "critique": '
    '"<what is missing or wrong, and what to do next>"}. Use "revise" only if NO usable result was '
    "produced, the step goal was not met, or a claim is contradicted by the tool results. Whenever you "
    "score below 0.95, the critique MUST name the specific gap that kept it from full marks. Be specific "
    "so the scientist can fix it."
)

_SYNTH_SYSTEM = (
    "You are the Principal Investigator writing the final research report. Ground the report ONLY in "
    "the accepted step results provided. Do NOT invent numbers, gene symbols, statistics, pathway or "
    "enrichment terms, or cell-type labels — if a pathway, marker, or cell type is not in the "
    "results, it does not exist for this report. Use the cell-class labels EXACTLY as given; never "
    "rename, merge, or expand one label into a different or additional cell type. "
    "DESCRIBE ONLY METHODS THAT WERE ACTUALLY PERFORMED — the Methods and Results may mention only the "
    "analyses that appear in the accepted step results below. Do NOT describe, name, or reference any "
    "tool, algorithm, software, model, or analysis that was not run (e.g. do NOT mention foundation-"
    "model annotation such as scGPT, multi-omics integration such as MOFA+/DIABLO, RNA velocity, "
    "trajectory inference, or batch integration unless it appears in the accepted steps), and NEVER "
    "write that a method was 'planned', 'attempted', or 'did not execute' — a method that was not run "
    "is simply omitted. Inventing an un-run method, even as a failed attempt, is fabrication. "
    "When a step reported nothing (enrichment found no terms, no citations, etc.), say so plainly "
    "rather than filling the gap with plausible-sounding biology. "
    "Report EXECUTION PARAMETERS — the genome assembly/build, filter thresholds, the PASS vs non-PASS "
    "counts, the number of variants/cells annotated — from the accepted TOOL RESULTS, NOT from the plan "
    "or agenda text: the plan states INTENDED parameters, but a tool may have auto-corrected them (e.g. "
    "the variant annotation reads the VCF's real genome build from its header and may OVERRIDE the "
    "planned assembly — state the assembly and execution_mode the result reports, never the plan's). "
    "Never state a PASS/non-PASS split, an allele-frequency cutoff, or a total the results do not show — "
    "do NOT write 'all PASS' or '0 non-PASS' unless the results report zero non-PASS. "
    "State limitations honestly and frame conclusions as hypotheses to validate."
)

# Axis B — PI-autonomous skill selection. Researchers do not know which protocol they
# need; the PI reads the skill library's one-line descriptions and picks one itself. The
# chosen skill's body then STEERS planning (it does not bypass the PI). Wording is kept
# distinct from the PI/Critic system prompts so offline test routers can tell them apart.
# --- DAG planner (feat/dag-planner) ------------------------------------------
# Structure an already-drafted flat agenda into a dependency DAG: for each step, which EARLIER
# steps it directly consumes. This changes ONLY execution ordering/scoping — the agenda text the
# user reviewed in plan mode is unchanged.
_DAG_STRUCTURE_SYSTEM = (
    "You are structuring an ordered analysis plan into a dependency DAG. You are given steps with "
    "ids (s1, s2, …). For EACH step, list which EARLIER steps it DIRECTLY depends on — i.e. it "
    "consumes that step's output/checkpoint (the data matrix, the clustering, a DE table, "
    "annotations). A step usually depends only on the step that produced the data it reads, NOT on "
    "every earlier step. Steps that are independent of each other (e.g. a literature search vs a "
    "differential-expression analysis) must NOT depend on each other. "
    "ALSO flag genuine METHODOLOGICAL FORKS as human decision points: set \"decision\": true and "
    "provide 2-4 short \"options\" ONLY for a step where a human should choose the approach because "
    "the choice materially changes the result and there is no single obvious answer — e.g. the "
    "dataset already carries cell-type labels (analyze by existing labels vs re-cluster de-novo), "
    "clustering resolution / granularity, or which contrasts to run. Do NOT flag routine steps (QC, "
    "running a standard tool) — most steps are NOT decisions. Do NOT flag a step whose text already "
    "says which approach to take (e.g. 'reuse the existing labels, do not re-cluster'): the plan was "
    "reviewed, so that choice is made. Do NOT flag the labels-vs-re-cluster fork when no step "
    "clusters the cells de-novo. Reply with ONLY a JSON array covering "
    'every id, e.g. [{"id": "s1", "depends_on": []}, {"id": "s2", "depends_on": ["s1"], '
    '"decision": true, "options": ["Use existing majorclass labels", "Re-cluster de-novo", "Both"]}, '
    '{"id": "s3", "depends_on": ["s2"]}, {"id": "s4", "depends_on": ["s2"]}]. Do not change the steps.'
)

# When several tasks are READY (dependencies met), the Coordinator picks which to run next — this
# is where the system stops being a fixed pipeline and an agent chooses the path through the graph.
_COORDINATOR_SYSTEM = (
    "You are the Coordinator scheduling a research workflow. Several tasks are READY (all their "
    "dependencies are already done). Choose the ONE most useful task to run next, given the research "
    "goal and what is already done — e.g. finish the core analysis chain before optional/background "
    "tasks. Reply with ONLY a JSON object naming a ready task id, e.g. {\"next\": \"s3\"}."
)

# Real multi-agent: instead of routing a task by keyword, the team's experts CLAIM the task whose
# expertise fits best — the agents decide who does what.
_CLAIM_SYSTEM = (
    "You are assigning the next task to ONE member of a research team. Given the task and each "
    "member's expertise, choose the SINGLE best-fit member to carry it out. Prefer the most specific "
    "relevant expertise; use a generalist only when nothing fits. Reply with ONLY a JSON object naming "
    "the member by number, e.g. {\"member\": 2}."
)


def _node_step_text(node: TaskNode) -> str:
    """The scoped brief for one DAG node: the goal PLUS explicit reuse/produce hints so the Scientist
    does ONLY this task and reuses upstream checkpoints instead of recomputing them (the structural
    cure for the 'step 1 runs the whole pipeline' / double-QC failure modes)."""
    parts = [node.goal]
    if node.consumes:
        parts.append("Reuse these existing inputs/checkpoints as-is (do NOT recompute them): "
                     + ", ".join(node.consumes) + ".")
    if node.produces:
        parts.append("This task is expected to produce: " + ", ".join(node.produces) + ".")
    if node.suggested_tool:
        parts.append(f"Suggested tool: {node.suggested_tool} (use it if it fits the task).")
    parts.append("Do ONLY this task — earlier tasks already ran and their outputs exist; do not "
                 "repeat or re-run them.")
    return " ".join(parts)


# A step's own ordinal, echoed back into its text. The plan reaches the PI NUMBERED
# ("1. **QC & normalization** — …") on the patch path and in the Critic's read-back, and the model
# copies that "1. " prefix into the step it hands back. Nothing stripped it, so the stored step
# became "1. **QC …** — …" while its neighbours stayed "**Title** — …" — and the console's step
# renderer, which reads a step's title off a LEADING ``**bold**``, fell through to its untitled
# branch: the reviewer got a bare "1" heading followed by raw markdown, for exactly the step they
# had just asked to change. Measured in Ziyao's plan_mode_report_v2_5 as 6/6 on the in-place
# revision path and 0/4 on add/delete, which never round-trips a numbered plan. Stripped at the
# SOURCE so every consumer — the plan card, the progress feed, plan.md, and the agenda the
# Scientist executes — sees ONE step shape.
_STEP_ORDINAL_RE = re.compile(r"^\(?\d{1,2}[.)、]\s+")


def _strip_step_ordinal(text: str) -> str:
    """Drop a leading numeric list marker ("3. ", "3) ", "(3) ") from one step's text.

    Deliberately narrow. A separator AND a following space are both required, so prose that opens
    with a number keeps it — "0.25 is the single-cell convention" and "2,000 highly-variable genes"
    both survive — and a prefix that is real CONTENT rather than a list marker ("Step 3: …") is left
    for the model to own."""
    return _STEP_ORDINAL_RE.sub("", (text or "").strip(), count=1).strip()


def _parse_agenda(raw: str, max_steps: int) -> list[str] | None:
    """Parse the PI's step list from a JSON array (tolerating code fences, a
    surrounding sentence, or a ``{"steps": [...]}`` / ``{"agenda": [...]}`` object)."""
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
    if isinstance(parsed, dict):
        parsed = parsed.get("steps") or parsed.get("agenda")
    if isinstance(parsed, list) and parsed:
        steps = [s for s in (_strip_step_ordinal(str(x)) for x in parsed) if s]
        return steps[:max_steps] or None
    return None


# The PI's disclosure of what it decided on its OWN knowledge rather than from the research-path
# guidance or the dataset profile. Prefixed rather than returned as a separate JSON field because
# the planning reply is a plain list of strings on every path (draft, patch, cycle), and one
# recognisable prefix survives all of them.
_SELF_SOURCED_PREFIX = "SELF-SOURCED:"


# Telling a QUESTION about the plan from a REQUEST to change it. Plan mode had three actions —
# approve / revise / cancel — so anything a reviewer typed became "revise": asking "why is step 3 a
# Wilcoxon test?" did not get an answer, it triggered a re-plan. And a whole-plan redraft damages
# the steps nobody mentioned (measured: 100% of trials silently lost an unmentioned step), so the
# cost of asking a question was losing part of the plan.
#
# Deterministic first, and biased toward QUESTION on purpose. The two errors are not symmetric:
# reading a change request as a question wastes one exchange, while reading a question as a change
# request rewrites work the reviewer never asked to touch.
_QUESTION_MARKS = ("?", "？")
# English fronts its question words, so matching at the START is right and avoids catching
# "use wilcoxon, and tell me what it does".
_QUESTION_OPENERS = (
    "why", "what", "what's", "whats", "how", "how's", "which", "who", "where", "when",
    "is ", "are ", "does ", "do ", "did ", "can ", "could ", "should ", "will ", "would ",
    "explain", "tell me",
)
# Chinese does not: the question word sits mid-sentence ("第3步为什么用 Wilcoxon"), and a
# startswith rule sent exactly that reply down the re-plan path.
_QUESTION_ANYWHERE = (
    "为什么", "为啥", "怎么", "如何", "哪个", "哪些", "是不是", "能不能", "会不会",
    "有没有", "多少", "解释", "说明一下", "讲一下", "什么意思", "是什么", "干什么", "做什么",
    "到底", "了什么", "过什么", "算什么", "指什么",
)
_CHANGE_VERBS = (
    "change", "use ", "add ", "remove", "delete", "drop ", "replace", "instead", "rerun",
    "re-run", "swap", "set ", "make it", "don't", "do not", "skip ", "merge", "split",
    "改", "换", "加上", "加一", "删", "去掉", "不要", "改成", "换成", "增加", "移除", "跳过",
)
# The change verbs are checked first, which is right for "could you use 0.5 instead?" and wrong for
# "why do you use wilcoxon here?" — the same verb, one a request and one a question ABOUT the
# request's subject. These openers cannot front a request in either language, so they settle it
# before the verb scan: asking why a step removes cells must not be read as "remove that step".
# The modal openers ("could you", "can you", "should we") deliberately stay behind the verb scan,
# because those DO front polite requests.
_PURE_INTERROGATIVES = (
    "why", "what", "what's", "whats", "how", "how's", "which", "who", "where", "when",
    "explain", "tell me",
)
# ...except when the interrogative fronts a PROPOSAL rather than a question.
_PROPOSAL_OPENERS = ("how about", "what about")
_PURE_INTERROGATIVES_ZH = ("为什么", "为啥")


def classify_plan_reply(text: str) -> str:
    """``"question"`` or ``"change"`` for a reviewer's reply in plan mode.

    Pure and deterministic so the common cases never cost a model call, and so the rule is
    testable. A reply carrying a change verb is a change even if it is phrased as a question
    ("could you use 0.5 instead?"); everything else that looks interrogative is a question;
    anything left over is a change, since a bare imperative ("stratify by majorclass") is the
    normal way to ask for one."""
    t = (text or "").strip()
    if not t:
        return "change"
    low = t.lower()
    if ((low.startswith(_PURE_INTERROGATIVES) and not low.startswith(_PROPOSAL_OPENERS))
            or any(z in t for z in _PURE_INTERROGATIVES_ZH)):
        return "question"
    if low.startswith(_PROPOSAL_OPENERS):
        return "change"      # "how about 0.5" proposes one; it does not ask about one
    if any(v in low for v in _CHANGE_VERBS):
        return "change"
    if (t.endswith(_QUESTION_MARKS) or low.startswith(_QUESTION_OPENERS)
            or any(m in t for m in _QUESTION_ANYWHERE)):
        return "question"
    return "change"


_PLAN_QA_SYSTEM = (
    "You are the Principal Investigator, answering a researcher's question about the analysis plan "
    "you just proposed. They are deciding whether to approve it. ANSWER THE QUESTION — do not "
    "rewrite, re-propose, or re-list the plan, and do not treat the question as a request to change "
    "anything. Ground the answer in the plan, the research-path guidance and the dataset profile "
    "you are given; where the answer is a methodological one, say plainly what the method does and "
    "why it suits (or does not suit) THIS dataset, including the unit the statistics run over. If "
    "the honest answer is that the plan has a weakness, say so and name the change that would fix "
    "it — but do not make the change; the researcher decides. If the question cannot be answered "
    "from what you have, say what is missing. A few clear sentences; no headings, no preamble."
)


def _split_self_sourced(steps: "list[str]") -> "tuple[list[str], str]":
    """Separate the ``SELF-SOURCED:`` disclosure from the executable agenda.

    Returns ``(agenda, disclosure_text)``. The disclosure is a statement about the plan, not work
    to run — left in the agenda it would be dispatched to the Scientist as a step. Empty string
    when the PI made no disclosure. Tolerates the prefix appearing on any line, since models put it
    first about as often as last."""
    agenda: list[str] = []
    notes: list[str] = []
    for s in steps:
        t = (s or "").strip().lstrip("*-# ").strip()
        if t.upper().startswith(_SELF_SOURCED_PREFIX):
            body = t[len(_SELF_SOURCED_PREFIX):].strip()
            if body:
                notes.append(body)
        else:
            agenda.append(s)
    return agenda, " ".join(notes).strip()


def _drop_question_echo(steps: "list[str]", question: str) -> "tuple[list[str], list[str]]":
    """Remove agenda items that are just the research question (or a trivial restatement of it)
    echoed back as a "step". Seen in production (run 97dfc89dc5aa): the PI's reply led with the
    question as item 1; it parsed as a step, could never be executed, failed three rounds and
    raised a decision card. Deterministic: an item is an echo when, after stripping markdown and
    punctuation, it equals the question or is a near-copy (>=90% of its words are the question's
    words and it adds none of its own beyond three). Returns (kept, dropped)."""
    import string

    def _norm_words(text: str) -> list[str]:
        t = re.sub(r"[*_`#]", " ", text.lower())
        t = t.translate(str.maketrans("", "", string.punctuation))
        return t.split()

    qw = _norm_words(question)
    qset = set(qw)
    kept: list[str] = []
    dropped: list[str] = []
    for step in steps:
        w = _norm_words(step)
        if not w:
            dropped.append(step)
            continue
        wset = set(w)
        covers_question = sum(1 for x in qset if x in wset) >= 0.9 * max(1, len(qset))
        extra = sum(1 for x in w if x not in qset)
        # An echo RESTATES the question: it contains essentially the whole question and adds
        # almost nothing. A short legitimate step whose words happen to appear in the question
        # ("Annotate the cells") covers only part of it and is kept.
        if w == qw or (covers_question and extra <= 3 and len(w) <= len(qw) + 3):
            dropped.append(step)
            continue
        kept.append(step)
    # Never empty the agenda: a plan that was ONLY an echo falls back to the raw list.
    return (kept, dropped) if kept else (steps, [])


def _parse_plan(raw: str, max_steps: int, allow_clarify: bool) -> tuple[str, Any] | None:
    """Parse the PI's planning reply into ``("agenda", [steps])`` or, when ``allow_clarify``
    and the PI asked, ``("clarify", [{"question", "options"}])``. Returns ``None`` if neither
    can be recovered (the caller falls back to a trivial agenda)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    parsed: Any = None
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL) or re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None

    if allow_clarify and isinstance(parsed, dict) and isinstance(parsed.get("clarify"), list):
        questions = []
        for q in parsed["clarify"][:3]:
            if not isinstance(q, dict):
                continue
            text = str(q.get("question", "")).strip()
            options = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()][:4]
            if text and options:
                questions.append({"question": text, "options": options})
        if questions:
            return ("clarify", questions)

    agenda = _parse_agenda(raw, max_steps)
    if agenda:
        return ("agenda", agenda)
    return None


def _parse_verdict(raw: str) -> dict[str, Any] | None:
    """Parse the Critic's JSON object, tolerating a chatty/thinking model that wraps
    the ``{...}`` in prose or code fences (safe_json_loads needs the whole string to
    be JSON; this extracts the first object block as a fallback)."""
    parsed = safe_json_loads(raw)
    if parsed is None:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    return parsed if isinstance(parsed, dict) else None


def make_run_code_tool(executor: CodeExecutor | None) -> HarnessTool:
    """The hybrid CodeAct tool: the Scientist can write-and-run Python when the
    curated tools don't cover a step. Execution is delegated to ``executor`` (a
    sandboxed runtime on the server); without one it returns a clear 'not enabled'
    rather than running arbitrary code in-process."""

    def _exec(args: dict[str, Any], ctx: HarnessContext) -> dict[str, Any]:
        code = str(args.get("code", "")).strip()
        if not code:
            return {"status": "error", "error": "no code provided"}
        if executor is None:
            return {
                "status": "not_enabled",
                "note": "Code execution is not enabled for this run (needs a sandboxed CodeAct runtime).",
            }
        # Provenance stamp (reproducibility): inject a deterministic seed, time the run, and
        # attach a CodeProvenance record to the result so it flows into the step/round/report
        # unchanged. We hash the ORIGINAL snippet (not the seed-prefixed one that actually ran).
        from . import provenance as _prov

        seed = _prov.resolve_seed() if _prov.seeding_enabled() else None
        to_run = (_prov.seed_preamble(seed) + code) if seed is not None else code
        started_at, t0 = _prov.now_iso(), _prov.perf_now()
        result = executor(to_run)
        duration_ms = int((_prov.perf_now() - t0) * 1000)
        if isinstance(result, dict):
            try:
                result["provenance"] = _prov.build_code_provenance(
                    code=code, seed=seed, executor=executor, ctx=ctx,
                    result=result, started_at=started_at, duration_ms=duration_ms,
                ).to_dict()
            except Exception:  # provenance must never break a run
                pass
        return result

    from .sandbox import build_run_code_context

    description = (
        "Write and run a short Python snippet (CodeAct) for custom ANALYSIS the other tools do not "
        "cover — scanpy/pandas/numpy etc. Read the dataset from the env var AISCIENTIST_DATASET, the "
        "run's checkpoints from AISCIENTIST_WORK, and write new figures/tables under AISCIENTIST_ARTIFACTS "
        "(use os.environ). Returns stdout / a result dict. Set a headless backend (matplotlib Agg). "
        # This used to say "pip install of analysis packages is fine if a step needs one", which
        # was measured to be false AND actively harmful: inside the sandbox pip can report success
        # while the very next import still fails (the user-site dir did not exist at interpreter
        # start, so it is not on sys.path), and nothing survives to the next step anyway. Missing
        # imports are now detected by parsing this code BEFORE it runs and resolved once.
        "Just import what you need: the imports in your code are checked before it runs, and "
        "anything missing is offered to the user for a one-time install into the lab-shared "
        "package area. Do NOT pip-install from inside the snippet (it does not persist and can "
        "silently half-work), and do NOT hand-write a replacement for a library you were denied. "
        "STRICT: use this ONLY for analysis (compute figures into figures/, tables into tables/). Do "
        "NOT write or render a report or document (no .docx/.pdf/.html, no python-docx/reportlab/"
        "pandoc), and do NOT zip/package/export the outputs: the final PDF+DOCX report and the "
        "downloadable bundle of everything under AISCIENTIST_ARTIFACTS are created AUTOMATICALLY when "
        "the run finishes. Building your own report or archive is duplicate work and will be discarded."
    )
    # Append the LIVE execution-environment context (real paths, obs schema, CWD + memory caveats)
    # so the model stops guessing column names / relative paths / working directory — the largest,
    # most repetitive class of run_code failures. Static for the run → computed once here.
    description += build_run_code_context(executor)

    return HarnessTool(
        name="run_code",
        description=description,
        parameters={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
        executor=_exec,
        reads_private_data=False, category="codeact",
    )


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


# --- concurrency safety (DAG scheduler) --------------------------------------
# Two ready nodes may run CONCURRENTLY only if their mutable footprints are disjoint. The scanpy
# analysis line shares BOTH the on-disk checkpoint chain (adata_qc/clustered/de.h5ad) AND scanpy's
# process-global state (sc.settings.figdir) — so every analysis/code node carries an "__analysis__"
# sentinel and no two of them can overlap. A literature/background node touches no checkpoint and no
# scanpy state (it reads accepted findings in-memory + hits an external API), so its footprint is
# empty and it CAN run alongside the analysis chain. This is the conservative default: anything not
# clearly independent stays sequential.
_READ_ONLY_TOOLS = frozenset({"literature_search", "deep_literature", "make_schematic"})
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

# On a hard failure, instead of a generic retry/skip, the LLM proposes CONCRETE alternative approaches
# for THIS step — the replacement options a human picks from (manual) or the agent auto-applies (bypass).
_ALTERNATIVES_SYSTEM = (
    "A single step in a research plan FAILED. Given the step, WHY it failed (the Critic's reason), the "
    "tools available, and the accepted upstream findings, propose 2-4 CONCRETE, DISTINCT alternative "
    "ways to accomplish THIS step — a different parameter, a different tool, a different method, or "
    "reusing an existing input. Each option is a SHORT imperative phrase a scientist can act on "
    "(e.g. 'Lower the Leiden resolution to 0.5', 'Use the existing cell-type labels instead of "
    "re-clustering', 'Reduce highly-variable genes to 2000', 'Aggregate to pseudobulk before the test'). "
    "Do NOT change the research GOAL — only change HOW this one step is done. "
    "NEVER propose a hand-rolled Python reimplementation of an operation that genuinely requires a "
    "specialized binary plus reference data — e.g. left-aligning indels / normalizing a VCF without "
    "bcftools and the reference genome, or read realignment without the aligner. A pure-Python 'manual' "
    "version silently produces WRONG results, which is worse than not running the step. For those, only "
    "offer an alternative that uses the correct tool where it is actually available, or prefer skipping "
    "and letting a downstream step that performs the operation cover it. "
    "Order them best-first. If the step genuinely cannot be salvaged, return fewer (or an empty array). "
    "Reply with ONLY a JSON array of short strings."
)


def _node_resources(node: TaskNode) -> set[str]:
    if _is_literature_step(node.goal) or (node.suggested_tool in _READ_ONLY_TOOLS):
        return set()                          # external/read-only: no shared mutable resource
    return {"__analysis__", *node.produces, *node.consumes}


def _concurrency_safe(a: TaskNode, b: TaskNode) -> bool:
    """True when nodes ``a`` and ``b`` can safely run at the same time (disjoint footprints)."""
    return _node_resources(a).isdisjoint(_node_resources(b))


# A tool named in backticks. The optional argument list matters: the PI is INSTRUCTED to write
# "the TOOL in backticks … and the key settings with their numbers", which it does as
# `run_de(stratify_by="majorclass", reference="WT")` — and the old pattern, which required the
# closing backtick immediately after the name, matched none of those. Every guard that asks "does
# this step name a real analysis tool?" therefore saw NO tool on exactly the steps that specified
# one most precisely; see the literature-routing guard in _is_literature_step for what that cost.
_BACKTICK_TOOL_RE = re.compile(r"`(run_[A-Za-z0-9_]+)(?:\s*\([^`]*\))?`")   # case-tolerant: `run_wilcoxon_DE`

# The same shapes, but for ANY tool name rather than the ``run_`` family. Nine of the catalog's
# twenty-two tools do not start with ``run_`` — ``scgpt_annotate``, ``annotate_variants``,
# ``map_phenotype_to_hpo``, ``diagnose_disease``, ``inspect_dataset`` among them — and every guard
# built on the ``run_`` patterns above was blind to exactly those. That is not cosmetic: the guard
# in _is_literature_step exists to stop a step being routed to the literature path on a stray word,
# and a step reading "Run `scgpt_annotate` … to request reference-transferred labels" hit the word
# `reference`, showed the guard NO tool, and was routed to literature — four literature_search calls,
# scgpt_annotate never invoked, and (literature steps do not retry) silently force-advanced.
# Backticks also hold obs column names (`majorclass`, `sampleid`), so a bare identifier is NOT
# evidence of a tool: candidates are intersected with the real catalog below.
_BACKTICK_NAME_RE = re.compile(r"`([A-Za-z][A-Za-z0-9_]*)(?:\s*\([^`]*\))?`")
_ANY_TOOL_CALL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\s*\(([^)]*)\)")

#: Tools whose presence MAKES a step a literature step. The catalog's real names — the historical
#: set named ``run_literature``/``run_paperqa``, neither of which the Scientist catalog serves, so
#: once the guard could see tool names at all a genuine ``literature_search`` step would have been
#: classified as NOT-literature. Kept together to make that pairing impossible to miss.
_LITERATURE_TOOLS = frozenset({
    "literature_search", "deep_literature", "run_literature", "run_paperqa", "run_paperqa_search",
})


@lru_cache(maxsize=1)
def _known_tool_names() -> frozenset[str]:
    """Every tool name the Scientist catalog actually serves.

    Deferred + cached: the registry pulls the whole tool tree, and this is consulted per step.
    An empty set on failure degrades to the ``run_`` prefix test below, i.e. exactly the previous
    behaviour — a classifier must never be the reason a run cannot start.
    """
    try:
        from .registry import build_scientist_catalog
        return frozenset(t.name for t in build_scientist_catalog())
    except Exception:  # noqa: BLE001 - classification degrades, it does not fail
        return frozenset()


def _declared_tools(step: str) -> frozenset[str]:
    """The tools a step DECLARES — the structured fact in an otherwise prose plan.

    The PI is instructed to name the tool in backticks (``Run `run_de(reference="WT")` …``), so the
    declaration is reliable where the surrounding prose is not. Candidates are intersected with the
    real catalog so a backticked obs column (`majorclass`) is never mistaken for a tool; when the
    catalog cannot be read we fall back to the ``run_`` family, which is what this used to see.
    """
    text = step or ""
    candidates = set(_BACKTICK_NAME_RE.findall(text)) | {
        m.group(1) for m in _ANY_TOOL_CALL_RE.finditer(text)}
    known = _known_tool_names()
    if known:
        return frozenset(c for c in candidates if c in known)
    return frozenset(c for c in candidates if c.startswith("run_"))


# ---------------------------------------------------------------------------
# Plan-time tool checking (deterministic; runs BEFORE the reviewer sees the plan)
#
# A plan is a promise about which tools will run with which settings, and until now nothing checked
# that the promise could be kept. Three of these reached production plan cards, unmarked, and would
# each have failed only after approval — with the reviewer's compute already committed:
#
#   `run_scanpy_de`  — in a plan NOBODY had edited; the tool is `run_de`
#   `run_pseudobulk(groupby_col=…)`  — the tool is `run_pseudobulk_de`, and `groupby_col` is invented
#   `run_de(method="DESeq2")`        — `method` goes straight to scanpy, which has no DESeq2 backend
#
# This is not a knowledge gap in the model: the SAME model, on the redraft path in the same test
# session, wrote `run_pseudobulk_de(sample_key=…, condition_key=…, group_key=…)` correctly and
# predicted its guard would trip. What was missing was the check. (Ziyao, plan_mode_report_v2_5,
# B-3.) So: catch it here, name the real tool where the intent is unambiguous, and put anything
# still unresolved in front of the human while approving is still a choice.
#
# The name→name corrections are only for cases where ONE registered tool is the obvious referent —
# never a guess between two. Everything else is reported, not rewritten.
_TOOL_NAME_FIXES = {
    "run_scanpy_de": "run_de",
    "run_scanpy_diffexp": "run_de",
    "run_de_scanpy": "run_de",
    "run_rank_genes_groups": "run_de",
    "run_wilcoxon_de": "run_de",
    "run_pseudobulk": "run_pseudobulk_de",
    "run_pseudobulk_deseq2": "run_pseudobulk_de",
    "run_scanpy_clustering": "run_clustering",
    "run_leiden": "run_clustering",
    "run_leiden_clustering": "run_clustering",
    "run_qc_scanpy": "run_scanpy_qc",
    "run_scanpy_quality_control": "run_scanpy_qc",
    "run_ora": "run_enrichment",
    "run_enrichr": "run_enrichment",
    "run_gsea": "run_gsea_prerank",
    "run_gseapy_prerank": "run_gsea_prerank",
    "run_doublet": "run_doublet_detection",
    "run_scrublet": "run_doublet_detection",
    "run_composition_analysis": "run_composition",
}

# ``run_de(groupby="sampleid", method="DESeq2")`` — the call as a plan writes it. Only the argument
# NAMES and any quoted/bare literal values are read; nothing is executed and nothing is inferred
# from a call the regex cannot parse.
_TOOL_CALL_RE = re.compile(r"\b(run_[A-Za-z0-9_]+)\s*\(([^)]*)\)")
_KWARG_RE = re.compile(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([A-Za-z0-9_.\-]+))""")


def _tool_schemas(catalog: "list[Any]") -> "dict[str, dict[str, Any]]":
    """``{tool name: JSON-schema properties}`` for every tool in the catalog."""
    out: dict[str, dict[str, Any]] = {}
    for tool in catalog or ():
        name = getattr(tool, "name", None)
        if name:
            out[str(name)] = ((getattr(tool, "parameters", None) or {}).get("properties") or {})
    return out


def check_plan_tooling(agenda: "list[str]", catalog: "list[Any]") -> "tuple[list[str], list[dict[str, Any]]]":
    """``(agenda with unambiguous tool-name typos corrected, list of remaining findings)``.

    Pure and deterministic — no model call — so it fires on every plan, on every path, and is
    testable without a GPU. Each finding is ``{kind, tool, detail}`` where ``kind`` is one of:

    * ``renamed``       — a name was corrected to the registered tool it obviously meant
    * ``unknown_tool``  — no such tool, and no single obvious referent
    * ``unknown_param`` — the tool exists but does not declare that argument
    * ``bad_value``     — the argument's value is outside the closed set the schema declares

    An empty ``catalog`` yields no findings: a caller with no tools cannot judge, and inventing
    findings from an unknown registry would be worse than silence."""
    schemas = _tool_schemas(catalog)
    if not schemas:
        return list(agenda), []
    findings: list[dict[str, Any]] = []
    fixed: list[str] = []
    for step in agenda:
        text = str(step)
        # 1. Names. Correct the unambiguous ones in place; report the rest.
        for raw in set(_BACKTICK_TOOL_RE.findall(text)) | {m.group(1) for m in _TOOL_CALL_RE.finditer(text)}:
            if raw in schemas:
                continue
            target = _TOOL_NAME_FIXES.get(raw.lower())
            if target and target in schemas:
                text = re.sub(rf"\b{re.escape(raw)}\b", target, text)
                findings.append({"kind": "renamed", "tool": raw, "detail": target})
            else:
                findings.append({"kind": "unknown_tool", "tool": raw, "detail": ""})
        # 2. Arguments — checked against the (possibly corrected) name's own schema.
        for call in _TOOL_CALL_RE.finditer(text):
            name, args = call.group(1), call.group(2)
            props = schemas.get(name)
            if not props:
                continue                      # already reported as unknown above
            for kw in _KWARG_RE.finditer(args):
                key = kw.group(1)
                value = next((g for g in kw.groups()[1:] if g is not None), "")
                spec = props.get(key)
                if spec is None:
                    findings.append({"kind": "unknown_param", "tool": name, "detail": key})
                    continue
                allowed = spec.get("enum")
                if allowed and value and value not in allowed:
                    findings.append({"kind": "bad_value", "tool": name,
                                     "detail": f"{key}={value!r}; allowed: {', '.join(map(str, allowed))}"})
        fixed.append(text)
    return fixed, findings

_LITERATURE_STEP_RE = re.compile(
    # `references?` must not fire on the `reference=` ARGUMENT of run_de — a step reading
    # `run_de(reference="WT")` was being classified as a literature step and routed to the
    # literature fast path, which is how the plan's central contrast goes missing.
    r"\b(literature|references?(?!\s*[=:])|citations?|published papers?|paper search)\b"
    r"|\b(?:published|biomedical|biological|scientific|literature)\s+background\b"
    r"|\bliterature\s+context\b|\bbiological\s+context\b|\bbiological\s+interpretation\b",
    re.I,
)
_REQUESTS_LITERATURE_RE = re.compile(
    r"\b(literature|references?|citations?|published papers?|paper search)\b"
    r"|\b(?:published|biomedical|biological|scientific|literature)\s+background\b"
    r"|\bliterature\s+context\b|\bbiological\s+context\b|\bbiological\s+interpretation\b",
    re.I,
)
_LOW_PRIORITY_FOR_LITERATURE_RE = re.compile(
    r"\b(enrich|enrichment|pathway|annotat|assign|label|marker genes per cluster|markers per cluster)\b",
    re.I,
)

#judging whether is literature step
def _is_literature_step(step: str) -> bool:
    """Is this step a literature search? Decided by the TOOL it declares, not by its prose.

    A step's role is a structured fact the planner already produces — the tool name — and reading it
    from the prose instead is what kept going wrong. The word ``reference`` is unavoidable in this
    domain (reference genome, reference allele, reference atlas, reference-transferred labels), and
    the phrase list below treats it as a literature signal; a DE step "…to guide biological
    interpretation" trips the list the same way. Both were real production failures. So: a step that
    declares a tool IS that tool's kind of step, full stop; the phrase list is the fallback for a
    step that names no tool at all, which is the only case where prose is the only evidence there is.
    """
    declared = _declared_tools(step)
    if declared:
        return bool(declared & _LITERATURE_TOOLS)
    # No tool named: judge the step by its HEADLINE (the bold title, else the first clause), not by
    # every word of its body. A tool-less analysis step routinely MENTIONS literature or a reference
    # in passing — "Reconcile transferred and original annotations … reference taxonomy …" and
    # "Reconcile evidence and generate the final report … literature context …" were both sent to
    # the literature path in run 3c5fbc8608a7 (rounds 12, 24) — while a real literature step says so
    # in its title ("Ground provisional mechanisms in literature"). And in the headline a singular
    # "reference" is an analysis word (reference atlas / genome / labels), so only the plural counts.
    headline = _step_headline(step).replace("_", " ")
    return bool(_LITERATURE_HEADLINE_RE.search(headline))


_LITERATURE_HEADLINE_RE = re.compile(
    r"\b(literature|references(?!\s*[=:])|citations?|published papers?|paper search|pubmed|europe\s*pmc)\b"
    r"|\b(?:published|biomedical|biological|scientific|literature)\s+background\b"
    r"|\bliterature\s+context\b|\bbiological\s+context\b|\bbiological\s+interpretation\b",
    re.I,
)


def _step_headline(step: str) -> str:
    """A step's title: the leading ``**bold**`` span the planner writes, else its first clause."""
    text = (step or "").strip()
    m = re.match(r"\*\*(.+?)\*\*", text)
    if m:
        return m.group(1)
    return re.split(r"(?<=[.;:!?])\s|\s[\u2014\u2013]\s|\s-\s", text, maxsplit=1)[0]

#judging whether user is asking for literature
def _requests_literature(*texts: str | None) -> bool:
    return any(_REQUESTS_LITERATURE_RE.search(text or "") for text in texts)

#if the 5 steps are full kick out ... in priority
# obs columns that carry an EXISTING cell-type annotation (as opposed to de-novo cluster labels
# like leiden/louvain, or non-biological metadata like donor/age/sex). When one is present the PI
# should ground marker/enrichment analysis in it, not in raw cluster numbers.
_CELLTYPE_COL_RE = re.compile(
    r"(?i)^(cell[_.]?type|major[_.]?class|sub[_.]?class|cell[_.]?class|cell[_.]?ontology"
    r"(?:[_.]?class)?|cell[_.]?label|celltype[_.]?annotation|annotation|predicted[_.]?label|"
    r"cell[_.]?identity)s?$"
)


def _looks_like_celltype_col(name: str) -> bool:
    return bool(_CELLTYPE_COL_RE.match(str(name or "").strip()))


# Per-cell QC / doublet bookkeeping. Low-cardinality like a design column and completely unlike one
# in meaning: `DF.classifications` holding the single value "Singlet" says the doublets were already
# removed, not that the study has one arm. Excluded from the replication summary so the columns that
# actually define the experiment are the only ones counted there.
_QC_COL_RE = re.compile(
    r"(?i)^(df[_.]?classification|doublet|scrublet|is[_.]?doublet|pann|percent[_.]?mt|pct[_.]?"
    r"counts?[_.]?mt|n[_.]?count|n[_.]?feature|nuclear[_.]?fraction|qc[_.]?)\w*s?$"
)


def _looks_like_qc_col(name: str) -> bool:
    return bool(_QC_COL_RE.match(str(name or "").strip()))


# A step whose ENTIRE action is to re-read what an earlier step already wrote to disk. It produces
# no new knowledge, and it costs a step: a production plan spent one of six on "Parse the generated
# DE tables to extract and report the exact tabular output", which the DE step had already
# returned. Deliberately much narrower than ``_is_report_busywork`` (which matches a bare "report"
# and would delete the perfectly good "…and report how many cells remain"): BOTH a read-back verb
# AND a reference to previously-produced output are required.
_READBACK_VERB_RE = re.compile(
    r"\b(parse|re-?read|tabulate|collate|transcribe|extract and report|report the exact"
    r"|report exactly|read out)\b", re.I)
_READBACK_TARGET_RE = re.compile(
    r"\b(generated|produced|resulting|existing|saved|written|output|above|previous|prior)\b"
    r"[^.]{0,40}?\b(tables?|results?|csvs?|files?|outputs?|statistics)\b", re.I)


def _is_readback_step(step: str) -> bool:
    s = (step or "").replace("_", " ")
    return bool(_READBACK_VERB_RE.search(s) and _READBACK_TARGET_RE.search(s))


# A pathway/GO/ORA enrichment step (as opposed to a marker-DE step, which is kept for annotation
# validation). Used to deterministically drop enrichment when the dataset has no experimental
# contrast — enriching a known cell type's identity markers is circular ("meaningless enrichment").
_ENRICHMENT_STEP_RE = re.compile(
    r"\b(enrich(?:ment|ed|es|ing)?|over[-\s]?represent(?:ation|ed|s|ing)?|pathway|gene[-\s]?ontology"
    r"|reactome|gsea|gene[-\s]?set[-\s]?enrichment|msigdb|hallmark gene set)\b",
    re.I,
)


# A step that PRODUCES a differential-expression table — what run_enrichment / run_gsea_prerank read.
_DE_PRODUCER_RE = re.compile(
    r"\b(run[_ ]de\b|run[_ ]pseudobulk[_ ]de|rank[_ ]genes[_ ]groups|differential(?:ly)?[- ]"
    r"express|marker genes?|pseudobulk|wilcoxon|deseq2?|DEGs?)\b", re.I)


def _is_de_producer_step(step: str) -> bool:
    if _is_literature_step(step) or _is_enrichment_step(step):
        return False
    return bool(_DE_PRODUCER_RE.search((step or "").replace("_", " ")))


def _is_enrichment_step(step: str) -> bool:
    # A literature / biological-interpretation step may MENTION "enriched pathways" without BEING an
    # enrichment-analysis step — never classify those as enrichment, so they survive the no-contrast
    # prune (only the actual ORA/pathway analysis step should be dropped).
    if _is_literature_step(step):
        return False
    return bool(_ENRICHMENT_STEP_RE.search((step or "").replace("_", " ")))


# Report/packaging busywork the PI is already told never to plan (the figures, tables and the final
# PDF+DOCX are assembled automatically when the run finishes). Re-checked DETERMINISTICALLY for a
# step proposed mid-run by exploration, where no plan-time review will catch it.
_REPORT_BUSYWORK_RE = re.compile(
    r"\b(write|writing|render|rendering|compile|compiling|assemble|assembling|package|packaging"
    r"|bundle|bundling|zip|export|exporting|organi[sz]e|organi[sz]ing)\b[^.]{0,40}"
    r"\b(report|manuscript|document|archive|deliverable|outputs?|figures? and tables?)\b"
    r"|\b(\.docx|\.pdf|\.html|\.zip)\b"
    # PURE-TRANSPORT verbs (export/package/bundle/zip/organize — never write/render, which a real
    # analysis step legitimately does to a summary table) aimed at what the tools already write:
    # "Export all computed DE tables … to the artifacts directory for automatic report assembly"
    # reached a plan card. Everything computed is already in artifacts/; the step is a slot for
    # nothing.
    r"|\b(export|exporting|package|packaging|bundle|bundling|zip|organi[sz]e|organi[sz]ing)\b"
    r"[^.]{0,60}\b(artifacts?(?: director(?:y|ies)| folder)?|tables?|results?|metrics)\b"
    # The step's stated PURPOSE, when its verb is an innocent one. Both branches above key on a
    # verb, and a live Qwen3.6 plan (2026-08-19, run 2) opened with "Generate" — which is also how
    # a real analysis step opens — so nothing fired on:
    #   "**Figures & tables** — Generate the visual summaries required FOR THE REPORT … volcano
    #    plots … heatmaps … enrichment bar plots … for automatic inclusion in the final PDF+DOCX
    #    report."
    # It ran: 8 run_code turns, 54 seconds, and produced two summary CSVs. Every figure it promised
    # was already on disk — `run_de` writes the volcanoes (_volcano) and `run_enrichment` writes the
    # bar plots — so the step re-made what the tools had already made and displaced a real analysis.
    # A purpose clause is the reliable signal: no genuine analysis step exists in order to be put
    # in the report. (The dotted-extension branch above missed it too: "PDF+DOCX" has no dots.)
    r"|\bfor\s+(?:automatic\s+)?inclusion\s+in\s+the\b"
    r"|\b(?:required|needed)\s+for\s+the\s+(?:final\s+)?(?:report|manuscript|deliverable)\b"
    r"|\b(?:pdf|docx|html)\s*[+/&]\s*(?:pdf|docx|html)\b",
    re.I,
)


def _is_report_busywork(step: str) -> bool:
    return bool(_REPORT_BUSYWORK_RE.search((step or "").replace("_", " ")))


def _norm_step(step: str) -> str:
    """Bag-of-words key for "is this the same step?". Used to stop exploration re-adding a step the
    plan already has under slightly different wording — the model's most common failure mode."""
    return " ".join(re.findall(r"[a-z0-9]+", (step or "").lower()))


# ---------------------------------------------------------------------------
# Resolving what a change request POINTS AT, before anything acts on it.
#
# Two measured failures, opposite in direction and neither of them announced (Ziyao,
# plan_mode_report_v2_5, B-5 and B-6):
#
#   "use resolution 0.5 for the clustering step" — sent to a plan with NO clustering step. Instead
#   of saying so, the redraft INVENTED one, complete with an `n_pcs=30, n_neighbors=15` the user had
#   never mentioned, wedged at position 2, consumed by nothing downstream. Asked the same thing as a
#   QUESTION ("how do I change the resolution?") the system answered correctly that the plan does
#   not cluster — so the two paths held different beliefs about the same plan.
#
#   "drop the enrichment step" — singular — sent to a plan with TWO (`run_enrichment` and
#   `run_gsea_prerank`). Both were deleted. The mirror case, "could you use padj 0.01 instead?" with
#   padj in two steps, changed exactly one. Delete took the maximum reading, modify took the
#   minimum, and neither said a choice had been made.
#
# One rule covers both: work out which steps the request names, and when the answer is "none" or
# "more than one", ask rather than act. Deterministic — a clarify that was not needed costs one
# exchange, while acting on the wrong reading costs the plan.
# Each topic pairs the words a REQUEST uses for it with the test for whether a PLAN STEP is one.
# The two are deliberately different jobs, and conflating them produced false alarms both ways:
# a step reading "…`run_enrichment` on the DE table" MENTIONS differential expression without being
# a DE step, and a user who says "the enrichment step" means either of ORA and GSEA — which is
# exactly why naming one of two is ambiguous. The step-side tests are the predicates the rest of
# the planner already uses, so a topic cannot drift from what the pruning guards believe.
_PLAN_TOPICS: "dict[str, tuple[re.Pattern[str], Callable[[str], bool]]]" = {
    "clustering": (
        re.compile(r"\b(cluster\w*|leiden|louvain|resolution)\b", re.I),
        lambda s: bool(_CLUSTERING_STEP_RE.search(s.replace("_", " ")))),
    "pathway enrichment": (
        re.compile(r"\b(enrichment|enrich\w*|ora|over.?representation|go terms?)\b", re.I),
        _is_enrichment_step),
    "GSEA": (
        re.compile(r"\b(gsea|prerank|pre.?ranked)\b", re.I),
        lambda s: bool(re.search(r"\b(gsea|prerank)\b", s.replace("_", " "), re.I))),
    "QC": (
        re.compile(r"\b(qc|quality control|percent\.?mt|mitochondrial)\b", re.I),
        lambda s: bool(re.search(r"\b(qc|quality control|scanpy qc)\b", s.replace("_", " "), re.I))),
    "differential expression": (
        re.compile(r"\b(differential expression|de|wilcoxon|rank.?genes)\b", re.I),
        _is_de_producer_step),
    "pseudobulk": (
        re.compile(r"\b(pseudobulk|pseudo.?bulk|deseq2?|edger)\b", re.I),
        lambda s: bool(re.search(r"\b(pseudobulk|deseq2?)\b", s.replace("_", " "), re.I))),
    "doublet detection": (
        re.compile(r"\b(doublets?|scrublet)\b", re.I),
        lambda s: bool(re.search(r"\b(doublets?|scrublet)\b", s.replace("_", " "), re.I))),
    "composition": (
        re.compile(r"\b(composition|proportions?|abundance)\b", re.I),
        lambda s: bool(re.search(r"\b(composition|proportions?|abundance)\b", s.replace("_", " "), re.I))),
    "batch integration": (
        re.compile(r"\b(integration|batch correct\w*|harmony|combat)\b", re.I),
        lambda s: bool(re.search(r"\b(integration|batch correct\w*|harmony|combat)\b", s.replace("_", " "), re.I))),
    "literature": (
        re.compile(r"\b(literature|papers?|citations?|pubmed)\b", re.I),
        _is_literature_step),
}

# An ADD is allowed to name something the plan does not have yet — that is what adding means. Only
# requests that point at an EXISTING step are checked for existence.
_ADD_INTENT_RE = re.compile(
    r"\b(add|insert|include|append|introduce|also run|also do|also add)\b"
    r"|加上|加一|增加|新增|添加|再跑|也跑", re.I)
def _is_definite_reference(request: str, word: str) -> bool:
    """True when ``word`` appears in ``request`` as a reference to an EXISTING step.

    A bare mention is not enough: "compare the arms with a wilcoxon test" names a method, while
    "the wilcoxon step" names a step the speaker believes is already in the plan. Only the second
    is checked for existence — the first is just how a new step gets described."""
    w = re.escape(word)
    return bool(
        # "the clustering step", "that enrichment analysis", "this DE step"
        re.search(rf"\b(?:the|that|this)\s+(?:\w+\s+){{0,3}}{w}\b", request, re.I)
        # "clustering step", "enrichment step" — the noun "step" makes it definite on its own
        or re.search(rf"\b{w}(?:\s+\w+){{0,2}}\s+步骤?\b|\b{w}(?:\s+\w+){{0,2}}\s+step\b", request, re.I)
        # "for the clustering", "in the DE", "from the enrichment"
        or re.search(rf"\b(?:for|in|from|of|on)\s+the\s+(?:\w+\s+){{0,3}}{w}\b", request, re.I)
    )


def _topics_named(request: str) -> "list[str]":
    """Plan topics the request refers to DEFINITELY (see :func:`_is_definite_reference`)."""
    named: list[str] = []
    for topic, (words, _) in _PLAN_TOPICS.items():
        if any(_is_definite_reference(request, m.group(0)) for m in words.finditer(request or "")):
            named.append(topic)
    return named


# Input carrying no change request. "hmm" was sent to the PI as a revision and came back with the
# ORA step's padj quietly moved from 0.05 to 0.01 — a statistical threshold changed by a filler
# word, with nothing on screen to say it had happened (report v2_5, B-4). A redraft is destructive
# by nature (measured: it damages steps nobody mentioned in 100% of trials), so it needs an actual
# instruction to justify it.
_NOISE_REPLY_RE = re.compile(
    r"^(?:hmm+|hm+|uh+|um+|er+|ah+|oh+|ok(?:ay)?|k|yeah?|yep|yes|no|nope|sure|fine|right|"
    r"thanks?|thank you|ty|cool|nice|good|great|\.{1,3}|\?+|!+|"
    r"嗯+|额+|呃+|哦+|噢+|好+的?|行|是|对|不|没有|谢谢|可以)$",
    re.I)


def is_noise_reply(text: str) -> bool:
    """True when a reviewer's reply asks for nothing — empty, punctuation, or a bare interjection.

    Only whole-string matches count: "ok, drop the GSEA step" is an instruction that happens to
    start with "ok", and must not be swallowed here."""
    t = (text or "").strip().strip(",.;:!?，。；：！？ ")
    return not t or bool(_NOISE_REPLY_RE.match(t))


# ``padj=0.05`` and ``padj 0.05`` / ``padj of 0.05`` — how plans state a setting either way.
_SETTING_RE = re.compile(
    r"\b([a-z_][a-z0-9_]{2,})\s*(?:=|:|\bof\b|\bto\b)?\s*[\"']?(-?\d+(?:\.\d+)?)[\"']?", re.I)


def _settings_in(step: str) -> "dict[str, str]":
    """``{parameter: value}`` for the numeric settings a step states."""
    return {m.group(1).lower(): m.group(2) for m in _SETTING_RE.finditer(step or "")}


def _similarity(a: str, b: str) -> float:
    """Token-overlap (Jaccard) between two steps — "is this the same step, reworded?"."""
    ta, tb = set(_norm_step(a).split()), set(_norm_step(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def diff_plans(before: "list[str]", after: "list[str]") -> "dict[str, list[Any]]":
    """What a redraft actually did: ``{"dropped", "added", "changed"}``.

    A whole-plan redraft is the one revision path that rewrites steps the reviewer never mentioned.
    Measured on three production redrafts: "also run GSEA and add a literature review step" deleted
    two unrelated steps and added a tool nobody asked for; "make the whole plan simpler" deleted the
    cross-cell-type synthesis; "hmm" moved a significance threshold. None of the three was reported
    anywhere in the console, so the only way to find out was to diff two plans by eye — which is
    what this does instead, deterministically, so the reviewer can accept or reject the collateral
    with the same look they give the plan (report v2_5, B-4).

    Steps are matched by token overlap, so a reworded step counts as the SAME step (its settings are
    then compared) rather than as one deletion plus one addition."""
    unmatched = list(range(len(after)))
    dropped: list[str] = []
    changed: list[dict[str, str]] = []
    for step in before:
        best, score = None, 0.45          # below this, two steps are different work, not a rewording
        for j in unmatched:
            if (s := _similarity(step, after[j])) > score:
                best, score = j, s
        if best is None:
            dropped.append(step)
            continue
        unmatched.remove(best)
        old_settings, new_settings = _settings_in(step), _settings_in(after[best])
        for key, old in old_settings.items():
            new = new_settings.get(key)
            if new is not None and new != old:
                changed.append({"step": after[best], "param": key, "before": old, "after": new})
    return {"dropped": dropped, "added": [after[j] for j in unmatched], "changed": changed}


def stale_downstream_settings(agenda: "list[str]", step_index: int,
                              param: str, old: str, new: str) -> "list[int]":
    """1-based steps AFTER ``step_index`` that still state ``param`` at its pre-change value.

    Changing a threshold in one step does not change the step that consumes its output. Asked to
    use ``padj 0.01``, the revision changed the DE step and left the ORA step below it saying it
    filters input genes at ``padj=0.05`` — while reading a table that by then contained only genes
    at padj<0.01. The plan contradicted itself, on paper, in front of the reviewer, and nothing
    said so (report v2_5, B-7). Reported rather than auto-propagated: whether the downstream number
    should follow is a methodological choice, and the reviewer is right there."""
    if old == new:
        return []
    return [i + 1 for i, step in enumerate(agenda)
            if i > step_index and _settings_in(str(step)).get(param.lower()) == old]


def resolve_plan_reference(request: str, agenda: "list[str]") -> "tuple[str, str, list[int]]":
    """``(verdict, topic, 1-based step numbers)`` for what a change request points at.

    ``verdict`` is ``"ok"`` (act on it), ``"missing"`` (the plan has no such step) or
    ``"ambiguous"`` (it has more than one, and the request named only one). ``"ok"`` is also the
    answer for a request that names nothing in particular — most requests — so this only ever
    interrupts when it has something specific to say."""
    if not request or not agenda or _ADD_INTENT_RE.search(request):
        return "ok", "", []
    for topic in _topics_named(request):
        is_step = _PLAN_TOPICS[topic][1]
        hits = [i + 1 for i, s in enumerate(agenda) if is_step(str(s))]
        if not hits:
            return "missing", topic, []
        if len(hits) > 1:
            return "ambiguous", topic, hits
    return "ok", "", []


# A step that clusters the cells de-novo (Leiden/Louvain). Used to detect the "analyze by existing
# labels vs re-cluster de-novo" methodological fork when the dataset already carries cell-type labels.
_CLUSTERING_STEP_RE = re.compile(r"\b(clusters?|clustering|clustered|leiden|louvain)\b", re.I)
# A step that says NOT to cluster is the plan deciding the fork, not a clustering step. Read as one,
# "reuse them and do not re-cluster" (the `\b` before "cluster" matches after the hyphen) raised
# "use the labels or re-cluster?" about the very step that had answered it (run f107bcf7b660).
_NEGATED_CLUSTERING_RE = re.compile(
    r"\b(?:do\s+not|don'?t|does\s+not|never|no|not|without|rather\s+than|instead\s+of|avoid(?:ing)?|"
    r"skip(?:ping)?)\s+(?:\w+\s+){0,3}?(?:de[-\s]?novo\s+)?(?:re-?)?"
    r"(?:clusters?|clustering|clustered|leiden|louvain)"
    r"(?:\s+(?:clusters?|clustering|clustered|leiden|louvain))*\b", re.I)


def _clusters_de_novo(step: str) -> bool:
    """True when a step CLUSTERS the cells de-novo, and not merely when it says not to."""
    text = _NEGATED_CLUSTERING_RE.sub(" ", (step or "").replace("_", " "))
    return bool(_CLUSTERING_STEP_RE.search(text))


def _plan_has_clustering(agenda: "list[str]") -> bool:
    return any(_clusters_de_novo(str(s)) for s in agenda)


def _is_label_fork(options: "Any") -> bool:
    """Whether a decision's options are the 'existing labels vs re-cluster' fork."""
    text = " ".join(str(o) for o in (options or ())).lower()
    return "label" in text and "cluster" in text


def _dataset_mode_rule(dr: "dict[str, Any] | None") -> str:
    """``"team"`` / ``"single"`` when the dataset profile settles it, else ``""``.

    team   — an experimental CONTRAST (a non-QC, non-label obs column with 2-3 levels) is present:
             the study is comparative, and comparative single-cell design is where a statistician,
             a biologist and a data expert disagree productively (unit of replication, composition
             confounding, stratification).
    single — cell-type labels present but NO contrast: a descriptive atlas; nothing to debate.
    ""     — no profile, or a profile with neither (a VCF, a folder, an unlabelled object): defer.
    """
    if not isinstance(dr, dict):
        return ""
    cats = dr.get("obs_categoricals") or {}
    if not isinstance(cats, dict) or not cats:
        return ""
    has_contrast = any(
        isinstance(info, dict) and isinstance(info.get("n"), int) and 2 <= info["n"] <= 3
        and not _looks_like_celltype_col(c) and not _looks_like_qc_col(c)
        for c, info in cats.items())
    has_labels = any(_looks_like_celltype_col(c) for c in cats)
    if has_contrast:
        return "team"
    if has_labels:
        return "single"
    return ""


def _annotated_without_contrast(dr: "dict[str, Any] | None") -> bool:
    """True when the dataset ALREADY carries a cell-type annotation column AND has NO experimental
    contrast — no non-annotation obs column with 2+ categories (a single sample / condition). In that
    regime there is no differential question, so pathway/GO enrichment on a cell type's own identity
    markers is circular (it just restates the cell type's definition) and should not be planned."""
    if not isinstance(dr, dict):
        return False
    cats = dr.get("obs_categoricals") or {}
    if not isinstance(cats, dict):
        return False
    universe = {**cats, **{k: {} for k in (dr.get("obs_keys") or [])}}
    annotated = any(_looks_like_celltype_col(c) for c in universe)
    has_contrast = any(
        isinstance(info, dict) and isinstance(info.get("n"), int) and info["n"] >= 2
        and not _looks_like_celltype_col(c)
        for c, info in cats.items()
    )
    return annotated and not has_contrast


# Tokens that look like biological ENTITIES: gene/protein symbols (DDX41, Trp53, ABCA4), cell types
# spelled as symbols (RGC, MG), pathway/complex names. Case-sensitive on purpose — an all-caps or
# Capitalized-plus-digit token in a question is almost always a symbol; a plain lowercase word is
# almost always instruction.
_ENTITY_TOKEN_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9-]{1,}\d[A-Za-z0-9-]*|[A-Z]{2,}[A-Za-z0-9-]*|[A-Z][a-z]{2,}\d+[a-z0-9]*)\b")
_LITERATURE_GENERIC = "Literature search for the key genes and pathways found"
# All-caps tokens that are METHOD / FORMAT names, not biology. Kept separate from the literature
# tool's stopword set because that set is lowercase-keyed and this one guards the symbol regex.
_LITERATURE_NOT_ENTITIES = {
    "WT", "KO", "DE", "QC", "PCA", "UMAP", "GO", "RNA", "DNA", "PDF", "CSV", "OK", "AND", "OR",
    "THE", "PLEASE", "TODO", "VCF", "VEP", "PASS", "GRCH37", "GRCH38", "HG19", "HG38", "BAM",
    "FASTQ", "H5AD", "TSV", "JSON", "API", "REST", "HPC", "GPU", "CPU", "LLM", "AI", "ORA", "GSEA",
    "FDR", "BH", "HVG", "MT", "SNP", "SNV", "INDEL", "CNV", "SV", "CADD", "REVEL", "SPLICEAI",
    "CLINVAR", "GNOMAD", "OMIM", "HPO", "ACMG", "DOI", "PMID", "PUBMED", "SCVI", "SCGPT",
    "SEURAT", "SCANPY", "DESEQ2", "EDGER", "LIMMA", "IRD", "WGS", "WES",
    "ONLY", "NOT", "DO", "MUST", "NEVER", "ALWAYS", "ALL", "ANY", "EACH", "EVERY", "TRUE",
    "FALSE", "NONE", "NULL", "NOTE", "TBD", "IMPORTANT", "REQUIRED", "STRICTLY", "EXACT",
    "EXACTLY", "FIRST", "LAST", "BEFORE", "AFTER", "WITH", "WITHOUT", "USE", "USING", "RUN",
    "SEE", "ABOVE", "BELOW", "THIS", "THAT", "THESE", "THOSE", "FROM", "INTO", "ONTO", "PER",
}


def _literature_step_text(question: str) -> str:
    """The literature step's LABEL: the question's biological SUBJECT, or a clean generic form.

    The old builder printed whatever survived the stopword filter — for a real question ("This
    dataset has 3 donors — please run pseudobulk…") that was the residue "has donors", for a
    Chinese question the sentence with a token punched out of its middle, for "help me analyze…"
    the single word "help". Every one reached a production plan card. The rule now: the focused
    query is used ONLY if it still reads as a subject — it names an entity symbol (DDX41, Abca4) or
    a real biological word (retina, knockout, glia), and it is not a leftover of function words.
    Otherwise the label says nothing specific rather than something wrong. (The per-query terms are
    planned from the ACCEPTED FINDINGS at run time, so nothing scientific rides on this label.)"""
    try:
        from ..tools.api import QUERY_STOPWORDS as _QUERY_STOPWORDS, focus_literature_query
        query = focus_literature_query(question or "")
        toks = [w for w in query.split() if w.lower().strip("-") not in _QUERY_STOPWORDS]
    except Exception:  # noqa: BLE001 - planning guard should never break planning
        toks = []
    # Drop CJK-bearing tokens: a Chinese sentence minus one word is not a subject, it is damage.
    toks = [t for t in toks if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", t)]
    # Drop method / format all-caps tokens, question words, and bare function words that the
    # focus filter leaves behind between two content tokens ("What in Rod cells in DDX41").
    _junk = {"what", "which", "how", "why", "when", "where", "who", "in", "of", "on", "at", "to",
             "by", "vs", "and", "or", "the", "a", "an", "is", "are", "does", "do", "did", "there",
             "this", "that", "these", "those", "it", "its", "with", "between", "among", "for",
             "they", "them", "we", "you", "me", "my", "our", "change", "changed", "changes",
             "changing", "differ", "differs", "differed", "different", "differences", "affect",
             "affected", "affects", "types", "type", "cells", "cell", "please", "help", "run"}
    toks = [t for t in toks if t.strip("-").upper() not in _LITERATURE_NOT_ENTITIES
            and t.lower().strip("-?.,;:") not in _junk]
    toks = [t.strip("?.,;:") for t in toks if t.strip("?.,;:")]
    entities = [t for t in toks if _ENTITY_TOKEN_RE.fullmatch(t.strip("-"))]
    # A biological content word: lowercase, >= 5 letters, not a function/instruction residue.
    # ("has", "help", "run", "for", "me" all fail the length gate; "retina", "knockout", "glia"
    # passes on the entity-adjacency below.)
    content = [t for t in toks if t.islower() and len(t.strip("-")) >= 5]
    if not entities and not content:
        return _LITERATURE_GENERIC
    if not entities and len(content) < 2 and len(toks) <= 2:
        # one lonely lowercase word with nothing around it ("donors") is residue, not a subject
        return _LITERATURE_GENERIC
    label = " ".join(toks[:6])
    return f"Literature search for {label}"


def _literature_query(question: str, step: str, rounds: "list[LabRound]") -> str:
    """Build a FOCUSED Europe PMC query for a literature-grounding step from the ACCEPTED findings
    (top marker genes + enriched pathway terms) plus the step's topic — instead of the raw run
    question, which pollutes the query with instruction words (e.g. "finish research"). Falls back
    to a focused (question+step) only when there are no findings yet."""
    from ..tools.api import focus_literature_query
    genes: list[str] = []
    terms: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for lst in (res.get("top_genes_by_group") or {}).values():
                for g in (lst or [])[:3]:
                    if g and g not in genes:
                        genes.append(g)
            for lst in (res.get("top_terms_by_group") or {}).values():
                for t in (lst or [])[:1]:
                    label = _term_label(t)
                    if label and label not in terms:
                        terms.append(label)
            # Variant-annotation results carry their genes as a list of variant dicts (not the scanpy
            # top_genes_by_group map) — pull the pathogenic / high-priority variant genes so a VCF
            # run's query is the REAL genes, not a garbled fallback from the raw question.
            for key in ("pathogenic_variants", "high_priority_variants"):
                for v in (res.get(key) or []):
                    g = v.get("gene") if isinstance(v, dict) else None
                    if g and g not in genes:
                        genes.append(g)
    # Keep the query BROAD: Europe PMC ANDs every term, so a long mash-up returns nothing. One pathway
    # term + a few genes is already at the edge of useful.
    bits: list[str] = []
    if terms:
        bits.append(terms[0])
    if genes:
        bits.append(" ".join(genes[:3]))
    q = " ".join(bits).strip()
    if q:
        return q
    # No findings yet → the QUESTION's biological subject first (the step now carries the PI's own
    # wording — "Summarize findings with literature context" — which names no biology), then the
    # step, then both. Only used pre-findings; once we HAVE genes, question words just pollute it.
    subj = _literature_step_text(question)
    if subj != _LITERATURE_GENERIC:
        return subj[len("Literature search for "):]
    return focus_literature_query(step) or focus_literature_query(f"{question} {step}") or step


# The LLM query planner reads the accepted findings and writes SEVERAL distinct literature
# queries, each targeting a different angle — instead of one generic keyword string.
_LIT_QUERY_SYSTEM = (
    "You are composing search queries for Europe PMC (a biomedical literature database) to find "
    "REAL published papers that ground a single-cell study's findings. Given the research question "
    "and the study's ACCEPTED findings (marker genes and enriched pathways, per cell class), write "
    "2-5 DISTINCT keyword queries, EACH targeting a DIFFERENT angle — e.g. one per major cell class, "
    "one per key enriched pathway or biological process, one for the disease or tissue context. "
    "CRITICAL: Europe PMC treats a query as free text and ANDs every term together, so each extra "
    "keyword SHRINKS the results — a query of 5+ specific terms (e.g. several gene symbols at once) "
    "usually returns ZERO papers. Keep each query BROAD: 2-4 keywords, combining at MOST ONE gene "
    "symbol with a cell type / pathway / tissue term (or no gene at all). NO instruction words, NO "
    "file names, NO boolean operators, NO quotes. "
    'Reply with ONLY a JSON array of query strings, e.g. '
    '["rod photoreceptor phototransduction retina", "RHO retinal degeneration", '
    '"amacrine cell synaptic signaling", "Müller glia retina"].'
)


def _grounding_vocab(rounds: "list[LabRound]") -> str:
    """A CLOSED vocabulary the report writer must stay within — the exact cell-class labels and the
    exact enriched pathway terms the tools actually produced. Injected into the synthesize prompt so
    the model cannot invent pathways (e.g. an EMT/ECM term that never appeared in the enrichment
    table) or expand a class label into a different cell type (e.g. 'MG' -> 'Müller/ganglion').
    Empty string when there is nothing to pin (the system prompt still forbids invention)."""
    classes: list[str] = []
    terms: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for key in ("top_genes_by_group", "top_terms_by_group"):
                for grp in (res.get(key) or {}):
                    if grp and str(grp) not in classes:
                        classes.append(str(grp))
            for lst in (res.get("top_terms_by_group") or {}).values():
                for t in (lst or []):
                    if t and str(t) not in terms:
                        terms.append(str(t))
    if not classes and not terms:
        return ""
    lines = ["Grounding vocabulary — stay STRICTLY within these; introduce nothing outside them:"]
    if classes:
        lines.append(
            "- Cell-class labels present in the data (use verbatim; do NOT rename, merge, split, or "
            "expand a single label into a different or additional cell type): " + ", ".join(classes) + "."
        )
    if terms:
        lines.append(
            "- Enriched pathway/term names ACTUALLY found (name enriched pathways ONLY from this list; "
            "a pathway not listed here was NOT enriched — do not present it as a finding): "
            + "; ".join(terms[:40]) + "."
        )
    return "\n".join(lines)


def _methods_performed(rounds: "list[LabRound]") -> str:
    """A CLOSED allowlist of the analyses/tools ACTUALLY executed in accepted steps, so the report's
    Methods cannot describe a technique that never ran (scGPT, multi-omics, trajectory, …) — not even
    as 'planned but failed'. Complements _grounding_vocab (which pins labels/terms). '' when empty."""
    tools: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            tl = s.get("tool")
            if tl and str(tl) not in tools and str(tl) != "finish":
                tools.append(str(tl))
    if not tools:
        return ""
    return (
        "Tools/analyses ACTUALLY executed (the Methods/Results may describe ONLY analyses among these; "
        "do NOT mention any other tool, model, or technique, and never describe one as planned/attempted/"
        "failed): " + ", ".join(tools) + "."
    )


# The AUTHORITATIVE scalar figures the tools reported — whitelisted keys (+ common variants), pulled
# recursively so nested blocks like ``variant_filters`` are covered.
_FACT_KEYS = frozenset({
    "assembly", "execution_mode", "normalized",
    "n_input_variants", "n_input", "n_kept", "n_dropped_common_af", "n_dropped_off_panel",
    "max_pop_af", "gene_panel_size", "total_variants", "n_pathogenic", "n_pathogenic_clinvar",
    "n_high_priority_rare_deleterious",
    "n_pass", "n_nonpass", "n_filtered", "n_records", "n_snps", "n_indels", "n_multiallelic",
    "n_samples", "ti_tv", "ti_tv_ratio", "het_hom", "het_hom_ratio", "call_rate",
    "n_cells", "n_genes", "n_clusters", "n_de_genes", "resolution",
})
_FACT_DIST_KEYS = ("by_impact", "by_consequence", "by_clinical_significance",
                   "impact_distribution", "consequence_distribution", "clinical_significance_distribution")


def _collect_facts(rounds: "list[LabRound]") -> "tuple[dict[str, Any], dict[str, dict]]":
    """Recursively pull the whitelisted authoritative figures (scalars) + category distributions from the
    ACCEPTED step results (nested blocks like ``variant_filters`` included). Shared by the synthesize
    grounding block and the post-report fact-check."""
    facts: dict[str, Any] = {}
    dists: dict[str, dict] = {}

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(obj, dict):
            return
        for k, v in obj.items():
            if k in _FACT_KEYS and isinstance(v, (int, float, str, bool)):
                facts[str(k)] = v                     # a later accepted step wins on a key clash
            elif k in _FACT_DIST_KEYS and isinstance(v, dict) and v:
                dists[str(k)] = v
            elif isinstance(v, dict):
                _walk(v, depth + 1)

    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            _walk(s.get("result"))
    return facts, dists


def resolve_evidence(paths: "list[str]", workspace: "Path | None") -> "tuple[list[str], list[str]]":
    """Split claimed artifact paths into (present on disk, missing). Deterministic — no model.

    ``evidence_pointers`` is called in four places and NOTHING ever checked that the files it names
    exist. So a tool could report a figure it never wrote and the Critic would ground its verdict on
    it: ``run_de`` returned ``figures/rank_genes_groups_leiden_de.png`` hardcoded, which resolves to
    nothing whenever the DE was grouped by anything other than ``leiden`` — a dangling evidence
    pointer that survived in production until it was read by hand.

    This is the cheapest member of the class of defects that matter here: the tool SUCCEEDS, the
    system contract holds, and only an independent check against the filesystem reveals it. Reports
    rather than judges — see ``_critic`` for why the verdict is not flipped on this alone."""
    if workspace is None:
        return list(paths), []
    art = Path(workspace) / "artifacts"
    present, missing = [], []
    for p in paths:
        rel = str(p).strip()
        if not rel:
            continue
        if Path(rel).is_absolute():
            # An absolute path is checked HERE, on the gateway host — but the HPC3 shell tools
            # (list_dir / read_text / stat_path / find_files) return the dfs3b paths they looked
            # at, and analysis jobs run there too. Those paths never exist on the eyeserver, so
            # every step that used the shell was told ALL its evidence was missing (a production
            # ORA step scored 0.0 for it while its tables sat right there in artifacts/). Map a
            # remote artifact to its local mirror by its artifacts/-relative suffix; treat any
            # other remote path as unverifiable from here — neither present nor missing.
            if "/artifacts/" in rel:
                mirror = art / rel.split("/artifacts/", 1)[1]
                (present if mirror.exists() else missing).append(rel)
                continue
            if Path(rel).exists():
                present.append(rel)
            elif rel.startswith(("/dfs3b/", "/pub/", "/data/homezvol", "/tmp/", "/scratch/")):
                continue                              # lives on the cluster; not checkable here
            else:
                missing.append(rel)
            continue
        (present if (art / rel).exists() else missing).append(rel)
    return present, missing


def _uncovered_groups(rounds: "list[LabRound]") -> "dict[str, str]":
    """Groups a step REFUSED to analyse, and why — e.g. cell types with too few cells in one arm.

    This is a RESULT, not a degradation: "we could not test this cell type" is part of what the
    study found, and a report that omits it implies a coverage it does not have. The DEG protocol
    requires stating it, but nothing carried it to the writer: ``_collect_facts`` only recurses into
    dicts, and both producers emit a LIST (``run_de``) or a flat dict (``run_pseudobulk_de``) of
    skips, so on the lab's real Ddx41 data 5 of 12 cell types went untested and the grounding block
    never mentioned it.

    Handles both shapes. Returns {group: reason}, empty when everything was covered."""
    out: dict[str, str] = {}
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result")
            if not isinstance(res, dict):
                continue
            skipped = res.get("skipped_groups")
            if isinstance(skipped, dict):                       # run_pseudobulk_de: {label: reason}
                for label, reason in skipped.items():
                    out[str(label)] = str(reason)[:200]
            elif isinstance(skipped, list):                     # run_de: [{group, n_*, reason}]
                # Name the ARMS, not the roles. "condition=7, reference=27" leaves the writer to work
                # out which arm is which; run 8847d521ba32's report has three of its five unequal
                # skipped pairs backwards.
                arms = contrast_arms(res)
                for item in skipped:
                    if not isinstance(item, dict) or not item.get("group"):
                        continue
                    if arms and "n_condition" in item and "n_reference" in item:
                        counts = (f"cells after QC: {arms[0]}={item['n_condition']}, "
                                  f"{arms[1]}={item['n_reference']}")
                    else:
                        counts = ", ".join(
                            f"{k.replace('n_', '')}={item[k]}" for k in ("n_condition", "n_reference")
                            if k in item)
                    reason = str(item.get("reason", "not analysed"))[:160]
                    out[str(item["group"])] = f"{reason}{f' ({counts})' if counts else ''}"
    return out


def _tested_counts(rounds: "list[LabRound]") -> "dict[str, dict[str, int]]":
    """Cells per group and arm AFTER QC, as the tools counted them: ``cells_by_group_and_arm`` (run_de,
    run_composition) and a contrast's ``skipped_groups``. These are the numbers a cell floor was
    applied to. The dataset profile counts the uploaded file before QC; in run f3731e0b7136 QC removed
    30% of nuclei, so Endothelial went from 7 / 27 in the file to 5 / 22 in the analysis."""
    out: dict[str, dict[str, int]] = {}
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result")
            if not isinstance(res, dict):
                continue
            table = res.get("cells_by_group_and_arm")
            if isinstance(table, dict):
                for group, per in table.items():
                    if isinstance(per, dict) and per:
                        out[str(group)] = {str(a): int(n) for a, n in per.items()
                                           if isinstance(n, (int, float)) and not isinstance(n, bool)}
            arms = contrast_arms(res)
            skipped = res.get("skipped_groups")
            if arms and isinstance(skipped, list):
                for item in skipped:
                    if (isinstance(item, dict) and item.get("group")
                            and isinstance(item.get("n_condition"), int)
                            and isinstance(item.get("n_reference"), int)):
                        out[str(item["group"])] = {arms[0]: item["n_condition"],
                                                   arms[1]: item["n_reference"]}
    return out


def pre_qc_counts(dataset_result: "dict[str, Any] | None") -> "dict[str, dict[str, int]]":
    """Cells per label and arm IN THE UPLOADED FILE (the dataset profile), before any QC."""
    dba = (dataset_result or {}).get("design_by_arm") if isinstance(dataset_result, dict) else None
    table = (dba or {}).get("cells_by_label_and_arm") if isinstance(dba, dict) else None
    return {str(k): dict(v) for k, v in (table or {}).items() if isinstance(v, dict)}


# A statement about coverage — which groups were tested, and why some were not. The only kind of
# sentence correct_coverage_counts may touch: elsewhere a report may rightly quote the uploaded file.
_COVERAGE_RE = re.compile(
    r"floor|below|too few|fewer than|not covered|not analy[sz]ed|not tested|untested|excluded|"
    r"skipped|insufficient|underpowered|could not be (?:tested|analy[sz]ed)|no primary", re.I)
_PRE_QC_RE = re.compile(r"before QC|pre-?QC|prior to QC|uploaded|raw (?:file|data|object|counts?)", re.I)
_COUNT_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3})*(?![\w.,]*\d)")


def _swap_to_tested(window: str, pre: "dict[str, int]", tested: "dict[str, int]") -> "tuple[str, bool]":
    """Replace the uploaded-file counts in ``window`` with the counts the analysis tested. Only a
    number EQUAL to the group's pre-QC count for its arm is touched, so a correct number never is."""
    arms = [a for a in tested if a in pre and pre[a] != tested[a]]
    if not arms:
        return window, False
    edits: dict[tuple[int, int], str] = {}
    for a in [a for a in tested if a in pre]:
        esc = re.escape(a)
        for pat in (rf"(?<![\w.,])(\d{{1,3}}(?:,\d{{3}})*)\s*{esc}\b",                 # "7 DDX41"
                    rf"\b{esc}\b(?:\s+arm)?\s*(?:=|:|\()?\s*(\d{{1,3}}(?:,\d{{3}})*)(?![\w.,]*\d)"):  # "WT=27", "WT arm (24"
            for m in re.finditer(pat, window):
                n = int(m.group(1).replace(",", ""))
                if n == pre[a] and pre[a] != tested[a]:
                    edits[m.span(1)] = str(tested[a])
    if not edits and len(tested) == 2 and all(a in pre for a in tested):
        # A bare pair, "HC (5 / 11)": the two arms in whichever order the pre-QC values sit.
        nums = list(_COUNT_RE.finditer(window))[:2]
        if len(nums) == 2:
            got = [int(m.group(0).replace(",", "")) for m in nums]
            a, b = list(tested)
            for first, second in ((a, b), (b, a)):
                if got == [pre[first], pre[second]] and [pre[first], pre[second]] != [tested[first], tested[second]]:
                    edits = {nums[0].span(): str(tested[first]), nums[1].span(): str(tested[second])}
                    break
    if not edits:
        return window, False
    out = window
    for (s, e), new in sorted(edits.items(), reverse=True):
        out = out[:s] + new + out[e:]
    return out, out != window


def correct_coverage_counts(md: str, uncovered: "dict[str, str] | Any",
                            tested: "dict[str, dict[str, int]]",
                            pre: "dict[str, dict[str, int]]") -> "tuple[str, list[str]]":
    """Put the tested (post-QC) counts back into the report's coverage statements.

    Run f3731e0b7136's manuscript said "Endothelial (7 DDX41 / 27 WT) … had both arms below the
    30-cell floor"; the analysis had tested 5 / 22. 7 / 27 were the uploaded file's counts, which the
    writers had been handed as authoritative. For each group the analysis refused, a count next to its
    name in a coverage sentence that equals its pre-QC count (and not its tested one) is replaced. A
    sentence that says it is describing the file before QC is left alone. Returns (md, issues)."""
    issues: list[str] = []
    groups = [g for g in (uncovered or {}) if g in tested and g in pre
              and any(pre[g].get(a) != n for a, n in tested[g].items() if a in pre[g])]
    if not groups or not md:
        return md, issues
    names = sorted({*map(str, pre), *map(str, tested)}, key=len, reverse=True)
    any_group = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in names))
    for g in groups:
        pos, fixed = 0, False
        pattern = re.compile(rf"\b{re.escape(g)}\b")
        while True:
            m = pattern.search(md, pos)
            if not m:
                break
            s_start = max(md.rfind(". ", 0, m.start()), md.rfind("\n", 0, m.start())) + 1
            s_end_candidates = [i for i in (md.find(". ", m.end()), md.find("\n", m.end())) if i != -1]
            s_end = min(s_end_candidates) if s_end_candidates else len(md)
            sentence = md[s_start:s_end]
            end = min(s_end, m.end() + 100)
            nxt = any_group.search(md, m.end(), end)
            if nxt:
                end = nxt.start()
            if _COVERAGE_RE.search(sentence) and not _PRE_QC_RE.search(sentence):
                new, changed = _swap_to_tested(md[m.end():end], pre[g], tested[g])
                if changed:
                    md = md[:m.end()] + new + md[end:]
                    end = m.end() + len(new)
                    fixed = True
            pos = end
        if fixed:
            issues.append(
                f"coverage counts: the report gave '{g}' the uploaded file's counts "
                f"({', '.join(f'{a} {n}' for a, n in pre[g].items())}); the analysis tested "
                f"{', '.join(f'{a} {n}' for a, n in tested[g].items())} after QC — corrected.")
    return md, issues


def _grounding_facts(rounds: "list[LabRound]") -> str:
    """A CLOSED set of the AUTHORITATIVE figures the tools reported — genome assembly, execution mode,
    PASS/non-PASS + variant/cell counts, thresholds, ratios, and category distributions — injected into
    the synthesize prompt so the report cannot invent a number, echo the plan's ASSUMED assembly over the
    one actually used, or write "0 non-PASS" when the result says otherwise. "" when nothing quantitative
    was produced. Complements _grounding_vocab (labels/terms) + _methods_performed (tools)."""
    facts, dists = _collect_facts(rounds)
    uncovered = _uncovered_groups(rounds)
    if not facts and not dists and not uncovered:
        return ""
    lines = [
        "Authoritative figures — every number, count, ratio, threshold, genome assembly, and execution "
        "mode in the report MUST match these EXACTLY. Do NOT state a figure that is not here, do NOT round "
        "or infer one, and do NOT echo the plan's assumed value (e.g. its genome assembly) over these. "
        "Never write 'all PASS' or '0 non-PASS' unless a non-PASS count of 0 appears here:",
    ]
    lines += [f"- {k} = {v}" for k, v in facts.items()]
    for k, d in dists.items():
        pairs = ", ".join(f"{kk}: {vv}" for kk, vv in list(d.items())[:20])
        lines.append(f"- {k}: {pairs}")
    if uncovered:
        # Coverage is a RESULT. Without this the writer sees results for 7 cell types, has no way
        # to know 5 more exist and were refused, and writes a manuscript that reads as though the
        # analysis covered the dataset.
        lines.append(
            "NOT ANALYSED — these groups were REFUSED by the analysis and have NO results. The "
            "report MUST state that they were not covered and why, MUST NOT present any finding "
            "for them, and MUST NOT describe the analysis as covering all groups. The cell counts "
            "in brackets are AFTER QC, the numbers the cell floor was applied to; quote THESE. The "
            "dataset profile and the plan count the uploaded file before QC, so their per-group "
            "numbers are larger and are not what the analysis tested:")
        lines += [f"- {g}: {why}" for g, why in list(uncovered.items())[:20]]
    return "\n".join(lines)


_ASSEMBLY_CANON = {"grch38": "GRCh38", "hg38": "GRCh38", "grch37": "GRCh37", "hg19": "GRCh37"}


def verify_report_facts(report_md: str, facts: "dict[str, Any]",
                        uncovered: "dict[str, str] | None" = None, *,
                        tested_counts: "dict[str, dict[str, int]] | None" = None,
                        pre_counts: "dict[str, dict[str, int]] | None" = None) -> "tuple[str, list[str]]":
    """The GUARANTEE layer: a deterministic post-generation fact-check of the manuscript against the
    authoritative figures, so a fabrication that slipped past the grounding prompt is caught regardless of
    whether the LLM obeyed it. Corrects two UNAMBIGUOUS cases in place — a wrong genome assembly (the
    report named a build other than the one actually used), and a '0/no/zero non-PASS' claim when the QC
    reported non-PASS records — and returns ``(corrected_md, issues)``. It only swaps those tokens/numbers,
    never narrative prose; ``issues`` are for the technical-report Diagnostics."""
    import re
    md = report_md or ""
    issues: list[str] = []

    # 1. Genome assembly — the report must name the build ACTUALLY used, not the plan's assumption.
    auth = _ASSEMBLY_CANON.get(str(facts.get("assembly") or "").strip().lower())
    if auth:
        for token in ("GRCh38", "GRCh37", "hg38", "hg19"):
            if _ASSEMBLY_CANON.get(token.lower()) == auth:
                continue                                  # this token already IS the right build
            pat = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
            hits = pat.findall(md)
            if hits:
                md = pat.sub(auth, md)
                issues.append(f"assembly: report stated '{token}' but the annotation used {auth} — "
                              f"corrected {len(hits)} mention(s).")

    # 2. PASS / non-PASS — a '0/no/zero non-PASS' claim when non-PASS records exist is a fabrication.
    nonpass = facts.get("n_nonpass")
    if nonpass is None:
        nonpass = facts.get("n_filtered")
    if isinstance(nonpass, (int, float)) and not isinstance(nonpass, bool) and nonpass > 0:
        pat = re.compile(r"\b(?:0|zero|no)\s+non-?PASS\b", re.IGNORECASE)
        if pat.search(md):
            md = pat.sub(f"{int(nonpass):,} non-PASS", md)
            issues.append(f"pass-split: report claimed 0/no non-PASS but {int(nonpass):,} records are "
                          f"non-PASS — corrected.")

    # 3. Coverage — groups the analysis REFUSED must not vanish from the write-up. Audited, not
    # rewritten: the grounding block already instructs the writer to state them, and inventing the
    # sentence here would put prose in the manuscript that no model wrote. An unmentioned group is
    # therefore reported as an issue so the Diagnostics show whether the instruction was obeyed.
    for group in (uncovered or {}):
        if not re.search(rf"\b{re.escape(str(group))}\b", md):
            issues.append(
                f"coverage: '{group}' was NOT analysed (too few cells) but the report never mentions "
                "it — the manuscript reads as though every group was covered.")
    # 4. Coverage counts — a refused group described with the uploaded file's counts, not the tested ones.
    md, count_issues = correct_coverage_counts(md, uncovered or {}, tested_counts or {}, pre_counts or {})
    issues += count_issues
    return md, issues


def _term_label(t: Any) -> str:
    """A pathway term as text. ``run_enrichment`` lists plain strings; ``run_gsea_prerank`` lists
    ``{"term", "gene_set", "nes", "fdr", "direction"}`` dicts. Every consumer that joined these
    as strings raised on the dict shape — and one of them (the literature digest) sat outside its
    caller's try, so the whole literature step degraded to searching for the pasted step brief."""
    if isinstance(t, dict):
        return str(t.get("term") or t.get("name") or t.get("Term") or "").strip()
    return str(t or "").strip()


def _literature_findings_digest(rounds: "list[LabRound]") -> str:
    """A compact per-cell-class digest of accepted findings (top markers + enriched pathways) that
    the LLM query planner turns into several angle-specific literature queries. Empty when there are
    no structured findings yet (planner then falls back to the question/step)."""
    genes_by: dict[str, list[str]] = {}
    terms_by: dict[str, list[str]] = {}
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for grp, lst in (res.get("top_genes_by_group") or {}).items():
                bucket = genes_by.setdefault(str(grp), [])
                for g in (lst or [])[:5]:
                    if g and g not in bucket:
                        bucket.append(g)
            for grp, lst in (res.get("top_terms_by_group") or {}).items():
                bucket = terms_by.setdefault(str(grp), [])
                for t in (lst or [])[:2]:
                    label = _term_label(t)
                    if label and label not in bucket:
                        bucket.append(label)
    lines: list[str] = []
    for grp in dict.fromkeys([*genes_by, *terms_by]):
        parts: list[str] = []
        if genes_by.get(grp):
            parts.append("markers " + ", ".join(genes_by[grp][:5]))
        if terms_by.get(grp):
            parts.append("pathways " + ", ".join(terms_by[grp][:2]))
        if parts:
            lines.append(f"- {grp}: " + "; ".join(parts))
    return "\n".join(lines)


def _parse_query_list(raw: str, max_n: int) -> list[str]:
    """Parse the planner's JSON array of query strings; sanitize each through ``focus_literature_query``
    (strips instruction/file words the model may still slip in) and dedupe. Returns [] on any parse
    failure so the caller falls back to the deterministic single query."""
    from ..tools.api import focus_literature_query
    s = (raw or "").strip()
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
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        q = focus_literature_query(item).strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append(q)
        if len(out) >= max(1, max_n):
            break
    return out


# If the user requested literature support:
# 1. If the literature_search tool is unavailable, leave the agenda unchanged.
# 2. If the agenda already contains literature-related steps, collapse them into one canonical step.
# 3. If the agenda has room, append the literature step.
# 4. If the agenda is full, replace a lower-priority step with the literature step.
def _ensure_literature_agenda(
    agenda: list[str],
    *,
    question: str,
    guidance: str | None,
    feedback: str,
    max_steps: int,
    has_literature_tool: bool,
    author: "Callable[[str], str] | None" = None,
) -> list[str]:
    """Deterministically preserve a literature step when the user requested one.

    The guard decides THAT the step exists. WHAT it says comes from ``author`` (the PI writing one
    step from the full question + dataset profile) when one is wired; the string template
    ``_literature_step_text`` is only the offline fallback. A PI-written literature step that is
    already in the plan is kept as written — it is not replaced by anything."""
    if not has_literature_tool or not _requests_literature(question, guidance, feedback):
        return agenda
    existing_lit = [existing for existing in agenda if _is_literature_step(existing)]
    if existing_lit:
        # Keep the PI's own wording; only collapse duplicates. Of several, keep the most
        # informative one (a bare tool name like "literature_search" loses to a real sentence);
        # a step that is ONLY a tool name is not a step, so fall back to the label for it.
        keep = max(existing_lit, key=len)
        if len(keep.strip()) < 30:
            keep = author(_literature_step_text(question)) if author is not None \
                else _literature_step_text(question)
        deduped: list[str] = []
        inserted = False
        for existing in agenda:
            if _is_literature_step(existing):
                if not inserted:
                    deduped.append(keep)
                    inserted = True
                continue
            deduped.append(existing)
        return deduped[:max_steps]
    fallback = _literature_step_text(question)
    step = author(fallback) if author is not None else fallback
    if len(agenda) < max_steps:
        return [*agenda, step]
    if not agenda:
        return [step]
    replace_idx = len(agenda) - 1
    for idx in range(len(agenda) - 1, -1, -1):
        if _LOW_PRIORITY_FOR_LITERATURE_RE.search(agenda[idx]):
            replace_idx = idx
            break
    out = list(agenda)
    out[replace_idx] = step
    return out

# Summarize the literature_search result into a DOI/PMID-backed answer that the Critic can accept
# and the final References section can reuse.
def _literature_answer(query: "str | list[str]", result: dict[str, Any]) -> str:
    queries = [query] if isinstance(query, str) else [q for q in (query or []) if q]
    disp = " | ".join(queries) or "(none)"
    noun = "query" if len(queries) <= 1 else "queries"
    citations = [
        c for c in (result.get("results") or [])
        if isinstance(c, dict) and (c.get("doi") or c.get("pmid"))
    ]
    if not citations:
        return (
            f"No DOI/PMID-backed literature citations were returned for {noun} `{disp}`. "
            "State this limitation in the report rather than inventing references."
        )
    lines = [
        f"Literature search {noun}: `{disp}`",
        "",
        "DOI/PMID-backed citations found (merged, de-duplicated across queries):",
    ]
    for i, c in enumerate(citations[:12], 1):
        lines.append(f"{i}. {c.get('citation') or c.get('title')}")
    return "\n".join(lines)


# --- Axis A: Virtual-Lab team mode (dynamic team + multi-agent meetings) ------
# A "team meeting" runs several expert agents that EACH keep an INDEPENDENT context (their
# own persona, never the other experts' raw turns) so the perspectives stay diverse, plus a
# first-class Scientific Critic and a PI synthesis — the Virtual-Lab structure. Execution
# still runs the REAL tools via the per-step Scientist loop; the meetings wrap it to DESIGN
# the approach and INTERPRET the results from multiple angles. Independent context here is
# in-run only; persistent per-agent memory (the Lab Archive) is Axis C, deferred.

_MODE_ROUTE_SYSTEM = (
    "You are the PI deciding HOW to run a request. Reply with ONLY 'team' or 'single'. Choose "
    "'team' (a multi-expert Virtual Lab) for open-ended, interpretation-heavy, or multi-"
    "disciplinary research questions; choose 'single' for a routine single-pipeline task (e.g. "
    "run a specific tool, a standard annotation) where one scientist suffices."
)

_TEAM_FORM_SYSTEM = (
    "You are the Principal Investigator assembling a small expert team for a research question. "
    "Reply with ONLY a JSON array of 2-4 experts, each "
    '{"title": "...", "expertise": "...", "goal": "..."}. Choose COMPLEMENTARY, specific '
    "expertises that fit THIS question (e.g. a single-cell biologist, a statistician, a disease-"
    "domain expert). Do NOT include the PI or the Critic themselves."
)

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


_MEETING_CRITIC_SYSTEM = (
    "You are the team's Scientific Critic in a meeting. Given the topic and the experts' "
    "contributions, judge how sound and complete the team's CURRENT thinking is, and point out "
    "the most important flaws, missing considerations, unsupported claims, and risks — specific "
    "and constructive. Reply with ONLY a JSON object: "
    '{"score": <0.0-1.0 = how ready the team\'s thinking is to act on>, '
    '"critique": "<the specific issues and what to address in the next round>"}.'
)

# --- PI↔Critic step meetings (docs/pi_critic_meeting_protocol.md) -------------
# Two bounded PI↔Critic exchanges wrapped around each step. The Critic challenges; the PI (who owns
# the plan) adjudicates. This is the model-judgment layer; deterministic guards remain the floor.
_PREFLIGHT_GATE_SYSTEM = (
    "You are the Scientific Critic in a PRE-FLIGHT review with the PI, held BEFORE a step runs. You are "
    "given the research question, the WHOLE plan (each step tagged done / current / remaining), the "
    "findings ALREADY accepted (with their artifacts), the dataset profile, and the ONE step about to "
    "execute. Decide whether running it AS WRITTEN is scientifically justified right now, challenging "
    "it on four axes: (1) NECESSITY — does its output feed the research question or a remaining step, "
    "or is it orphan work nothing consumes? (2) REDUNDANCY — is it already covered by an accepted step "
    "or an existing checkpoint? (3) PRECONDITION — are its methodological preconditions met? e.g. "
    "pathway/GO enrichment or discovery differential expression needs a real experimental CONTRAST "
    "(2+ conditions), not a single annotated sample; a per-cluster claim needs the clusters reconciled "
    "against any provided labels. (4) ALTITUDE — is the granularity right? Reply with ONLY a JSON "
    'object: {"action": "proceed" | "amend" | "skip", "reason": "<one sentence>", '
    '"amendment": "<if amend: the concrete change to fold into the step\'s brief; else empty>"}. '
    'Use "skip" ONLY for a genuinely circular, redundant, or unfounded step — most steps proceed. '
    "The PI makes the final call and may overrule you."
)
_PREFLIGHT_PI_SYSTEM = (
    "You are the Principal Investigator adjudicating a PRE-FLIGHT objection the Critic raised about the "
    "next step, before it runs. You are given the step, the plan, the accepted findings, the dataset "
    "profile, and the Critic's proposed action + reason. You OWN the plan and have the final call: keep "
    "the step, fold in an amendment, or drop it. Only drop a step you agree is circular, redundant, or "
    'unfounded — when in doubt, keep it. Reply with ONLY a JSON object: {"action": "proceed" | "amend" '
    '| "skip", "reason": "<one sentence>", "amendment": "<if amend: the concrete change to the step\'s '
    'brief; else empty>"}.'
)
_POSTSTEP_PI_SYSTEM = (
    "You are the Principal Investigator reviewing a step that JUST completed, to keep the remaining plan "
    "honest. You are given the step, the Critic's verdict, what the step produced (answer + artifacts), "
    "the findings accepted so far, and the REMAINING planned steps (verbatim). State whether this step "
    "CHANGED the picture, and whether any remaining step is now UNNECESSARY — already answered, or only "
    "justified by a branch that did not pan out. Reply with ONLY a JSON object: "
    '{"contribution": "new" | "confirmed" | "nothing", '
    '"prune": ["<verbatim remaining step to drop>", ...], "reason": "<one sentence>"}. '
    "Prune CONSERVATIVELY — only steps clearly made moot; usually the list is empty."
)
# The EXPLORATION turn — the only place in the system where the plan can GROW. Everything else
# (pre-flight gate, post-step review, plan review) can at best leave the plan unchanged, so without
# this a result that contradicts the plan's premise has nowhere to go: the run keeps executing the
# agenda it drafted before it had seen any data. The prompt is written to make "no new path" the
# cheap default answer, because a false positive here spends real GPU hours on noise.
_EXPLORE_SYSTEM = (
    "You are the Principal Investigator, reading a step that JUST completed, to decide whether its "
    "result opened a research path the CURRENT PLAN DOES NOT COVER. This is how the lab discovers "
    "something it did not set out to find — and also how it wastes a day of compute if you are "
    "undisciplined. THE DEFAULT ANSWER IS 'NOTHING NEW': empty lists are the normal reply, and you "
    "must return them whenever the result merely CONFIRMS what the plan expected, is a routine "
    "quality/summary output, or is only 'interesting' without contradicting anything.\n"
    "Propose a new path ONLY when the result is genuinely SURPRISING with respect to the plan: it "
    "contradicts the premise a planned step rests on, a signal appears in a population/condition "
    "nobody planned to look at, or two accepted findings are mutually inconsistent.\n"
    "A HYPOTHESIS MUST BE FALSIFIABLE. State it as a claim about the biology (not about the data "
    "processing), give the observation you would EXPECT IF IT IS TRUE, and give a test whose outcome "
    "would DISTINGUISH it from the obvious competing explanation — including the boring one "
    "(a technical artefact, ambient RNA, doublets, batch, coverage). 'Investigate X further', "
    "'characterise Y in more detail', and 'validate the results' are NOT hypotheses; reject them.\n"
    "Each proposed step must: be achievable with the LISTED TOOLS; be ONE tool's worth of work; NOT "
    "duplicate or restate any step already in the plan (the verbatim plan is given — check it); and "
    "actually test one of your hypotheses. Never propose a step that writes, renders, packages, or "
    "exports a report/figure bundle — those are produced automatically at the end of the run. "
    "Write each step in plain scientific English for a researcher to read — the action, what it is "
    "performed on, and what it would show — NAMING the tool it runs in backticks, matching the "
    "style of the existing plan. No bare keyword arguments, no code, no file paths.\n"
    "EVERY hypothesis must name the RIVAL explanation it is competing with and the DISCRIMINATOR "
    "that separates them. A claim with no alternative cannot be tested, only narrated: the first "
    "run with this loop enabled produced one hypothesis ('the shifts are a depth artefact'), had "
    "nothing to compare it against, and marked it supported on a prediction its own design "
    "guaranteed — while the real signal sat in its tables. So: `rival` is the OTHER explanation "
    "that would produce the same observation (if your hypothesis is the technical one, the rival "
    "is the biological one, and vice versa), and `discriminator` says which outcome favours yours "
    "and which favours the rival. A discriminator whose outcome is fixed by the study design — a "
    "between-arm p-value when there is one sample per arm, say — is not a discriminator, because "
    "both explanations predict the same result; discriminate on effect DIRECTION, cell-type "
    "SPECIFICITY, or an orthogonal measurement instead.\n"
    "You are ALSO given the hypotheses still OPEN from earlier steps, each WITH its rival. If THIS "
    "step's result bears on one, adjudicate it by saying which side it favoured: 'hypothesis', "
    "'rival', or 'neither', with one sentence of evidence taken from THIS result. Do not adjudicate "
    "a hypothesis this result says nothing about, and do not re-propose one already in the list.\n"
    "Reply with ONLY a JSON object: "
    '{"surprise": "<one sentence: what was unexpected, or exactly \'nothing\'>", '
    '"hypotheses": [{"statement": "...", "prediction": "...", "test": "...", '
    '"rival": "<the competing explanation>", '
    '"discriminator": "<outcome X favours the hypothesis; outcome Y favours the rival>"}], '
    '"new_steps": [{"step": "<plain-English step>", "hypothesis": "<verbatim statement it tests>"}], '
    '"resolve": [{"hypothesis": "<verbatim OPEN statement or its id>", '
    '"favoured": "hypothesis|rival|neither", "evidence": "<one sentence from this result>"}]}. '
    "At most 2 hypotheses and 2 new steps per step."
)
# Planning a FOLLOW-UP cycle. Different judgement from the first plan: cycle 1 is planned blind (the
# question + the dataset profile), this one is planned against what the data actually said — so the
# only steps worth writing are ones the first cycle's RESULTS made worth writing.
_NEXT_CYCLE_SYSTEM = (
    "You are the Principal Investigator deciding whether to run ANOTHER cycle of analysis, and what "
    "it should contain. The previous cycle(s) are finished; you are given the research question, "
    "every step already performed WITH its result, the hypothesis ledger (each hypothesis with its "
    "status: open, supported, refuted, or inconclusive), the dataset profile, and the tools.\n"
    "STOPPING IS A LEGITIMATE AND COMMON ANSWER. Stop when the research question has been answered "
    "as far as this dataset allows, when the only open hypotheses need data or tools you do not "
    "have, or when a further cycle would just re-describe what is already known. Do NOT invent a "
    "cycle to look busy — a study that stops when it has run out of answerable questions is a good "
    "study.\n"
    "Continue ONLY when there is a CONCRETE question the previous cycles RAISED but did not settle "
    "— typically an OPEN hypothesis with a discriminating test that the available tools can "
    "actually run, or a result that changed what the right analysis is. The new plan must be work "
    "the FIRST cycle could not have known to do: do NOT repeat, re-run, or lightly reword a step "
    "already performed (they are listed verbatim — check), and do NOT re-run an upstream stage "
    "whose checkpoint already exists.\n"
    "Every step must be achievable with the listed tools, be ONE tool's worth of work, and be "
    "written in plain scientific English for a researcher — the action, what it is performed on, "
    "and what it reveals — NAMING the tool it runs in backticks, with no bare keyword arguments, "
    "no code, and no file paths. Never plan a step "
    "that writes, renders, packages, or exports a report: the manuscript is assembled automatically "
    "once all cycles finish.\n"
    "Reply with ONLY a JSON object: "
    '{"continue": true|false, "reason": "<one sentence: what this cycle would settle, or why the '
    'study should stop>", "agenda": ["<step>", ...]}. '
    'With "continue": false, return an empty agenda.'
)
_PLAN_REVIEW_CRITIC_SYSTEM = (
    "You are the Scientific Critic reviewing a DRAFT analysis plan with the PI, BEFORE any step runs. "
    "You are given the research question, the dataset profile, and the ordered draft agenda. "
    "USE YOUR OWN KNOWLEDGE OF SINGLE-CELL AND GENOMICS METHOD, not only the protocol you were "
    "handed: a plan can follow a protocol's wording and still be scientifically incoherent, and "
    "catching that is exactly your job. Judge the plan AS A WHOLE, not step by step:\n"
    "(1) NECESSARY — a step is an orphan only when NOTHING consumes its output AND the dataset does "
    "not warrant it. A step that only re-reads, parses, tabulates or restates what an earlier step "
    "already produced is not an analysis step; it produces no new knowledge and must be dropped. "
    "The same goes for re-RENDERING: volcano plots, per-group DEG tables and the final report are "
    "produced automatically by the tools that computed them, so a step that exists to draw or "
    "compile those again is duplicate work — but do NOT flag a step merely because it names no "
    "existing tool: writing an ad-hoc analysis (via `run_code`) that the toolset lacks is allowed "
    "when it adds real scientific value. Two misses to learn from: a plan spent a whole `run_code` "
    "step re-reading obs columns the DATASET PROFILE already states (the condition column, the "
    "cell-type column, the per-arm cell counts, the number of replicate levels) — the profile is "
    "an earlier step's output for this purpose; and a 'figures and tables' step re-drew the "
    "volcano plots and enrichment heatmaps that run_de and run_enrichment already write to "
    "figures/, which is the re-RENDERING case above wearing a different name. "
    "But a NARROWLY WORDED QUESTION IS NOT A REASON TO DROP AN ANALYSIS. A researcher who writes "
    "'run tool X and report what it returns' is naming a starting point, not commissioning a "
    "one-line study, and the standard is what the DATA warrants: quality control, the "
    "composition comparison, the per-cell-type stratification, and pathway interpretation stay in "
    "the plan when the dataset's design supports them, even though the question mentions none of "
    "them. Removing them is the failure this review exists to prevent, not an example of it — "
    "measured on this exact prompt, an earlier wording cut a sound 5-step plan to 2 steps by "
    "calling QC and composition 'orphans relative to the research question'. Drop a step only when "
    "you can say what makes it redundant or impossible ON THIS DATA (it recomputes a column the "
    "profile already carries; it needs replicates the profile shows do not exist).\n"
    "(2) COHERENT — do any two steps CONTRADICT each other? If a later step's justification says the "
    "earlier step's method is invalid (e.g. step 5 aggregates to samples 'instead of treating cells "
    "as independent replicates' while step 3 does exactly that and its output is still reported), the "
    "plan cannot be run as written: say which of the two is the real analysis and drop or relabel the "
    "other. Likewise if the plan re-clusters de-novo AND the data already carries cell-type labels, "
    "reconcile them or drop the clustering — anonymous cluster IDs that no later step maps back to "
    "the labels are dead weight.\n"
    "(3) THE DATA SUPPORTS IT — check every column a step depends on against the PROFILE, and say so "
    "when it is missing. A step that aggregates 'per donor' when no donor/sample column exists in the "
    "profile cannot execute; pathway/GO enrichment or discovery-style DE needs a real experimental "
    "contrast (2+ conditions), not a single annotated sample. State the UNIT OF REPLICATION the plan "
    "implies (how many samples per arm, read off the profile) and flag any step whose statistics "
    "assume more replication than the data has.\n"
    "(4) USES WHAT IS ALREADY THERE — if the profile shows existing cell-type labels, QC columns, "
    "doublet calls or an integrated embedding, a step that recomputes them from scratch is waste and "
    "usually worse than what it replaces. Say which existing column the step should use instead.\n"
    "(5) LEGIBLE — each step should name the tool it runs, so a researcher can see which test is "
    "about to be applied. Flag a step whose method cannot be identified from its text.\n"
    "(6) COMPUTABLE — read every operation a step specifies and ask whether it can actually be "
    "computed from the things it names. The failure to catch: a step proposed correlating each "
    "gene's log fold-change against the between-arm MEDIAN library size — a per-gene vector "
    "against a single number, which has no second variable and cannot be computed. It reached "
    "execution and failed three times. Watch for correlating or regressing something per-gene or "
    "per-cell against a per-GROUP summary; testing an effect with no contrast; comparing a "
    "quantity to itself; and any 'flag genes that correlate with <a scalar>'. Name the "
    "computable operation the step should perform instead — for depth imbalance that is "
    "`run_depth_matched_de` (down-sample the deeper arm, re-rank, correlate the two RANKINGS).\n"
    "(7) THE OUTPUTS ARE PRODUCED — every figure, table or quantity a step promises must be "
    "computed by some step in the plan. The failure to catch: a figures step promised a "
    "cell-type composition chart while no step ran a composition analysis at all, so the plan "
    "committed to showing a number nothing calculated. Either add the step that computes it "
    "(here `run_composition`) or drop the promise.\n"
    "(8) THE DESCRIPTION MATCHES THE TOOL — when a step names a tool, its prose must describe "
    "what that tool actually does. The failure to catch: a step said preranked GSEA would return "
    "scores 'based on log2FC ranks' when `run_gsea_prerank` ranks on the Wilcoxon z-score, and "
    "the same sentence claimed both. A wrong description is copied verbatim into the paper's "
    "Methods, where nobody can check it against the code.\n"
    "(10) THE PRIMARY ANALYSIS IS PRESENT — before critiquing what IS written, name the analysis "
    "the question and the protocol are asking for, and check the plan actually contains it. Every "
    "other rule here judges the steps that exist; this one is about the step that is missing, "
    "which nothing downstream can notice. The failure to catch: a differential-expression plan "
    "arrived with QC, a depth-matched check, composition and two enrichment steps — and no DE "
    "step at all. The enrichment steps read a DE table no step wrote. It passed this review "
    "unflagged, twice. If the primary analysis is absent, that is the issue to report, and the "
    "revision must ADD it.\n"
    "(9) LITERATURE STEPS ASK, THEY DO NOT ASSERT — a step that searches the literature must "
    "phrase its questions without asserting the biology it is about to look up. The failure to "
    "catch: a step described DDX41 as 'a DEAD-box helicase (BRR2) in the U4/U6 snRNP complex'. "
    "BRR2 is a different protein of a different family, and that false premise would have steered "
    "every query and every citation that came back. Strip factual claims about a gene, protein or "
    "complex out of the query and let the retrieved papers supply them.\n"
    "Reply with "
    'ONLY a JSON object: {"issues": ["<specific problem>", ...], "revised_agenda": ["<step>", ...]}. '
    "YOUR revised_agenda MUST FIX WHAT YOUR OWN issues REPORT. The two fields are one answer, not "
    "two: an issue that says a step is missing and a revised_agenda that still lacks it is "
    "self-contradictory, and the reader downstream sees only the agenda. Measured: reviewing "
    "drafts whose DE step was missing, this review named the omission correctly every time and "
    "then returned an agenda without the DE step in three of four attempts. Before you answer, "
    "re-read your issues list against your revised_agenda and make them agree.\n"
    "``revised_agenda`` is your proposed corrected plan — add the missing analysis or reconciliation "
    "step where one is needed, drop orphan/circular/contradictory steps, keep the other step text "
    "verbatim; return the ORIGINAL agenda unchanged if it is already sound.\n"
    "WEIGH ADDING AND DROPPING DIFFERENTLY. The two mistakes do not cost the same: a step you "
    "wrongly drop is gone silently — no later step reports its absence, and the result simply "
    "lacks an analysis nobody sees was missing — while a step you wrongly keep costs some compute "
    "and is visible in the output where a reader can discount it. So hold a drop to the higher "
    "bar: say which specific thing makes it redundant or impossible ON THIS DATA, and if the "
    "reason is only that a step looks tangential to a narrowly worded question, keep it. Measured "
    "on this prompt: reviewing two drafts that were missing their DE step, this review added "
    "nothing and removed the composition step from both. Dropping nothing is a perfectly good "
    "review; a shorter agenda is not the goal. The PI makes the final call."
)
# The Critic writes its issues in prose, so "did it report a missing analysis, and WHICH one"
# are both text questions. The family matters: a first version of this asked only whether the
# agenda named *any* analysis tool, and every draft it was meant to catch named run_scanpy_qc,
# run_composition and run_enrichment — just not the DE step the Critic was complaining about — so
# the guard never fired once. Resolve the issue to a tool FAMILY and check for that family.
# NB: no trailing \b — the truncated stems ("never comput") must still match "never computed",
# and a \b after "comput" demands a boundary that "computed" does not have. The leading \b stays.
_MISSING_WORDS = re.compile(
    r"\b(missing|absent|lacks?|lacking|no\s+\S+\s+step|no step|never comput|not comput|"
    r"nothing comput|does not comput)", re.I)
# Ordered: the first family whose words appear in the issue is the one the Critic means.
_ANALYSIS_FAMILIES: "tuple[tuple[Any, tuple[str, ...]], ...]" = (
    (re.compile(r"\b(differential expression|\bDE\b|contrast|fold[- ]change|pseudobulk)\b", re.I),
     ("run_de", "run_pseudobulk_de")),
    (re.compile(r"\b(composition|proportion|abundance)\b", re.I), ("run_composition",)),
    (re.compile(r"\b(enrichment|pathway|GSEA|ORA)\b", re.I), ("run_enrichment", "run_gsea_prerank")),
    (re.compile(r"\b(clustering|cluster|annotation|cell[- ]type label)\b", re.I),
     ("run_clustering", "scgpt_annotate")),
    (re.compile(r"\b(quality control|\bQC\b|filtering)\b", re.I), ("run_scanpy_qc",)),
    (re.compile(r"\b(depth|library size|down-?sampl)\b", re.I), ("run_depth_matched_de",)),
)


def _missing_analysis_family(issues: "list[str]") -> "tuple[str, ...] | None":
    """The tool family a Critic issue says is ABSENT, or None if none does.

    Narrow on purpose: the issue must say something is missing AND name an analysis, so
    "the DE step should also report effect sizes" does not trip it.
    """
    for issue in issues:
        if not _MISSING_WORDS.search(issue):
            continue
        for pattern, tools in _ANALYSIS_FAMILIES:
            if pattern.search(issue):
                return tools
    return None


def _names_tool(agenda: "list[str]", tools: "tuple[str, ...]") -> bool:
    """Does any step name one of ``tools``?  (Steps quote tools in backticks.)"""
    text = "\n".join(agenda)
    return any(re.search(r"`%s`" % re.escape(t), text) for t in tools)


_PLAN_REVIEW_PI_SYSTEM = (
    "You are the Principal Investigator finalizing the analysis plan after the Critic reviewed it, "
    "BEFORE any step runs. You OWN the plan. You are given the question, the dataset profile, the draft "
    "agenda, and the Critic's issues + proposed revision. Decide the FINAL agenda: adopt the fixes you "
    "agree with, keep steps you still want, never drop a step you believe is needed — but DO remove a "
    "genuinely orphan, circular, or redundant step and DO add a reconciliation step where a claim needs "
    'one. Reply with ONLY a JSON object: {"final_agenda": ["<step>", ...], "reason": "<one sentence>"}.'
)

_MEETING_SYNTH_SYSTEM = (
    "You are the Principal Investigator synthesizing a team meeting. The members were given "
    "DIFFERENT slices of the evidence on purpose, so where they disagree the disagreement is a "
    "RESULT, not noise to be smoothed away — and an unresolved question is a more useful output "
    "than a consensus that was never earned. Write two sections, grounded ONLY in what was "
    "actually said:\n"
    "AGREED — the decisions the team genuinely converged on, each with the reason.\n"
    "UNRESOLVED — every substantive disagreement: WHO holds which position, what evidence each "
    "cites, and WHAT WOULD SETTLE IT (the specific check, table, or figure). If a member said its "
    "view was partial, say what it could not see.\n"
    "Leave UNRESOLVED empty ONLY if there was genuinely no disagreement. Never invent agreement, "
    "never invent data, and never resolve a disagreement by picking a side the evidence does not."
)


# Tools a MEETING expert may call. A whitelist, never a blacklist: a meeting must not be able to
# mutate the run's state, and the analysis catalog is full of tools that rewrite the checkpoint
# chain. These three only READ — the data file, and the published literature — which is exactly
# what lets one expert hold evidence the others do not.
_MEETING_TOOLS = frozenset({"inspect_dataset", "literature_search", "deep_literature"})


def _assign_evidence(experts: "tuple[Specialist, ...]",
                     rounds: "list[LabRound]") -> "list[str]":
    """Give each expert a DIFFERENT slice of the accepted findings — one block per expert.

    Without this, every member of a "team" sees the identical prompt and differs only by a persona
    string, so their disagreement is drawn from one posterior: it is self-consistency sampling
    wearing name tags, not a team. Disagreement between agents that examined DIFFERENT evidence is
    information; disagreement between agents that read the same paragraph is sampling noise.

    Findings are dealt round-robin so each expert owns a comparable share, and each is told the
    others hold the rest — an expert that does not know its view is partial reports it as the whole
    picture. Empty (no accepted findings yet, i.e. the design meeting) returns blank blocks, and the
    asymmetry there comes from the tools instead."""
    n = max(1, len(experts))
    accepted = [r for r in rounds if r.verdict.verdict == "accept"]
    if not accepted:
        return [""] * n

    def _line(r: "LabRound") -> str:
        res = r.scientist_result or {}
        answer = str(res.get("final_answer") or "").strip() or "(no write-up)"
        ev: list[str] = []
        for s in res.get("steps", []) or []:
            ev += [p for p in evidence_pointers(s.get("result")) if p not in ev]
        tail = f"\n   evidence on disk: {', '.join(ev[:6])}" if ev else ""
        return f"- Step {r.step_index}: {r.step}\n   result: {answer[:600]}{tail}"

    blocks: list[str] = []
    for i in range(n):
        mine = accepted[i::n]
        others = len(accepted) - len(mine)
        if not mine:
            blocks.append(
                "YOUR ASSIGNED EVIDENCE: none — the findings were shared among the other members. "
                "Contribute from your expertise and interrogate what they report; do not invent "
                "results you were not given.")
            continue
        head = (f"YOUR ASSIGNED EVIDENCE — you are the member responsible for these "
                f"{len(mine)} finding(s). The other {others} finding(s) were assigned to OTHER "
                "members, so your view is deliberately PARTIAL:")
        foot = ("\nReport what YOUR evidence actually shows — read the artifacts above with your "
                "tools if the write-up is thin or you doubt it. Say plainly where your evidence "
                "may CONTRADICT what another member is likely to report, and what would settle it. "
                "Do NOT claim anything about findings you were not assigned.")
        blocks.append(head + "\n" + "\n".join(_line(r) for r in mine) + foot)
    return blocks


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


def _round_feedback(score: float, critique: str) -> str:
    """Score-driven instruction for the NEXT round's experts: the challenge is conditioned on
    the Critic's ACTUAL assessment (not a blanket 'disagree'). Low score -> push back hard;
    mid -> address concerns + challenge weak claims; high -> consolidate (the meeting also ends
    early before reaching here when the score clears ``meeting_accept_score``)."""
    if score < 0.5:
        return (f"\n\nThe Critic found serious problems (score {score:.2f}): {critique}\n"
                "Reconsider your position and push back HARD where the evidence is weak — do NOT just agree.")
    if score < 0.8:
        return (f"\n\nThe Critic raised concerns (score {score:.2f}): {critique}\n"
                "Address these specifically and challenge any claim you find unsupported.")
    return (f"\n\nThe Critic was largely satisfied (score {score:.2f}) but noted: {critique}\n"
            "Consolidate and tighten — refine only what materially matters. A high score is NOT a "
            "reason to drop a disagreement you still hold: if your evidence still conflicts with "
            "the synthesis, say so and name what would settle it.")


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
    # Hypothesis-driven exploration — the ONLY path by which the plan GROWS mid-run. Off = today's
    # behaviour exactly: the agenda drafted before any data is seen can be skipped/amended/pruned but
    # never extended, so the run can only ever execute the paths it started with. On: after a step is
    # ACCEPTED the PI reads its result against the whole plan and, if the result genuinely contradicts
    # the plan's premise, records a FALSIFIABLE hypothesis (statement + prediction + discriminating
    # test) and appends the step(s) that test it — each one still passing the pre-flight gate, the
    # Critic, and ``max_steps`` before it costs anything. Later steps adjudicate the open hypotheses,
    # so the ledger closes the loop instead of just generating work. See ``agents/hypotheses.py``.
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
    # User-REQUIRED atomic skills (the console's skill multi-select): names from the ``skills/``
    # library the run MUST apply. Unlike the global on-demand manifest, these are mandatory.
    required_skills: "tuple[str, ...]" = ()


@dataclass(frozen=True)
class CriticVerdict:
    verdict: str   # "accept" | "revise"
    score: float
    critique: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CriticVerdict":
        return cls(str(d.get("verdict", "revise")), float(d.get("score", 0.0)), str(d.get("critique", "")))


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
    ``modify_note`` steers the changed step; ``guidance`` restores the skill steering.
    """

    agenda: list[str]
    prior_rounds: list["LabRound"]
    from_step_index: int            # 0-based index of the changed step (always re-run)
    modify_note: str = ""
    guidance: str | None = None
    redo_indices: "frozenset[int] | None" = None   # None = evaluate; else the exact 0-based set to re-run

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
                   redo_indices=frozenset(redo_indices) if redo_indices is not None else None)


# --- plan revision: patch ONE step instead of re-drafting the whole plan -----------------------
# MEASURED (experiments/plan_revision_ab, 2026-08-10, the real served Qwen3.6, 19 trials): asking
# the PI to re-draft the plan with the user's feedback damaged steps the user never mentioned in
# 100% of trials — a mean of 3.89 other steps rewritten, and the plan's length changed 89% of the
# time. In the worst case "use resolution 1.0" silently replaced the differential-expression step
# with a descriptive summary and dropped the final step. The SAME model asked for a PATCH instead
# was 19/19 perfect, because the steps it does not name are never regenerated at all.
#
# So the revision path asks for a patch first. Anything a single-step edit cannot express — adding,
# removing or reordering steps — is signalled by the model with step 0 and falls back to the
# unchanged whole-plan redraft, which is still what handles those cases.
# One step, written by the PI, to be INSERTED into an existing plan by scaffolding that has decided
# the step must exist (a requested literature step; a DE step that planned enrichment depends on).
# The scaffolding used to write these itself from string templates — which is how a plan card came
# to say "Literature search for has donors". Deciding THAT a step exists is a deterministic guard's
# job; deciding WHAT it says is the planner's, with the full question and dataset in front of it.
_PLAN_AUTHOR_STEP_SYSTEM = (
    "You are the Principal Investigator. Your analysis plan is missing ONE step that the study "
    "needs; write that single step. Write it exactly like the other steps: start with a short bold "
    "title (`**2-6 word title** — `), then one or two complete sentences a researcher can review — "
    "the TOOL in backticks, the concrete operation, the UNIT any statistic runs over, and the key "
    "settings with their numbers (state the default when you keep it). Adapt it to THIS question "
    "and dataset; do not restate the other steps. Reply with ONLY the step text — no numbering, no "
    "JSON, no preamble."
)


_PLAN_PATCH_SYSTEM = (
    "You are revising ONE step of an analysis plan. You are given the numbered plan and the user's "
    "requested change. Identify the SINGLE step the user is talking about and rewrite ONLY that "
    "step, keeping its style and level of detail — including the leading `**bold title** — ` if "
    "the plan's steps carry one (update the title when the change makes it stale). Do NOT rewrite, "
    "reorder, add, or remove any other step. "
    'Reply with ONLY a JSON object: {"step": <1-based step number>, "new_text": "<the rewritten '
    'step>"}. '
    'If the request CANNOT be expressed as a change to one existing step — it adds a step, removes '
    'one, reorders them, or changes the whole approach — reply {"step": 0, "new_text": ""} instead. '
    "Do not guess in that case; step 0 is the correct answer and the plan will be redrafted properly."
)


def _parse_plan_patch(raw: str, n_steps: int) -> "tuple[int, str] | None":
    """``(0-based index, new step text)`` from the patch reply, or ``None`` when it is unusable or
    the model declined (step 0). ``None`` means "fall back to the redraft" — never "do nothing"."""
    obj = safe_json_loads(raw)
    if not isinstance(obj, dict):
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        try:
            obj = json.loads(m.group(0)) if m else None
        except (ValueError, TypeError):
            obj = None
    if not isinstance(obj, dict):
        return None
    try:
        n = int(obj.get("step", 0))
    except (TypeError, ValueError):
        return None
    text = _strip_step_ordinal(str(obj.get("new_text") or ""))
    if not (1 <= n <= n_steps) or not text:
        return None
    return n - 1, text


_RESUME_EVAL_SYSTEM = (
    "You are the Principal Investigator deciding, after ONE analysis step is re-run, which of the "
    "LATER steps must also be re-run because their result depends on the changed step's output. "
    "Answer with ONLY a JSON array of step numbers to re-run (e.g. [3,4]); [] if none."
)


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
        self.config = config or LabConfig()
        self._complete_fn = complete_fn
        # Scientist = the tool-calling harness with the FULL registry catalog (curated
        # tools + analysis line + schematic + run_code CodeAct). Tests inject their own.
        from .registry import build_scientist_catalog
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
            from .agent_memory import AgentMemory
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
    def self_sourced(self) -> str:
        """What the PI decided from its OWN knowledge rather than from the guidance or the dataset
        profile (see ``_split_self_sourced``). ``''`` when it disclosed nothing."""
        return self._self_sourced

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
            self._checkpoint(LabResult(question, list(agenda), list(rounds), False, accepted, "",
                                       self._ledger.to_list()))
        except Exception as exc:  # noqa: BLE001 - durability metadata must never fail a run
            print(f"[lab] mid-run checkpoint failed: {exc}")

    def run(self, *args: Any, **kwargs: Any) -> LabResult:
        """Run the lab, then stamp on the papers the experts consulted while DESIGNING the study.

        Twelve code paths construct a ``LabResult``; stamping here rather than at each of them is
        what keeps the design background from being present on some exits and missing on others.
        Mid-run ``checkpoint`` snapshots deliberately go without it — it is a property of the
        finished run, and the meeting that produced it may not have happened yet.
        """
        result = self._run_inner(*args, **kwargs)
        if isinstance(result, LabResult) and not result.design_background:
            with self._design_lock:
                result.design_background = list(self._design_citations)
        if isinstance(result, LabResult) and not result.claim_audit and self._claim_audit:
            result.claim_audit = dict(self._claim_audit)
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
    ) -> LabResult:
        emit: EventFn = on_event or (lambda _e: None)
        self._checkpoint = checkpoint

        # A2 continuation: re-enter the SAME state machine without re-planning. Skip skill/mode/PI
        # planning, reuse the prior agenda + accepted rounds, and re-execute from the changed step
        # onward. The kept steps' analysis checkpoints (work/adata_*.h5ad) are reused off disk.
        if resume is not None:
            if resume.guidance is not None:
                self._guidance = resume.guidance
            agenda = list(resume.agenda)
            k = max(0, min(resume.from_step_index, len(agenda) - 1))
            # Decide which downstream steps actually depend on the change and must be re-run; the
            # rest (e.g. an independent literature step) are reused. Explicit set wins; else evaluate.
            redo = set(resume.redo_indices) | {k} if resume.redo_indices is not None \
                else self._evaluate_redo_indices(agenda, k, resume.modify_note, resume.prior_rounds, emit)
            # Reuse the prior accepted round for every step NOT being re-run (by 0-based index); any
            # step without a reusable round falls into the re-run set so nothing is silently skipped.
            kept_by_index = {r.step_index - 1: r for r in resume.prior_rounds
                             if (r.step_index - 1) not in redo}
            redo_indices = frozenset(i for i in range(len(agenda))
                                     if i in redo or i not in kept_by_index)
            emit({"type": "run_resumed", "agenda": len(agenda), "from_step": k + 1,
                  "redo": sorted(i + 1 for i in redo_indices),
                  "kept": sorted(i + 1 for i in kept_by_index)})
            return self._run_loop(
                question, agenda, emit, should_cancel, pull_injections,
                exec_roster=self.config.specialists, exec_multi=self.config.multi_specialist,
                redo_indices=redo_indices, kept_by_index=kept_by_index, seed_notes=resume.modify_note)

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
        if self._guidance is None and self.config.auto_select_skill:
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
        # Stop coverage for the PRE-execution phases. should_cancel was only consulted inside the
        # execution loops, so a Stop clicked during mode routing, team formation, a multi-round
        # design meeting (minutes of tool-using expert turns), or the PI's drafting was silently
        # ignored until execution began. Stored on self so _team_meeting can break between rounds.
        self._should_cancel = should_cancel
        # Per-RUN, not per-object: a ResearchLab instance can serve more than one run, and a
        # spent budget carried into the next one would silently stop asking from the first
        # failure — the opposite of the guarantee.
        self._human_failure_asks = 0

        def _cancelled_before_plan() -> "LabResult | None":
            if should_cancel is not None and should_cancel():
                emit({"type": "run_cancelled", "completed_steps": 0, "agenda": 0})
                return LabResult(question, [], [], False, 0,
                                 "Stopped by the user before the plan was drafted — no tools ran.")
            return None

        self._mode = self._route_mode(question, emit) if self.config.mode == "auto" else self.config.mode
        exec_roster, exec_multi = self.config.specialists, self.config.multi_specialist
        if self._mode == "team":
            self._team = self._form_team(question, emit)
            exec_roster, exec_multi = self._team, True
            design = self._team_meeting(
                question,
                "How should we approach this question? Propose the analysis plan, the key "
                "pitfalls to avoid, and how to validate the result.",
                self._team, "design", emit)
            self._guidance = ((self._guidance + "\n\n") if self._guidance else "") + \
                "Team design-meeting synthesis (incorporate into the plan):\n" + design

        # PI node. In plan mode the PI may ask a clarifying question first; without it,
        # force a straight agenda (there is no one to answer a clarify).
        if (early := _cancelled_before_plan()) is not None:
            return early
        kind, payload = self._pi_plan(question, emit, allow_clarify=plan_review is not None)
        # PI<->Critic review runs BEFORE the plan reaches the human, not instead of it. The old
        # condition skipped the review whenever a human was curating ("their decision wins") —
        # and "Plan first" is checked by default, so on the path almost every real run takes, the
        # only thing that ever read a plan back was switched off. A human reviewing a plan that
        # has already been critiqued still decides everything; they just decide with the Critic's
        # objections in front of them instead of without them.
        if plan_review is not None and kind == "agenda":
            payload = self._plan_review(question, list(payload), emit)
            emit({"type": "pi_agenda", "agenda": payload})

        # Plan mode (human-in-the-loop): the user does NOT text-edit the plan — they
        # review it and reply in natural language; that feedback goes BACK to the PI,
        # which re-drafts. The loop runs until the user approves an agenda or cancels.
        # ``plan_review(kind, payload)`` blocks for the user and returns a decision dict:
        #   {"action": "approve"}                 -> run the current agenda
        #   {"action": "revise", "feedback": ...} -> re-plan with the user's notes
        #   {"action": "cancel"}                  -> abort, run nothing
        if plan_review is not None:
            feedback_log: list[str] = []
            reported_tooling: set[str] = set()
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
                if kind == "agenda":
                    payload = self._check_plan_tooling(list(payload), emit, reported_tooling)
                decision = plan_review(kind, payload) or {"action": "cancel"}
                action = str(decision.get("action", "cancel")).lower()
                if action == "approve" and kind == "agenda":
                    break
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

        # PI↔Critic PLAN-TIME review (before any step runs) — the plan-time complement to the per-step
        # meetings: catch an incoherent plan (orphan de-novo clustering never reconciled with provided
        # labels, circular enrichment, a step nothing consumes) at its SOURCE, before any compute. Only
        # In plan mode this already ran ABOVE, before the human saw the plan; here it covers the
        # autonomous path, where nothing else ever reads the plan back.
        if plan_review is None:
            agenda = self._plan_review(question, agenda, emit)

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
        # artifacts/ and the report is assembled automatically; the step is a slot for nothing.
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

        if self.config.max_cycles <= 1:
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
            done = "\n".join(f"- {r.step}: {r.scientist_result.get('final_answer') or '(no answer)'}"
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
        from .skill_induction import candidates, induce, write_skill
        from .skills import SKILLS as _LIB, register_skill
        try:
            cands = candidates(rounds)
            if not cands:
                return []
            kept, rejected = induce(
                cands, self._complete,
                existing_manifest=skill_manifest(),
                tool_names=", ".join(t.name for t in self.scientist.catalog if t.name != "finish"),
                taken=set(_LIB), max_new=self.config.max_induced_skills)
            written: list[str] = []
            for skill in kept:
                out = write_skill(self.config.induced_skills_dir, skill)
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
                emit({"type": "skill_induced", "name": written_name,
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
        by_index = {r.step_index - 1: r for r in prior_rounds}

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
            # work this round just produced is still resumable.
            self._checkpoint_state(question, agenda, rounds,
                                   accepted_steps + (1 if verdict.verdict == "accept" else 0))

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
                f"{r.scientist_result.get('final_answer') or '(no answer)'}"
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
        executed_indices = {r.step_index - 1 for r in rounds}
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
                from .dag import LabPlan, TaskNode
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
                from .dag import LabPlan
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
            from .dag import LabPlan
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
            result = self._scientist(question, step_text, specialist, critique,
                                     prior_rounds + node_rounds, emit, should_cancel,
                                     user_notes=step_notes, memory=mem_block,
                                     extra_steps=_budget_bonus(last_result, attempts))
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
        if not choice or low == skip_opt.lower():
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
        from .lab_graph import build_lab_graph, order_rounds, serialize_conflicting_nodes

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
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=len(batch)) as ex:
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
                f"- {r.step}: {r.scientist_result.get('final_answer') or '(no answer)'}"
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
        the profile says neither does the PI's judgement of the question decide."""
        forced = _dataset_mode_rule((self.ctx.decisions or {}).get("dataset_result"))
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
            if step.get("tool") not in _LITERATURE_TOOLS:
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
        tools = [t for t in getattr(self.scientist, "catalog", []) if t.name in _MEETING_TOOLS]
        if not tools or not self.config.meeting_tools:
            return self._complete_concurrent(msg_lists)

        from dataclasses import replace as _dc_replace

        from .research_harness import HarnessConfig, ResearchHarness
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
                          "state": "done", "detail": str(ev.get("summary") or "")[:200]})
                elif et == "tool_error":
                    emit({"type": "expert_tool", "member": member, "tool": ev.get("tool"),
                          "state": "error", "detail": str(ev.get("error") or "")[:200]})
            return _on

        def _one(item: "tuple[str, list[dict[str, Any]]]") -> str:
            member, messages = item
            brief = messages[0]["content"] + "\n\n" + messages[1]["content"]
            try:
                harness = ResearchHarness(catalog=list(tools), config=cfg,
                                          chat_fn=getattr(self.scientist, "_chat_fn", None))
                res = harness.run(brief, self.ctx, on_event=_relay(member))
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

        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(len(msg_lists), self.config.max_meeting_concurrency))
        labels = list(names or [])
        items = [(labels[i] if i < len(labels) else f"expert {i + 1}", m)
                 for i, m in enumerate(msg_lists)]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(_one, items))

    def _team_meeting(self, question: str, topic: str, experts: tuple[Specialist, ...],
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
            return ""
        head: list[str] = []
        if dr.get("cells") and dr.get("genes"):
            head.append(f"{dr['cells']} cells x {dr['genes']} genes")
        if dr.get("dataset_kind"):
            head.append(str(dr["dataset_kind"]))
        lines = ["Dataset profile" + (f": {', '.join(head)}." if head else ":")]

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
                small = [l for l, per in lab.items() if min(per.values()) < 30]
                # IN THE FILE, before QC. A plan that quoted these as the tested counts ("Microglia
                # 60 / 24") handed pre-QC numbers down to the report (run f3731e0b7136 tested 57 / 21).
                parts.append(f"Cells per `{dba.get('label_column')}` per arm in the uploaded file, "
                             "BEFORE QC (QC removes cells, so a step's tool reports the counts it "
                             "actually tested; quote those, not these, for tested groups): "
                             + "; ".join(f"{l} " + "/".join(str(per[a]) for a in arms)
                                         for l, per in lab.items())
                             + f" (order {'/'.join(arms)})."
                             + (f" {len(small)} label(s) have <30 cells in an arm and cannot be "
                                f"tested per cell type: {', '.join(small)}." if small else ""))
            if dba.get("depth_imbalance"):
                parts.append("⚠ DEPTH IMBALANCE: " + str(dba["depth_imbalance"]) + " Plan for it: "
                             "state it, and treat a same-direction shift across all cell types "
                             "(especially ribosomal / translation genes) as technical until a "
                             "depth-matched or pseudobulk-normalised comparison says otherwise. "
                             "But the two DIRECTIONS are not symmetric, and a write-up that "
                             "misses this reaches a confidently wrong conclusion: depth inflates "
                             "detection, so it can only manufacture apparent UP-regulation in the "
                             "DEEPER arm. Genes DOWN in the deeper arm ran against the gradient "
                             "and cannot be a depth artefact — they are the most credible set in "
                             "the whole comparison, and a lopsided up/down count (e.g. 5,028 up "
                             "vs 508 down) is itself the depth signature pointing at which side "
                             "to trust. Never write 'all shifts are depth artefacts' without "
                             "saying, per cell type, which direction ran with the gradient and "
                             "which against it.")
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
        """Draft (or re-draft) the plan. Returns ``("agenda", [steps])`` or, when
        ``allow_clarify`` and the request is genuinely ambiguous, ``("clarify",
        [{question, options}])``. ``feedback`` (the user's natural-language notes on the
        previous draft) + ``prior_agenda`` drive a revision instead of a fresh plan.

        A revision tries a SINGLE-STEP PATCH first (``_patch_plan``) and only redrafts the whole
        plan when the request cannot be expressed that way. ``latest_feedback`` is the note the
        user just typed; the patch is judged against that alone, because ``feedback`` accumulates
        every earlier note and re-applying those to an agenda that already carries them would
        undo the user's own earlier revisions."""
        emit({"type": "planning"})   # heartbeat: drafting takes 20-60s and used to render nothing
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
            f"- {t.name}: {t.description}" for t in self.scientist.catalog if t.name != "finish"
        )
        # A pre-selected research path STEERS the PI here (it does not bypass it): the PI
        # still drafts the agenda — adapting the guidance to this dataset — and plan mode
        # still lets the user shape the result, in natural language, before any tool runs.
        parts = [f"Research question:\n{question}\n"]
        if self._guidance:
            parts.append(f"Follow this research-path guidance when planning:\n{self._guidance}\n")
        dataset_ctx = self._dataset_context()
        if dataset_ctx:
            parts.append(dataset_ctx + "\n")
        parts.append(f"The scientist can run ONLY these tools (plan steps achievable with them):\n{tools_desc}\n")
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

        raw = self._complete([
            {"role": "system", "content": _PI_SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ], role="plan")
        parsed = _parse_plan(raw, self.config.max_steps, allow_clarify)
        if parsed is None:
            # The PI's output did not parse — most often because the reply was CUT OFF at the
            # role's output ceiling, which leaves the agenda JSON unterminated. The fallback puts
            # the user's own question in as the only step, and until now said nothing: the reviewer
            # got a plan card whose first step was their own question restated, with a literature
            # step appended after it, and no sign that anything had failed. Say so. A degradation
            # the reader cannot see is worse than the failure it is standing in for.
            emit({"type": "plan_unparsed", "chars": len(raw or ""),
                  "preview": " ".join((raw or "").split())[:200]})
            parsed = ("agenda", [question])
        kind, payload = parsed
        if kind == "clarify":
            emit({"type": "pi_clarify", "questions": payload})
        else:
            # Split off the PI's SELF-SOURCED disclosure — what it decided on its own knowledge
            # rather than from the guidance or the profile. It is a statement, not work: leaving it
            # in the agenda would hand it to the Scientist as a step to execute. Kept on the lab so
            # the plan the user reviews, plan.md and the technical report can all carry it, because
            # "which of these choices did the protocol actually specify" is otherwise unanswerable
            # once the numbers are in a Methods section.
            payload, disclosure = _split_self_sourced(list(payload))
            if disclosure:
                self._self_sourced = disclosure
                emit({"type": "plan_self_sourced", "text": disclosure})
            payload, echoed = _drop_question_echo(list(payload), question)
            if echoed:
                emit({"type": "steps_pruned", "reason": "question_echo", "steps": echoed})
            #after PI generate agenda, check if the literature is inside
            _agenda_now = list(payload)
            payload = _ensure_literature_agenda(
                _agenda_now,
                question=question,
                guidance=self._guidance,
                feedback=feedback,
                max_steps=self.config.max_steps,
                has_literature_tool=any(t.name in ("deep_literature", "literature_search")
                                        for t in self.scientist.catalog),
                author=lambda fb: self._author_step(
                    question, _agenda_now,
                    brief=("A literature step near the end: search the published literature with "
                           "`deep_literature` for the key genes, cell types and pathways this "
                           "study will surface, and attach DOI-backed citations that ground the "
                           "biological interpretation. Name the biological subject of THIS "
                           "question; do not restate the analysis steps."),
                    fallback=fb),
            )
            emit({"type": "pi_agenda", "agenda": payload})
        return kind, payload

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
        from . import context_budget as cb
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
        from . import context_budget as cb
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
                  "summary": f"{'ok' if ok else 'error'}: {len(res or [])}"})
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
        q = _literature_query(question, step, rounds) or step or question
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
        emit({"type": "tool_result", "tool": "deep_literature",
              "summary": f"{'ok' if ok else state}: {len(contexts)} context(s)"})
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
        if ok and cites:
            lines = [answer_text] if answer_text else []
            if cites:
                lines += ["", "Citations from the indexed corpus (use ONLY these in the References "
                              "section — do NOT add any others):"]
                lines += [f"{i}. {c}" for i, c in enumerate(cites[:12], 1)]
            return HarnessResult("ok", "deep_literature_corpus", "\n".join(lines), tool_calls, [])
        note = (output.get("error") or output.get("note")) if isinstance(output, dict) else None
        return HarnessResult(
            "incomplete", "no_literature_citations",
            "The indexed corpus returned no grounded answer or citations for this question. State "
            "this limitation in the report rather than inventing references.",
            tool_calls, [{"tool": "deep_literature", "error": note or "no corpus answer"}],
        )

    def _scientist(self, question: str, step: str, specialist: Specialist, critique: str,
                   rounds: list[LabRound], emit: EventFn,
                   should_cancel: "Callable[[], bool] | None" = None,
                   user_notes: str = "", memory: str = "",
                   extra_steps: int = 0) -> HarnessResult:
        # Literature grounding: prefer deep_literature (PaperQA over the lab's indexed corpus on
        # HPC3); fall back to literature_search (Europe PMC) ONLY when the corpus tool isn't wired
        # (dev / no-HPC), so behaviour never regresses where deep_literature can't run.
        if _is_literature_step(step):
            deep_tool = next((t for t in self.scientist.catalog if t.name == "deep_literature"), None)
            lit_tool = next((t for t in self.scientist.catalog if t.name == "literature_search"), None)
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
                return self._run_literature_step(question, step, rounds, lit_tool, specialist, emit)
            if lit_tool is not None:
                return self._run_literature_step(question, step, rounds, lit_tool, specialist, emit)

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
            "write a report or package/zip outputs (those are produced automatically). Data you "
            "prepared with run_code goes to a tool through its `input` parameter (a file in "
            "AISCIENTIST_WORK) — do not re-implement the tool's analysis in code to use it."
        )
        # Atomic SKILLS by PROGRESSIVE DISCLOSURE: never the full bodies (they'd bloat context), only
        # a pointer. Small library → list the name+summary MANIFEST inline; large library (> the
        # threshold) → don't list any, tell the agent to search_skills(query) first. Either way the
        # fixed registry stays the small always-on core, and read_skill_reference fetches a body on
        # demand ONLY when a step needs analysis the tools don't cover. Global (pipeline-independent).
        n_skills = len(ATOMIC_SKILLS)
        # Each skill is a folder: SKILL.md (guidance) + reference.py (code), revealed in two levels —
        # `read_skill_reference(name)` for the when-to-use/how-to-adapt guidance, then
        # `read_skill_reference(name, file="reference.py")` for the code to adapt and run.
        _fetch = ("call `read_skill_reference(name)` for its guidance, then "
                  "`read_skill_reference(name, file=\"reference.py\")` for the code, adapt it, and run it "
                  "via run_code (read checkpoints from AISCIENTIST_WORK, write under AISCIENTIST_ARTIFACTS). "
                  "If a tool already covers the step, use the tool instead")
        if n_skills and n_skills <= SKILL_MANIFEST_MAX:
            parts.append(
                "Atomic skills — VETTED, adaptable code templates (not auto-run), NOT in your context. "
                f"If — and only if — THIS step needs analysis the tools above do not cover, {_fetch}:\n"
                + skill_manifest()
            )
        elif n_skills:
            parts.append(
                f"{n_skills} atomic skills are available — VETTED, adaptable code templates (not "
                "auto-run), NOT listed here to save context. If — and only if — THIS step needs "
                "analysis the tools above do not cover, call `search_skills(query)` to find the "
                f"relevant ones by capability, then {_fetch}."
            )
        parts.append("Execute THIS step with the tools, then call `finish` with what you found for this step.")
        brief = "\n\n".join(parts)
        emit({"type": "scientist_start", "step": step, "specialist": specialist.name})
        # Only the USER-provided spans are untrusted for raw-data sniffing — the question and
        # any mid-run notes. The rest of the brief (persona, PI step text, prior-finding
        # artifact paths, tool roster, reference-template manifest) is system-built and trusted
        # by construction, so it is never mistaken for a raw expression dump.
        untrusted_text = "\n".join(t for t in (question, user_notes) if t)
        result = self.scientist.run(brief, self.ctx, on_event=emit, should_cancel=should_cancel,
                                    untrusted_text=untrusted_text, extra_steps=extra_steps)
        # Named-tool-not-called nudge. A step whose text names the tool that IS the step
        # (`run_de`, `run_composition`) sometimes ends with the model writing final text after a
        # round of reconnaissance — inspect, list_dir, read_tool_source — without ever calling it
        # ("stop_reason: model_final_text", zero evidence). The Critic bounces that, correctly,
        # but at the price of a whole extra round. One deterministic nudge, once, is cheaper: if
        # the step names catalog tools, none of them ran, and the model stopped on its own, tell
        # it exactly that and let it finish the step. Never applied to a step that stopped for a
        # cancel, an error budget or a step limit — those are not "forgot to call it".
        named = {n for n in _BACKTICK_TOOL_RE.findall(step or "")}
        catalog = {t.name for t in getattr(self.scientist, "catalog", [])}
        named &= catalog
        called = {str(st.get("tool")) for st in (result.steps or [])}
        if (named and not (named & called)
                and getattr(result, "stop_reason", "") == "model_final_text"
                and not (should_cancel is not None and should_cancel())):
            emit({"type": "tool_nudge", "step": step, "tools": sorted(named)})
            nudge = ("\n\nNOTE: your last attempt ended without calling "
                     + ", ".join(f"`{n}`" for n in sorted(named))
                     + " — the tool this step is about. The reconnaissance is done; call it now with "
                     "the settings the step specifies, then report what it returned.")
            result = self.scientist.run(brief + nudge, self.ctx, on_event=emit,
                                        should_cancel=should_cancel, untrusted_text=untrusted_text,
                                        extra_steps=extra_steps)
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
        raw = self._complete([
            {"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ], role="critic")
        parsed = _parse_verdict(raw) or {}
        verdict = str(parsed.get("verdict", "revise")).strip().lower()
        if verdict not in ("accept", "revise", "reject"):
            verdict = "revise"
        if verdict == "reject":
            verdict = "revise"
        try:
            score = float(parsed.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        critique = str(parsed.get("critique", "")).strip()

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

        v = CriticVerdict(verdict, max(0.0, min(1.0, score)), critique)
        emit({"type": "critic", "step": step, "verdict": v.verdict, "score": v.score,
              "critique": v.critique})
        return v

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

        * off unless ``config.hypothesis_driven``;
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
        if not self.config.hypothesis_driven:
            return []
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
            existing.add(key)
            added.append(text)
            self._ledger.link_test(h.id, text)
            emit({"type": "step_added", "step": text, "hypothesis_id": h.id,
                  "statement": h.statement, "after_step": step})
        self._new_steps_added += len(added)
        return added

    def _plan_review(self, question: str, agenda: list[str], emit: EventFn) -> list[str]:
        """PI↔Critic review of the WHOLE draft agenda, BEFORE any step runs — the plan-time complement
        to the per-step meetings. The single planning pass is one-shot with no second look; this is that
        second look, at the plan's SOURCE (an incoherent plan is caught before any compute is spent).
        The Critic flags orphan/circular/incoherent steps and proposes a revision; the PI (owns the
        plan) finalizes. ``never worse than the draft``: any empty/oversized/garbled revision falls back
        to the original. No-op returning the draft unless ``config.plan_review`` (and 2+ steps)."""
        if not self.config.plan_review or len(agenda) < 2:
            return agenda
        profile = self._dataset_context()
        critic = _parse_verdict(self._complete([
            {"role": "system", "content": _PLAN_REVIEW_CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"research_question": question, "dataset_profile": profile, "draft_agenda": agenda})},
        ], role="critic")) or {}
        issues = [str(x).strip() for x in (critic.get("issues") or []) if str(x).strip()]
        proposed = [str(s).strip() for s in (critic.get("revised_agenda") or []) if str(s).strip()]
        if not issues and (not proposed or proposed == agenda):
            return agenda   # Critic found nothing wrong → no PI round, plan unchanged
        pi = _parse_verdict(self._complete([
            {"role": "system", "content": _PLAN_REVIEW_PI_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"research_question": question, "dataset_profile": profile, "draft_agenda": agenda,
                 "critic_issues": issues, "critic_revised_agenda": proposed})},
        ], role="plan")) or {}
        final = [str(s).strip() for s in (pi.get("final_agenda") or []) if str(s).strip()]
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
                     "draft_agenda": agenda, "critic_issues": issues,
                     "critic_revised_agenda": proposed,
                     "unresolved": f"The Critic reported that the plan is missing an analysis "
                                   f"that none of your steps computes. Add a step that calls one "
                                   f"of {list(_missing)}, keeping every other step you already "
                                   f"decided on."})},
            ], role="plan")) or {}
            second = [str(x).strip() for x in (retry.get("final_agenda") or []) if str(x).strip()]
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
        vocab = _grounding_vocab(accepted)
        vocab_block = f"\n{vocab}\n" if vocab else ""
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
        if accepted and claim_audit.enabled():
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
                f"{ledger_block}"
                f"{audit_prompt}"
                f"{interp}\n"
                "Write the final report now."
            )},
        ], role="writer")
        # Guarantee layer: deterministically CORRECT any fabricated assembly / non-PASS claim that slipped
        # past the grounding prompt, and surface each correction as a diagnostic (not into the manuscript).
        facts_dict, _dists = _collect_facts(accepted)
        report, fact_issues = verify_report_facts(
            report, facts_dict, _uncovered_groups(accepted), tested_counts=_tested_counts(accepted),
            pre_counts=pre_qc_counts((self.ctx.decisions or {}).get("dataset_result")))
        if fact_issues:
            emit({"type": "report_fact_check", "issues": fact_issues})
        return report

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
        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, min(len(message_lists), self.config.max_meeting_concurrency))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(self._complete, message_lists))   # ex.map preserves input order

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
        budgeted, max_tokens = self._budget_single_shot(messages)
        if self._complete_fn is not None:
            return _call_with_role(self._complete_fn, budgeted, role)
        from ..gateway import vllm_client  # deferred: keep agents decoupled from the gateway
        return vllm_client.complete(
            self.ctx.tunnel_port, self.ctx.model, budgeted, max_tokens=max_tokens
        )

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
