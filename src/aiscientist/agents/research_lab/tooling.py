"""Tools as the plan sees them: the code tools, known tool names, tools named in a step, plan checks."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Callable, Iterable
from ..tool_schema import TOOL_NAME, NAME_LEFT, NAME_RIGHT, REPORT_TOOL, PlanParameters, plan_parameters
from ..plan_doc import PlanDoc, PlanStep, call_settings, check_plan
from ..tool_names import canonical_tool_name
from ..skills import Skill, get_skill, refresh_skills
from ..research_harness import HarnessContext, HarnessTool

from .models import CodeExecutor


def _env_arg(args: dict[str, Any]) -> str:
    env = args.get("environment") or ""
    return ",".join(str(e) for e in env) if isinstance(env, list) else str(env).strip()


def make_run_code_tool(executor: CodeExecutor | None,
                       software_note: "Callable[[str], list[dict]] | None" = None,
                       environments: "Callable[[str], list] | None" = None) -> HarnessTool:
    """The hybrid CodeAct tool: the Scientist can write-and-run Python when the
    curated tools don't cover a step. Execution is delegated to ``executor`` (a
    sandboxed runtime on the server); without one it returns a clear 'not enabled'
    rather than running arbitrary code in-process.

    ``software_note(output)``: for a failed snippet, the programs or R packages it lacked and the
    HPC3 module that provides each (``HpcShell.missing_software_note``), so the model runs the
    module with run_shell instead of rewriting the method (docs/environments/README.md §4).

    ``environments(ids)``: the published lab environments ``environment=`` names
    (``HpcShell.resolve_environments``), mounted read-only into the snippet's job with their bin/ first
    on PATH (``hpc/environments.py``). None when lab environments are off: the parameter is then refused."""

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
        from .. import provenance as _prov

        seed = _prov.resolve_seed() if _prov.seeding_enabled() else None
        to_run = (_prov.seed_preamble(seed) + code) if seed is not None else code
        started_at, t0 = _prov.now_iso(), _prov.perf_now()
        from ..skills_registry import active_skill, execute
        try:
            skill = active_skill(ctx, str(args.get("skill") or ""))
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        refs: list = []
        wanted = _env_arg(args)
        if wanted:
            if environments is None:
                return {"status": "error", "error": "Lab environments are not enabled in this session."}
            try:
                refs = environments(wanted)
            except Exception as exc:  # noqa: BLE001 - the model reads the reason
                return {"status": "error", "error": str(exc)}
            if any(getattr(r, "kind", "") == "image" for r in refs):
                return {"status": "error", "error": ("A container-image environment runs with "
                                                     "run_in_environment(environment=..., command=...).")}
        from ...hpc.environments import using
        with using(refs):
            result = execute(skill, ctx, executor, lambda: executor(to_run), "run_code", code=code)
        duration_ms = int((_prov.perf_now() - t0) * 1000)
        if isinstance(result, dict):
            try:
                result["provenance"] = _prov.build_code_provenance(
                    code=code, seed=seed, executor=executor, ctx=ctx,
                    result=result, started_at=started_at, duration_ms=duration_ms,
                ).to_dict()
            except Exception:  # provenance must never break a run
                pass
            # A max/mean printed from the first N rows reads like the file's own (runs 74873590ca7b,
            # 7f2955e8e834: ".raw max" of 300 nuclei). Say so on the result, before the answer is written.
            try:
                from ..sampled_stats import annotate
                annotate(result, code)
            except Exception:  # a reading aid must never break a run
                pass
            if software_note is not None and str(result.get("status") or "") not in ("ok", "success"):
                try:
                    text = "\n".join(str(result.get(k) or "") for k in ("error", "stderr", "stdout"))
                    note = software_note(text)
                    if note:
                        result["missing_software"] = note
                except Exception:  # a reading aid must never break a run
                    pass
        return result

    from ..sandbox import build_run_code_context

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
        "A program this Python image lacks (an R script, a command-line tool such as bedtools or STAR) "
        "may be one of HPC3's software modules: call find_software('<name>') and run it with run_shell "
        "(`module load ...`), on files you write to AISCIENTIST_WORK. Software no module has can be built "
        "as a lab environment with request_environment (when that tool is offered); pass its id as "
        "`environment` here and its programs (Rscript, mosdepth, ...) are on PATH for subprocess calls. "
        "STRICT: use this ONLY for analysis (compute figures into figures/, tables into tables/). Do "
        "NOT write or render a report or document (no .docx/.pdf/.html, no python-docx/reportlab/"
        "pandoc), and do NOT zip/package/export the outputs: the downloadable bundle of everything "
        "under AISCIENTIST_ARTIFACTS is created AUTOMATICALLY when the run finishes, and a written "
        "report comes only from the plan's own `write_report` step. Building your own report or "
        "archive is duplicate work and will be discarded."
    )
    # Append the LIVE execution-environment context (real paths, obs schema, CWD + memory caveats)
    # so the model stops guessing column names / relative paths / working directory — the largest,
    # most repetitive class of run_code failures. Static for the run → computed once here.
    description += build_run_code_context(executor)

    return HarnessTool(
        name="run_code",
        description=description,
        parameters={"type": "object", "properties": {"code": {"type": "string"},
                    "skill": {"type": "string", "description": "Skill being applied; defaults to the last skill read."},
                    "environment": {"type": "string", "description": (
                        "Optional lab environment id(s) (request_environment / find_software), comma-separated: "
                        "mounted read-only, programs first on PATH.")}},
                    "required": ["code"]},
        executor=_exec,
        reads_private_data=False, category="codeact",
    )


def make_run_in_environment_tool(executor: Any,
                                 environments: "Callable[[str], list] | None" = None) -> "HarnessTool | None":
    """``run_in_environment`` — run a skill's shell commands inside the environment its SKILL.md
    declares (``image:``), as a Slurm job on HPC3. None when ``executor`` cannot run images (no HPC
    session), so a local run's roster does not list a tool that cannot work.

    This is what lets a skill that needs its own software stack (an R/Snakemake pipeline such as
    CellQC) work with no wrapper tool: the platform builds its declared image once for the lab, and the agent follows the skill's commands here. Only a skill's DECLARED image can be used —
    the agent never names an image itself, so what runs is what the skill's author pinned.

    ``environments(ids)`` (``HpcShell.resolve_environments``): with ``environment=`` instead of a
    skill, the command runs in a lab environment the user approved (``request_environment``): a
    container-image one in its image, a package one in run_code's image with the environment mounted
    read-only and first on PATH."""
    runner = getattr(executor, "run_in_image", None)
    if not callable(runner):
        return None

    def _exec_env(wanted_env: str, command: str) -> dict[str, Any]:
        import hashlib
        if environments is None:
            return {"status": "error", "error": "Lab environments are not enabled in this session."}
        if not command:
            return {"status": "error", "error": "no command provided"}
        try:
            refs = environments(wanted_env)
        except Exception as exc:  # noqa: BLE001 - the model reads the reason
            return {"status": "error", "error": str(exc)}
        images = [r for r in refs if getattr(r, "kind", "") == "image"]
        if images and len(refs) > 1:
            return {"status": "error", "error": "A container-image environment cannot be combined with others."}
        workdir = "env/" + re.sub(r"[^\w.\-]", "_", "+".join(r.id for r in refs))[:120]
        if images:
            result = runner(command, images[0].image, workdir=workdir)
        else:
            from ...hpc.environments import using
            run_command = getattr(executor, "run_command", None)
            if not callable(run_command):
                return {"status": "not_enabled", "note": "this session cannot run batch jobs with an environment"}
            with using(refs):
                result = run_command(command, workdir=workdir)
        if isinstance(result, dict):
            result["environments"] = [r.id for r in refs]
            result["provenance"] = {"environments": [r.id for r in refs],
                                    "command_sha256": hashlib.sha256(command.encode()).hexdigest()[:16]}
        return result

    def _exec(args: dict[str, Any], ctx: HarnessContext, *,
              _mcp_server: Any = None, _resources: tuple | None = None,
              _tool_cache: bool = False, _read_only: tuple = (),
              _mcp_model: Any = None) -> dict[str, Any]:
        import hashlib
        from pathlib import Path as _Path
        from ..skills import SKILLS, get_skill, refresh_skills
        wanted_env = _env_arg(args)
        command = str(args.get("command") or "").strip()
        if wanted_env and not str(args.get("skill") or "").strip():
            return _exec_env(wanted_env, command)
        refresh_skills()
        wanted = str(args.get("skill") or "").strip()
        with_env = sorted(s.name for s in list(SKILLS.values()) if s.image)
        skill = get_skill(wanted) or next(
            (s for s in list(SKILLS.values()) if s.name.lower() == wanted.lower()), None)
        if skill is None:
            return {"status": "error", "error": f"no skill named {wanted!r}. Skills that declare an "
                                                f"environment: {with_env or 'none'}"}
        from ..mcp_bridge import protected_skill_tools
        from ..atlas_expression import SKILL as ATLAS_SKILL, direct_call_refusal
        protected = protected_skill_tools(skill.name)
        if protected and _mcp_server is None:
            message = (direct_call_refusal() if skill.name == ATLAS_SKILL
                       else "Use the MCP bridge tools: " + ", ".join(protected))
            return {"status": "error", "error": message}
        if _mcp_server is not None and (skill.name != _mcp_server.skill or skill.image != _mcp_server.image):
            return {"status": "error", "error": "MCP pinned image differs from the installed skill image"}
        if not skill.image:
            why = f" ({skill.env_problem})" if skill.env_problem else ""
            return {"status": "error",
                    "error": f"skill {skill.name!r} declares no environment{why}: use run_code or "
                             f"the typed tools. Skills that declare one: {with_env or 'none'}"}
        if not command:
            return {"status": "error", "error": "no command provided"}
        folder = _Path(skill.folder) if skill.folder else None
        files = {rel: str(folder / rel) for rel in skill.files
                 if folder is not None and (folder / rel).is_file()}
        workdir = "env/" + re.sub(r"[^\w.\-]", "_", skill.name)
        from ..skills_registry import execute
        # Platform callers select resources, the user's tool cache and extra read-only folders; none
        # is in the schema, so the model cannot set them.
        cpus, mem_gb, time_limit = _resources or (skill.cpus, skill.mem_gb, skill.time_limit)
        cache = {"tool_cache": True} if _tool_cache else {}
        if _read_only:                 # an MCP adapter's deployment-named folders (retigene's index)
            cache["read_only"] = tuple(_read_only)
        if _mcp_server is not None:
            # Only the pinned registry selects placement/reference mounts. These options
            # are absent from the planner's tool schema and cannot come from its arguments.
            from ..mcp_analysis import setting_paths
            cache.update(gpu=_mcp_server.runs_on == "gpu:scgpt",
                         extra_ro_binds=setting_paths(_mcp_server))
        from ..mcp_model import model_files, run_with_model
        platform_files = model_files(_mcp_model)
        if set(platform_files) & set(skill.files):
            return {"status": "error", "error": "skill file conflicts with the platform model relay"}
        def run():
            if skill.registry_release:
                import tempfile
                with tempfile.TemporaryDirectory(prefix="aiscientist-skill-run-") as temp:
                    staged = {}
                    for rel, text in skill.files.items():
                        path = _Path(temp) / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding="utf-8")
                        staged[rel] = str(path)
                    staged.update(platform_files)
                    return runner(command, skill.image, workdir=workdir, cpus=cpus,
                                  mem_gb=mem_gb, time_limit=time_limit, files=staged, **cache)
            return runner(command, skill.image, workdir=workdir, cpus=cpus,
                          mem_gb=mem_gb, time_limit=time_limit, files={**files, **platform_files}, **cache)
        if _mcp_model is not None and _mcp_server is None:
            return {"status": "error", "error": "model credentials require a pinned MCP call"}
        result = execute(skill, ctx, executor,
                         lambda: run_with_model(_mcp_model, executor, run),
                         "run_in_environment", code=command)
        if isinstance(result, dict):
            result["skill"] = skill.name
            result["provenance"] = {
                "skill": skill.name, "image": skill.image,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest()[:16]}
        return result

    from ..skills import SKILLS, refresh_skills
    refresh_skills()
    declared = [f"  - {s.name}: {s.image}" for s in sorted(SKILLS.values(), key=lambda s: s.name)
                if s.image]
    description = (
        "Run SHELL commands inside the software environment a SKILL declares (the `image:` in its "
        "SKILL.md), as a CPU Slurm job on HPC3. Use it to follow a skill whose steps are "
        "command-line programs that no tool in this catalog runs (e.g. an R/Snakemake pipeline): "
        "read the skill first (read_skill_reference), then send its commands here, adapted to this "
        "run's paths. The environment already has the software: do NOT install anything, and skip "
        "the skill's own installation steps. Inside the job: the working directory, also "
        "$AISCIENTIST_ENV_DIR, is this skill's own directory and persists across calls in this run "
        "(put a pipeline's intermediate output there); $AISCIENTIST_DATASET_ROOT is the bound "
        "dataset folder, READ-ONLY (link or copy what a pipeline must write next to); "
        "$AISCIENTIST_DATASET is the primary file in it; $AISCIENTIST_WORK holds the run's "
        "checkpoints (the analysis tools read adata_qc.h5ad there after QC); write deliverables "
        "under $AISCIENTIST_ARTIFACTS (tables/, figures/, or a folder named after the pipeline); "
        "$AISCIENTIST_SKILL_DIR has the skill's bundled files; $AISCIENTIST_JOB_CPUS is the CPU "
        "count. CPUs, memory and time come from the skill. The first use of an environment builds "
        "it once for the whole lab, which can take 10-30 minutes. Returns the exit code, the tails "
        "of stdout/stderr, and the files written under $AISCIENTIST_ARTIFACTS.")
    if environments is not None:
        description += (
            " Or pass `environment` (an id from request_environment or find_software) instead of `skill` to "
            "run the commands as a batch job in that lab environment (its programs first on PATH; 8 CPUs, the "
            "run_code memory cap): for a program that needs more time or memory than run_shell gives.")
    if declared:
        description += "\nSkills that declare an environment:\n" + "\n".join(declared)
    properties = {
        "skill": {"type": "string", "description": "the skill whose environment to use"},
        "command": {"type": "string", "description": "bash to run in it (several lines are fine)"}}
    if environments is not None:
        properties["environment"] = {"type": "string", "description": (
            "a lab environment id, instead of a skill")}
    return HarnessTool(
        name="run_in_environment",
        description=description,
        parameters={"type": "object", "properties": properties,
                    "required": ["command"] if environments is not None else ["skill", "command"]},
        executor=_exec,
        reads_private_data=False, category="codeact",
    )


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
_BACKTICK_NAME_RE = re.compile(r"`(" + TOOL_NAME + r")(?:\s*\([^`]*\))?`")
_ANY_TOOL_CALL_RE = re.compile(NAME_LEFT + "(" + TOOL_NAME + ")" + NAME_RIGHT + r"\s*\(([^)]*)\)")


@lru_cache(maxsize=1)
def _known_tool_names() -> frozenset[str]:
    """Every tool name the Scientist catalog actually serves.

    Deferred + cached: the registry pulls the whole tool tree, and this is consulted per step.
    An empty set on failure degrades to the ``run_`` prefix test below, i.e. exactly the previous
    behaviour — a classifier must never be the reason a run cannot start.
    """
    try:
        from ..registry import build_scientist_catalog
        # run_in_environment is only built for a live HPC3 session, so the bare catalog lacks it;
        # a step that names it must still be recognised as that tool's step.
        from ..mcp_bridge import configured_tool_names
        # The gateway owns the report step; it is not a Scientist catalog tool.
        return (frozenset(t.name for t in build_scientist_catalog())
                | {"run_in_environment", REPORT_TOOL} | configured_tool_names())
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
_KWARG_RE = re.compile(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([A-Za-z0-9_.\-]+))""")


def _tool_schemas(catalog: "list[Any]") -> "dict[str, dict[str, Any]]":
    """``{tool name: JSON-schema properties}`` for every tool in the catalog."""
    out: dict[str, dict[str, Any]] = {REPORT_TOOL: {}}
    for tool in catalog or ():
        name = getattr(tool, "name", None)
        if name:
            out[str(name)] = plan_parameters(getattr(tool, "parameters", None) or {})
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
    if not catalog:
        return list(agenda), []
    schemas = _tool_schemas(catalog)
    findings: list[dict[str, Any]] = []
    fixed: list[str] = []
    for step in agenda:
        text = str(step)
        # 1. Names. Correct the unambiguous ones in place; report the rest.
        for raw in set(_BACKTICK_NAME_RE.findall(text)) | {m.group(1) for m in _ANY_TOOL_CALL_RE.finditer(text)}:
            if raw in schemas or raw == REPORT_TOOL or get_skill(raw) is not None:
                continue                      # a tool, the report step, or a skill applied with run_code
            if not raw.startswith("run_"):
                continue                      # retain the legacy typo policy, using complete names
            target = _TOOL_NAME_FIXES.get(raw.lower())
            if target and target in schemas:
                text = re.sub(NAME_LEFT + re.escape(raw) + NAME_RIGHT, target, text)
                findings.append({"kind": "renamed", "tool": raw, "detail": target})
            else:
                findings.append({"kind": "unknown_tool", "tool": raw, "detail": ""})
        # 2. Arguments — checked against the (possibly corrected) name's own schema.
        for call in _ANY_TOOL_CALL_RE.finditer(text):
            name, args = call.group(1), call.group(2)
            props = schemas.get(name)
            if props is None:
                continue                      # unknown tools are handled above
            if isinstance(props, PlanParameters):
                doc = PlanDoc(request="", steps=(PlanStep(id="check", text=text, tool=name,
                              settings=call_settings(text, name, json_literals=True)),))
                _, errors = check_plan(doc, {name: props})
                findings.extend({"kind": "bad_value", "tool": name, "detail": f["detail"]} for f in errors)
                continue
            if not props or not name.startswith("run_"):
                continue                      # preserve the legacy non-MCP prose check's scope
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


def _primary_tool(step: str) -> "str | None":
    """The tool a step itself runs: the FIRST catalog tool it names. Later names are usually what it
    reads or feeds ("…ranked by Wilcoxon z, from `run_de`'s ranking files"), not what it runs."""
    text = step or ""
    declared = _declared_tools(text)
    first: "tuple[int, str] | None" = None
    for rx in (_BACKTICK_NAME_RE, _ANY_TOOL_CALL_RE):
        for m in rx.finditer(text):
            if m.group(1) in declared and (first is None or m.start() < first[0]):
                first = (m.start(), m.group(1))
    return first[1] if first else None


def _names_tool(agenda: "list[str]", tools: "tuple[str, ...]") -> bool:
    """Does any step name one of ``tools``?  (Steps quote tools in backticks.)"""
    return any(canonical_tool_name(name) in tools for step in agenda for name in _declared_tools(step))


def retick_tools(steps: "list[str]", names: "Iterable[str]") -> "list[str]":
    """``steps`` with the backticks put back around tool names written bare.

    Every consumer of a plan finds a step's tool by its backticks: the scope filter, the tooling
    check, the plan review's contracts, the DAG's node kinds. The PI's finalising turn rewrites
    steps and sometimes writes ``run run_scanpy_qc`` without them (measured 2026-10-12: one of
    fifteen reviewed plans, all three of its steps), after which the plan names no tool at all as
    far as the code can tell. Only names with an underscore or hyphen are restored, so no ordinary word is
    ever quoted; a longer name is never split (``run_de`` inside ``run_depth_matched_de``)."""
    wanted = sorted({n for n in names if "_" in n or "-" in n}, key=len, reverse=True)
    if not wanted:
        return list(steps)
    bare = re.compile(r"(?<![`\w-])(" + "|".join(re.escape(n) for n in wanted) + r")(?![`\w-])")
    return [bare.sub(r"`\1`", step) for step in steps]


def plan_tool_contracts(agenda: "list[str]", catalog: "list[Any]") -> "dict[str, str]":
    """``{tool name: what the model is told it does}`` for every catalog tool the agenda names.

    The plan review judges redundancy ("this step recomputes what a tool already returns") and
    fidelity ("the step describes the tool wrongly"), and both are questions about the tool, not
    the plan. Only the tools the plan names are sent: they are the ones a step can duplicate or
    misdescribe, and the whole catalog would bury them."""
    text = "\n".join(agenda)
    return {t.name: str(getattr(t, "description", "") or "")
            for t in catalog
            if t.name != "finish" and re.search(r"`%s`" % re.escape(t.name), text)}


def _describe_param(name: str, spec: "dict[str, Any]") -> str:
    """One tool parameter as the binder reads it: name, type, default, closed set, meaning."""
    bits = [str(spec.get("type") or "any")]
    if "default" in spec:
        bits.append(f"default {json.dumps(spec['default'], default=str)}")
    if spec.get("enum"):
        bits.append("one of " + "/".join(map(str, spec["enum"])))
    meaning = " ".join(str(spec.get("description") or "").split())[:140]
    return f"{name} ({', '.join(bits)})" + (f": {meaning}" if meaning else "")


def _tool_runs_on(tool: str) -> str:
    """Where a tool's manifest says it runs (``hpc:analysis``, ``inprocess``, …); "" if unknown."""
    try:
        from ...tools import catalog as tool_catalog
        found = tool_catalog.manifest(tool)
    except Exception:  # noqa: BLE001 - a label on the plan card, never a reason to fail a plan
        return ""
    return found.runs_on if found is not None else ""


def _tool_skill(tool: str) -> "Skill | None":
    """The skill a tool's manifest names (``skill:`` in TOOL.md, e.g. run_cellqc's CellQC skill), if it
    is in the library; ``None`` for a tool that names none."""
    try:
        from ...tools import catalog as tool_catalog
        found = tool_catalog.manifest(tool)
        if found is not None and found.skill:
            return get_skill(found.skill)
        from ..mcp_bridge import configured_tool_skills
        skill_name = configured_tool_skills().get(tool)
        if skill_name:
            refresh_skills()
        return get_skill(skill_name) if skill_name else None
    except Exception:  # noqa: BLE001 - an unreadable manifest names no skill
        return None


def _tool_ends_step(tool: str) -> bool:
    """Whether a tool's manifest says one successful call is its whole step (``ends_step``)."""
    try:
        from ...tools import catalog as tool_catalog
        found = tool_catalog.manifest(tool)
    except Exception:  # noqa: BLE001 - without the manifest the step runs as any other step
        return False
    return bool(found is not None and found.ends_step)


def _planner_tool_description(tool: HarnessTool) -> str:
    """The tool's description plus the schema defaults its manifest asks the planner to see."""
    from ...tools.catalog import manifest
    found = manifest(tool.name)
    props = (tool.parameters or {}).get("properties", {})
    defaults = [_describe_param(n, props[n]) for n in (found.planner_defaults if found else ())
                if n in props and "default" in props[n]]
    return tool.description + (" Declared defaults: " + "; ".join(defaults) if defaults else "")
