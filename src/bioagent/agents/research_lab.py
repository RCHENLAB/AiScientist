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

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .dag import LabPlan, TaskNode, lift_agenda_to_dag, parse_dag
from .hypotheses import HypothesisLedger
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
    "WRITE EACH STEP FOR A RESEARCHER TO READ, IN PLAIN SCIENTIFIC ENGLISH — a complete, self-"
    "contained sentence describing the ANALYSIS and what it reveals biologically: the action, what "
    "it is performed on, its scientific purpose, and what the reader learns or what it produces "
    "(cell populations, marker genes, enriched pathways, condition differences). Do NOT write code "
    "or programmer detail: no tool or function names, no keyword arguments or parameter values "
    "(min_genes=..., groupby=...), no file paths — those are internal execution details a biologist "
    "does not want to read. The plan the user reviews should read like the Methods paragraph of a "
    "paper, not a script. The example below shows ONLY the writing STYLE and level of detail — ADAPT "
    "the real steps to THIS question and dataset (apply the profile rules above); do NOT copy it "
    "verbatim or treat it as a fixed pipeline. "
    'Reply with ONLY a JSON object {"agenda": [step strings]}, e.g. '
    '{"agenda": ['
    '"Assess per-cell data quality and remove low-quality or dying cells, then normalize expression '
    'so the downstream structure reflects real biology rather than differences in sequencing depth, '
    'and report how many cells and genes remain.", '
    '"Group the cells into transcriptionally distinct populations and lay them out on a 2-D map, so '
    'the major cell types present in the tissue can be seen, separated, and counted.", '
    '"For each population, identify the genes that most specifically mark it, establishing the '
    'molecular identity of every cell type.", '
    '"Determine which biological pathways and processes are over-represented in the marker genes of '
    'each population, turning gene lists into interpretable biology.", '
    '"Search the published literature for the key genes and pathways found above and attach real, '
    'DOI-backed citations that support the biological interpretation."'
    ']}. '
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
    "running a standard tool) — most steps are NOT decisions. Reply with ONLY a JSON array covering "
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
        steps = [str(x).strip() for x in parsed if str(x).strip()]
        return steps[:max_steps] or None
    return None


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
        "cover — scanpy/pandas/numpy etc. Read the dataset from the env var BIOAGENT_DATASET, the "
        "run's checkpoints from BIOAGENT_WORK, and write new figures/tables under BIOAGENT_ARTIFACTS "
        "(use os.environ). Returns stdout / a result dict. Set a headless backend (matplotlib Agg). "
        "(pip install of analysis packages is fine if a step needs one.) "
        "STRICT: use this ONLY for analysis (compute figures into figures/, tables into tables/). Do "
        "NOT write or render a report or document (no .docx/.pdf/.html, no python-docx/reportlab/"
        "pandoc), and do NOT zip/package/export the outputs: the final PDF+DOCX report and the "
        "downloadable bundle of everything under BIOAGENT_ARTIFACTS are created AUTOMATICALLY when "
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


_LITERATURE_STEP_RE = re.compile(
    r"\b(literature|references?|citations?|published papers?|paper search)\b"
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
    normalized = (step or "").replace("_", " ")
    return bool(_LITERATURE_STEP_RE.search(normalized))

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


# A pathway/GO/ORA enrichment step (as opposed to a marker-DE step, which is kept for annotation
# validation). Used to deterministically drop enrichment when the dataset has no experimental
# contrast — enriching a known cell type's identity markers is circular ("meaningless enrichment").
_ENRICHMENT_STEP_RE = re.compile(
    r"\b(enrich(?:ment|ed|es|ing)?|over[-\s]?represent(?:ation|ed|s|ing)?|pathway|gene[-\s]?ontology"
    r"|reactome|gsea|gene[-\s]?set[-\s]?enrichment|msigdb|hallmark gene set)\b",
    re.I,
)


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
    r"|\b(\.docx|\.pdf|\.html|\.zip)\b",
    re.I,
)


def _is_report_busywork(step: str) -> bool:
    return bool(_REPORT_BUSYWORK_RE.search((step or "").replace("_", " ")))


def _norm_step(step: str) -> str:
    """Bag-of-words key for "is this the same step?". Used to stop exploration re-adding a step the
    plan already has under slightly different wording — the model's most common failure mode."""
    return " ".join(re.findall(r"[a-z0-9]+", (step or "").lower()))


# A step that clusters the cells de-novo (Leiden/Louvain). Used to detect the "analyze by existing
# labels vs re-cluster de-novo" methodological fork when the dataset already carries cell-type labels.
_CLUSTERING_STEP_RE = re.compile(r"\b(clusters?|clustering|clustered|leiden|louvain)\b", re.I)


def _plan_has_clustering(agenda: "list[str]") -> bool:
    return any(_CLUSTERING_STEP_RE.search((s or "").replace("_", " ")) for s in agenda)


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


def _literature_step_text(question: str) -> str:
    # Label the literature step from the biological SUBJECT of the question. focus_literature_query
    # strips instruction + pipeline/method keywords (VEP, ClinVar, GRCh38, PASS, annotate, …), so a
    # biology question ("DDX41 knockout mouse retina") stays intact while a method-heavy one no longer
    # produces a garbled label. The per-query terms are still (re)planned from the ACCEPTED FINDINGS.
    try:
        from ..tools.literature_search import _QUERY_STOPWORDS, focus_literature_query

        query = focus_literature_query(question)
        # focus_literature_query returns a SHORT non-instruction phrase VERBATIM (it only strips a
        # long / instruction-heavy prompt), so a thin generic run prompt — e.g. the preset default
        # "complete the research" — otherwise leaks in as a garbled label. Keep only CONTENT tokens
        # for the LABEL; if none survive it is pure filler, so use the clean generic form instead.
        # (The per-query terms are (re)planned from the ACCEPTED FINDINGS regardless of this label.)
        query = " ".join(w for w in query.split() if w.lower().strip("-") not in _QUERY_STOPWORDS)
    except Exception:  # noqa: BLE001 - planning guard should never break planning
        query = ""
    return f"Literature search for {query}" if query else "Literature search for the key genes and pathways found"


def _literature_query(question: str, step: str, rounds: "list[LabRound]") -> str:
    """Build a FOCUSED Europe PMC query for a literature-grounding step from the ACCEPTED findings
    (top marker genes + enriched pathway terms) plus the step's topic — instead of the raw run
    question, which pollutes the query with instruction words (e.g. "finish research"). Falls back
    to a focused (question+step) only when there are no findings yet."""
    from ..tools.literature_search import focus_literature_query
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
                    if t and t not in terms:
                        terms.append(t)
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
    # No findings yet → fall back to a focused topic from the step (the clean generic label), then the
    # question. Only used pre-findings; once we HAVE genes, appending question words just pollutes it.
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


def _grounding_facts(rounds: "list[LabRound]") -> str:
    """A CLOSED set of the AUTHORITATIVE figures the tools reported — genome assembly, execution mode,
    PASS/non-PASS + variant/cell counts, thresholds, ratios, and category distributions — injected into
    the synthesize prompt so the report cannot invent a number, echo the plan's ASSUMED assembly over the
    one actually used, or write "0 non-PASS" when the result says otherwise. "" when nothing quantitative
    was produced. Complements _grounding_vocab (labels/terms) + _methods_performed (tools)."""
    facts, dists = _collect_facts(rounds)
    if not facts and not dists:
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
    return "\n".join(lines)


_ASSEMBLY_CANON = {"grch38": "GRCh38", "hg38": "GRCh38", "grch37": "GRCh37", "hg19": "GRCh37"}


def verify_report_facts(report_md: str, facts: "dict[str, Any]") -> "tuple[str, list[str]]":
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
    return md, issues


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
                    if t and t not in bucket:
                        bucket.append(t)
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
    from ..tools.literature_search import focus_literature_query
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
) -> list[str]:
    """Deterministically preserve a literature step when the user requested one."""
    if not has_literature_tool or not _requests_literature(question, guidance, feedback):
        return agenda
    # Build the step LABEL from the research question ONLY — never the feedback (which can carry a
    # team design-meeting synthesis, e.g. "…Convergence Divergences Core Conditional") or the
    # methodology guidance; both pollute focus_literature_query into a garbled step string. Intent
    # detection above still considers all three; the actual per-query terms are planned later from
    # the ACCEPTED FINDINGS, not from this label.
    step = _literature_step_text(question)
    if any(_is_literature_step(existing) for existing in agenda):
        deduped: list[str] = []
        inserted = False
        for existing in agenda:
            if _is_literature_step(existing):
                if not inserted:
                    deduped.append(step)
                    inserted = True
                continue
            deduped.append(existing)
        return deduped[:max_steps]
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
    "performed on, and what it would show — with NO tool names, NO parameters, and NO file paths, "
    "matching the style of the existing plan.\n"
    "You are ALSO given the hypotheses still OPEN from earlier steps. If THIS step's result bears on "
    "one of them, adjudicate it: 'supported', 'refuted', or 'inconclusive', with one sentence of "
    "evidence taken from THIS result. Do not adjudicate a hypothesis this result says nothing about, "
    "and do not re-propose a hypothesis already in the list.\n"
    "Reply with ONLY a JSON object: "
    '{"surprise": "<one sentence: what was unexpected, or exactly \'nothing\'>", '
    '"hypotheses": [{"statement": "...", "prediction": "...", "test": "..."}], '
    '"new_steps": [{"step": "<plain-English step>", "hypothesis": "<verbatim statement it tests>"}], '
    '"resolve": [{"hypothesis": "<verbatim OPEN statement or its id>", '
    '"status": "supported|refuted|inconclusive", "evidence": "<one sentence from this result>"}]}. '
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
    "and what it reveals — with no tool names, no parameters, and no file paths. Never plan a step "
    "that writes, renders, packages, or exports a report: the manuscript is assembled automatically "
    "once all cycles finish.\n"
    "Reply with ONLY a JSON object: "
    '{"continue": true|false, "reason": "<one sentence: what this cycle would settle, or why the '
    'study should stop>", "agenda": ["<step>", ...]}. '
    'With "continue": false, return an empty agenda.'
)
_PLAN_REVIEW_CRITIC_SYSTEM = (
    "You are the Scientific Critic reviewing a DRAFT analysis plan with the PI, BEFORE any step runs. "
    "You are given the research question, the dataset profile, and the ordered draft agenda. Judge the "
    "plan AS A WHOLE, not step by step: (1) is every step NECESSARY and does its output feed the "
    "question or a later step — no orphan branch nothing consumes? (2) are the steps mutually COHERENT "
    "— e.g. if the plan re-clusters de-novo AND also uses provided cell-type labels, are the two "
    "reconciled, or is one of them dead weight producing anonymous clusters no later step maps back to "
    "the labels? (3) are methodological PRECONDITIONS met for THIS dataset — pathway/GO enrichment or "
    "discovery-style DE needs a real experimental contrast (2+ conditions), not a single annotated "
    "sample; a per-cluster claim needs the clusters reconciled with any provided labels? Reply with "
    'ONLY a JSON object: {"issues": ["<specific problem>", ...], "revised_agenda": ["<step>", ...]}. '
    "``revised_agenda`` is your proposed corrected plan — drop orphan/circular steps, add a "
    "reconciliation step where a claim needs one, keep the other step text verbatim; return the "
    "ORIGINAL agenda unchanged if it is already sound. The PI makes the final call."
)
_PLAN_REVIEW_PI_SYSTEM = (
    "You are the Principal Investigator finalizing the analysis plan after the Critic reviewed it, "
    "BEFORE any step runs. You OWN the plan. You are given the question, the dataset profile, the draft "
    "agenda, and the Critic's issues + proposed revision. Decide the FINAL agenda: adopt the fixes you "
    "agree with, keep steps you still want, never drop a step you believe is needed — but DO remove a "
    "genuinely orphan, circular, or redundant step and DO add a reconciliation step where a claim needs "
    'one. Reply with ONLY a JSON object: {"final_agenda": ["<step>", ...], "reason": "<one sentence>"}.'
)

_MEETING_SYNTH_SYSTEM = (
    "You are the Principal Investigator synthesizing a team meeting. Given the team members' "
    "individual contributions and the Critic's critique, write a concise synthesis: the decisions "
    "or conclusions the team converges on, grounded ONLY in what was actually said. Note real "
    "disagreements honestly; do not invent agreement or data."
)


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
            "Consolidate and tighten — refine only what materially matters.")


@dataclass(frozen=True)
class LabConfig:
    # None (default) = derive the round budget from the agenda so EVERY planned step gets to run.
    # Each step is still capped at ``max_revisions`` retries (force-advance), so total work is
    # inherently bounded by len(agenda)×(1+max_revisions) — no step is ever starved. An explicit
    # int is a hard cap (used by tests, or a cloud override via BIOAGENT_MAX_ROUNDS).
    max_rounds: int | None = None
    # Runaway guard on PI agenda length — NOT a planning budget. The PI plans as many steps as the
    # analysis genuinely needs (a thorough study is commonly 6-12 and up to ~20 is fine); this only
    # stops a hallucinated/pathological plan. Tunable via BIOAGENT_MAX_STEPS.
    max_steps: int = 20
    max_revisions: int = 2              # revisions on one step before force-advancing
    # Planner/scheduler. "linear" = the flat-agenda _run_loop (default, unchanged). "dag" =
    # structure the agenda into a dependency DAG and run it with the ready-set scheduler +
    # Coordinator (_run_dag): data-driven ordering, structural task reuse, scoped node briefs.
    # Branch feat/dag-planner — opt-in until proven, so main/prod stay on the linear loop.
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
    # PI↔Critic step meetings (docs/pi_critic_meeting_protocol.md). Two bounded PI↔Critic exchanges
    # wrapped around each step: a PRE-FLIGHT necessity/reasonableness gate before the Scientist runs
    # (skip a circular/redundant/unfounded step, or amend its brief) and a POST-STEP contribution
    # review after the Critic (prune remaining steps a completed step made moot). A deterministic
    # floor (enrichment without a contrast) hard-skips regardless of the models. Off = today's loop.
    # Fully enacted on the linear planner; the DAG planner enacts amend + the floor only (a model
    # "skip"/downstream-prune there needs the scheduler's dependency-aware replan — see the doc).
    step_meetings: bool = False
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
    # cash-flavoured number. Tunable via BIOAGENT_MAX_NEW_STEPS.
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "agenda": list(self.agenda),
            "rounds": [r.to_dict() for r in self.rounds],
            "converged": self.converged,
            "accepted_steps": self.accepted_steps,
            "final_answer": self.final_answer,
            "hypotheses": list(self.hypotheses),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LabResult":
        return cls(
            str(d.get("question", "")), list(d.get("agenda", [])),
            [LabRound.from_dict(r) for r in d.get("rounds", [])],
            bool(d.get("converged", False)), int(d.get("accepted_steps", 0)),
            str(d.get("final_answer", "")),
            [h for h in (d.get("hypotheses") or []) if isinstance(h, dict)],
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


_RESUME_EVAL_SYSTEM = (
    "You are the Principal Investigator deciding, after ONE analysis step is re-run, which of the "
    "LATER steps must also be re-run because their result depends on the changed step's output. "
    "Answer with ONLY a JSON array of step numbers to re-run (e.g. [3,4]); [] if none."
)


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
        self._new_steps_added = 0
        # Run-scope context state: which accepted rounds have been folded into a digest, and the
        # digest block that stands in for them in every later brief.
        self._compacted_indices: set[int] = set()
        self._compacted_block: str = ""

    @property
    def guidance(self) -> str | None:
        """The resolved research-path steering (an explicit preset, or the skill the PI selected).
        Exposed so the gateway can persist it into run_state for an A2 resume that stays on the same
        protocol. Meaningful only after :meth:`run` has resolved it."""
        return self._guidance

    # -- orchestration (the explicit state machine; maps to a LangGraph) -------

    def run(
        self,
        question: str,
        on_event: EventFn | None = None,
        plan_review: "Callable[[str, Any], dict[str, Any]] | None" = None,
        should_cancel: "Callable[[], bool] | None" = None,
        pull_injections: "Callable[[], list[str]] | None" = None,
        resume: "ResumeState | None" = None,
        decision_review: "Callable[[Any], dict[str, Any]] | None" = None,
        should_compact: "Callable[[], bool] | None" = None,
    ) -> LabResult:
        emit: EventFn = on_event or (lambda _e: None)

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
                                     content_confidence=str(_dec.get("content_confidence") or ""))
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
        emit({"type": "skills_loaded",
              "skills": [{"key": s.key, "label": s.label, "tools": list(s.tools)} for s in self._skills]})

        # Axis A — execution mode (Virtual-Lab team vs single scientist). "auto" lets the PI
        # route; the frontend mode toggle sets "team"/"single" explicitly. In team mode the
        # PI forms a team and the team DESIGNS the approach in a meeting (each expert keeps an
        # independent context) — its synthesis augments the planning guidance below.
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
        kind, payload = self._pi_plan(question, emit, allow_clarify=plan_review is not None)

        # Plan mode (human-in-the-loop): the user does NOT text-edit the plan — they
        # review it and reply in natural language; that feedback goes BACK to the PI,
        # which re-drafts. The loop runs until the user approves an agenda or cancels.
        # ``plan_review(kind, payload)`` blocks for the user and returns a decision dict:
        #   {"action": "approve"}                 -> run the current agenda
        #   {"action": "revise", "feedback": ...} -> re-plan with the user's notes
        #   {"action": "cancel"}                  -> abort, run nothing
        if plan_review is not None:
            feedback_log: list[str] = []
            while True:
                decision = plan_review(kind, payload) or {"action": "cancel"}
                action = str(decision.get("action", "cancel")).lower()
                if action == "approve" and kind == "agenda":
                    break
                if action == "cancel":
                    emit({"type": "plan_cancelled"})
                    prior = payload if kind == "agenda" else []
                    return LabResult(question, prior, [], False, 0,
                                     "Run cancelled by the user during plan review — no tools were executed.")
                # revise / answering a clarify -> re-plan with the accumulated feedback
                fb = str(decision.get("feedback", "")).strip()
                if fb:
                    feedback_log.append(fb)
                kind, payload = self._pi_plan(
                    question, emit, feedback="\n".join(feedback_log),
                    prior_agenda=payload if kind == "agenda" else None,
                    allow_clarify=True)
            agenda = payload
            # NOTE: do NOT re-emit pi_agenda here — _pi_plan() already emitted the agenda
            # when it drafted (and re-emits on every revision), so re-emitting the approved
            # plan would post a second identical "📋 Plan ready" card in plan mode.
        else:
            agenda = payload if kind == "agenda" else [question]

        # PI↔Critic PLAN-TIME review (before any step runs) — the plan-time complement to the per-step
        # meetings: catch an incoherent plan (orphan de-novo clustering never reconciled with provided
        # labels, circular enrichment, a step nothing consumes) at its SOURCE, before any compute. Only
        # when a HUMAN is not curating the plan (plan_review is None) — a human reviewer's decision wins,
        # we do not auto-override it. No-op unless config.step_meetings.
        if plan_review is None:
            agenda = self._plan_review(question, agenda, emit)

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
                self._team, "interpretation", emit)
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
            ])
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
                                     user_notes=call_notes)   # Scientist node
            executed += 1
            verdict = self._critic(question, step, result, emit)                           # Critic node
            rounds.append(LabRound(len(rounds) + 1, step_idx + 1, step, specialist.name, result.to_dict(), verdict))

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
                self._team, "interpretation", emit)
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
            ])
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
                if not flagged and _CLUSTERING_STEP_RE.search((n.goal or "").replace("_", " ")):
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
            ])
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
            ])
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
                                     user_notes=step_notes, memory=mem_block)
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
            ])
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
        choice = str(decision.get("choice", "")).strip()
        low = choice.lower()
        if not choice or low == skip_opt.lower():
            return ("skip", "")
        if low == abort_opt.lower():
            return ("abort", "")
        return ("retry", choice)   # a chosen alternative (or free text) ⇒ retry WITH it as guidance

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
                self._team, "interpretation", emit)
        self._induce_skills(rounds, emit)      # end of run: keep what the lab figured out
        final_answer = self._synthesize(question, rounds, emit, team_interpretation=team_interpretation)
        emit({"type": "lab_done", "converged": converged, "accepted_steps": accepted_steps, "agenda": n_nodes})
        return LabResult(question, agenda_goals, rounds, converged, accepted_steps, final_answer,
                         self._ledger.to_list())

    # -- roles ----------------------------------------------------------------
    # Axis B pipeline routing lives in ``preset_pipelines.select_pipeline`` (called from ``run()``).

    # -- Axis A: Virtual-Lab team mode ----------------------------------------

    def _route_mode(self, question: str, emit: EventFn) -> str:
        """``mode="auto"`` only: the PI decides team vs single. Defaults to single."""
        raw = self._complete([
            {"role": "system", "content": _MODE_ROUTE_SYSTEM},
            {"role": "user", "content": f"Research question:\n{question}\n\nReply 'team' or 'single'."},
        ])
        mode = "team" if "team" in raw.strip().lower()[:24] else "single"
        emit({"type": "mode_selected", "mode": mode})
        return mode

    def _form_team(self, question: str, emit: EventFn) -> tuple[Specialist, ...]:
        """The PI dynamically assembles a complementary expert team for THIS question (each
        expert becomes an independent-context persona). Falls back to the fixed roster."""
        raw = self._complete([
            {"role": "system", "content": _TEAM_FORM_SYSTEM},
            {"role": "user", "content": f"Research question:\n{question}\n\nAssemble the team (JSON array)."},
        ])
        team = tuple(_parse_team(raw, self.config.team_size)) or self.config.specialists
        emit({"type": "team_formed", "members": [{"title": e.name, "expertise": e.persona} for e in team]})
        return team

    def _team_meeting(self, question: str, topic: str, experts: tuple[Specialist, ...],
                      kind: str, emit: EventFn) -> str:
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
            # Round > 0: experts collaborate by building on the PI's shared synthesis + the
            # Critic's score-driven feedback (the "challenge" is conditioned on the ACTUAL
            # critique, not a blanket disagree). They still never see each other's raw turns,
            # so the requests stay independent and batchable.
            collab = ("\n\nThe team's shared synthesis from the prior round — BUILD ON IT: extend, "
                      f"correct, or challenge it from your expertise; do not merely restate it.\n{synthesis}{feedback}"
                      if synthesis else "")
            msg_lists = [[
                {"role": "system", "content": (
                    f"You are {e.name}, an expert team member. {e.persona} Contribute concise, "
                    "specific, technically-grounded input on the meeting topic from YOUR expertise. "
                    "Be honest about uncertainty and disagree when the evidence warrants.")},
                {"role": "user", "content": f"Research question:\n{question}\n\nMeeting topic:\n{topic}{collab}\n\nYour input:"},
            ] for e in experts]
            texts = self._complete_concurrent(msg_lists)              # CONCURRENT → batched on the A100
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
        ])) or {}
        try:
            score = float(parsed.get("score", 0.5))
        except (TypeError, ValueError):
            score = 0.5
        critique = str(parsed.get("critique") or "").strip() or "(no specific critique)"
        emit({"type": "meeting_critic", "kind": kind, "round": rnd + 1, "score": score, "text": critique})
        return score, critique

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
                lines.append(
                    annotated
                    + ". Ground marker (run_de) and enrichment analysis on one of THESE columns — pass it "
                    f"as `groupby` (e.g. groupby=\"{anno[0]}\") — do NOT run DE/enrichment on de-novo "
                    "leiden cluster numbers, which are not interpretable on their own. Clustering + UMAP is "
                    "still fine for showing structure."
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

    def _pi_plan(
        self,
        question: str,
        emit: EventFn,
        *,
        feedback: str = "",
        prior_agenda: list[str] | None = None,
        allow_clarify: bool = False,
    ) -> tuple[str, Any]:
        """Draft (or re-draft) the plan. Returns ``("agenda", [steps])`` or, when
        ``allow_clarify`` and the request is genuinely ambiguous, ``("clarify",
        [{question, options}])``. ``feedback`` (the user's natural-language notes on the
        previous draft) + ``prior_agenda`` drive a revision instead of a fresh plan."""
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
        ])
        parsed = _parse_plan(raw, self.config.max_steps, allow_clarify)
        if parsed is None:
            parsed = ("agenda", [question])
        kind, payload = parsed
        if kind == "clarify":
            emit({"type": "pi_clarify", "questions": payload})
        else:
            #after PI generate agenda, check if the literature is inside
            payload = _ensure_literature_agenda(
                list(payload),
                question=question,
                guidance=self._guidance,
                feedback=feedback,
                max_steps=self.config.max_steps,
                has_literature_tool=any(t.name in ("deep_literature", "literature_search")
                                        for t in self.scientist.catalog),
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
            "enrichment) — its checkpoint under BIOAGENT_WORK already exists; reuse it. Re-running an "
            "upstream stage (especially with DIFFERENT parameters, e.g. a new mito threshold) "
            "overwrites its checkpoint and DESYNCHRONIZES the already-exported tables/figures from it, "
            "corrupting the run. Execute ONLY this step's work.\n" + "\n".join(lines)
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
        fallback = [_literature_query(question, step, rounds)]
        digest = _literature_findings_digest(rounds)
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
        except Exception:  # noqa: BLE001 - fall back to the raw step topic
            queries = [step]
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
        if ok and (answer_text or cites):
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
                   user_notes: str = "", memory: str = "") -> HarnessResult:
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
            "write a report or package/zip outputs (those are produced automatically)."
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
                  "via run_code (read checkpoints from BIOAGENT_WORK, write under BIOAGENT_ARTIFACTS). "
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
        return self.scientist.run(brief, self.ctx, on_event=emit, should_cancel=should_cancel,
                                  untrusted_text=untrusted_text)

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
        payload = {
            "research_question": question,
            "step": step,
            "scientist_status": result.status,
            "scientist_final_answer": result.final_answer,
            "tool_results": tool_results,
            "evidence": all_evidence,
            "tool_errors": result.errors,
        }
        raw = self._complete([
            {"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ])
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
        ])) or {}
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
        ])) or {}
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
        ])) or {}
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
        Side effect: the run's :class:`~bioagent.agents.hypotheses.HypothesisLedger` gains any new
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
            closed = self._ledger.resolve(str(item.get("hypothesis", "")),
                                          str(item.get("status", "")),
                                          str(item.get("evidence", "")))
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
            h = self._ledger.add(str(item.get("statement", "")), prediction=prediction, test=test,
                                 origin_step=step)
            if h is not None:
                emit({"type": "hypothesis_formed", "id": h.id, "statement": h.statement,
                      "prediction": h.prediction, "test": h.test, "origin_step": step,
                      "surprise": str(rev.get("surprise", ""))[:300]})

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
        to the original. No-op returning the draft unless ``config.step_meetings`` (and 2+ steps)."""
        if not self.config.step_meetings or len(agenda) < 2:
            return agenda
        profile = self._dataset_context()
        critic = _parse_verdict(self._complete([
            {"role": "system", "content": _PLAN_REVIEW_CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"research_question": question, "dataset_profile": profile, "draft_agenda": agenda})},
        ])) or {}
        issues = [str(x).strip() for x in (critic.get("issues") or []) if str(x).strip()]
        proposed = [str(s).strip() for s in (critic.get("revised_agenda") or []) if str(s).strip()]
        if not issues and (not proposed or proposed == agenda):
            return agenda   # Critic found nothing wrong → no PI round, plan unchanged
        pi = _parse_verdict(self._complete([
            {"role": "system", "content": _PLAN_REVIEW_PI_SYSTEM},
            {"role": "user", "content": json.dumps(
                {"research_question": question, "dataset_profile": profile, "draft_agenda": agenda,
                 "critic_issues": issues, "critic_revised_agenda": proposed})},
        ])) or {}
        final = [str(s).strip() for s in (pi.get("final_agenda") or []) if str(s).strip()]
        if not final or len(final) > self.config.max_steps:
            return agenda   # never worse than the draft
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
        report = self._complete([
            {"role": "system", "content": _SYNTH_SYSTEM},
            {"role": "user", "content": (
                f"Research question:\n{question}\n\n"
                f"Accepted step results (ground the report ONLY in these):\n{summary}\n"
                f"{methods_block}"
                f"{facts_block}"
                f"{vocab_block}"
                f"{ledger_block}"
                f"{interp}\n"
                "Write the final report now."
            )},
        ])
        # Guarantee layer: deterministically CORRECT any fabricated assembly / non-PASS claim that slipped
        # past the grounding prompt, and surface each correction as a diagnostic (not into the manuscript).
        facts_dict, _dists = _collect_facts(accepted)
        report, fact_issues = verify_report_facts(report, facts_dict)
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

    def _complete(self, messages: list[dict[str, Any]]) -> str:
        # Budget FIRST, then dispatch — the injected ``complete_fn`` (production wires one in
        # via the gateway's ``_lab_llm``) must receive the SAME trimmed prompt, otherwise the
        # single-shot budgeting is dead code on the only path production takes and the prompt
        # overflows the served window again. A trimmed prompt also leaves the model real
        # output room, so vLLM never computes a 0-token output budget ("requested 0 output
        # tokens"). The injected fn keeps its ``(messages) -> str`` contract.
        budgeted, max_tokens = self._budget_single_shot(messages)
        if self._complete_fn is not None:
            return self._complete_fn(budgeted)
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
