"""The single source of truth for the Scientist's toolset.

Two kinds of tool meet here:

* **Domain tools** live in ``aiscientist.tools``, one folder per tool, each with a ``TOOL.md`` whose
  front matter is its manifest. ``tools.catalog`` discovers them, and nothing in this module names
  one: a new tool is a new folder. Its manifest says where it runs (``runs_on``), and that is all
  the routing below needs.
* **Platform tools** expose the platform's own machinery, so they are assembled here: ``finish`` and
  the smoke QC/DE fallbacks (``research_harness.default_catalog``), CodeAct ``run_code`` (the per-run
  sandbox), the HPC3 shell family (``hpc.shell``) and ``describe_environment``.

    build_scientist_catalog(code_executor=..., analysis_executor=..., ...) -> list[HarnessTool]

Each HarnessTool self-describes (``category`` / ``requires`` / ``reads_private_data``), so the
System page renders straight off this list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..tools import catalog as tool_catalog
from .tool_skills import attach_skill

if TYPE_CHECKING:
    from .research_harness import HarnessTool


# Lightweight smoke tools that the REAL scanpy analysis line supersedes. They exist as a
# fallback for hosts WITHOUT scanpy; when scanpy is installed, keeping them would force the
# PI/Scientist to choose between a real tool and a placeholder twin for the same step, which
# muddies planning and weakens results — so we drop the twins whenever the real tool can run.
_SUPERSEDED_WHEN_SCANPY = ("run_qc", "run_de_markers")

# Which tools go to which HPC3 job line now comes from each tool's manifest (``runs_on``). These
# names survive as views of the manifests because tests and docs refer to them.
#
# Why the manifest and not a list here: EVERY tool that reads or writes the run's ``work/``
# checkpoints must run where those checkpoints live. The hand-kept list for the analysis line once
# held only four tools, so ``run_composition`` / ``run_pseudobulk_de`` / ``run_gsea_prerank`` /
# ``run_doublet_detection`` / ``run_marker_annotation`` / ``run_integration`` executed IN-PROCESS on
# the eyeserver, whose local work/ is empty when QC ran as a Slurm job, and ``run_composition``
# failed three rounds running with "no analysis checkpoint found" while a 647 MB adata_qc.h5ad sat on
# dfs3b. A tool now cannot be added without saying where it runs.
#
# ``deep_literature`` is offloaded not for compute but because the PubMedBERT index lives on /dfs3b,
# which the eyeserver gateway cannot read in place.
_HPC_ANALYSIS_TOOLS = tuple(tool_catalog.names(tool_catalog.runs_on("hpc:analysis")))
_HPC_VARIANT_TOOLS = tuple(tool_catalog.names(tool_catalog.runs_on("hpc:variant")))
_HPC_PHENOTYPE_TOOLS = tuple(tool_catalog.names(tool_catalog.runs_on("hpc:phenotype")))
_HPC_LITERATURE_TOOLS = tuple(tool_catalog.names(tool_catalog.runs_on("hpc:literature")))


def _route_to_executor(tool: "HarnessTool", executor: Any, names: tuple[str, ...]) -> "HarnessTool":
    """If ``tool.name`` is in ``names``, return a copy whose executor delegates to the HPC
    ``executor`` (which itself falls back in-process). Other tools pass through unchanged."""
    if tool.name not in names:
        return tool
    import dataclasses

    def _exec(args: dict, ctx: Any, _ex=executor, _name=tool.name) -> dict:
        return _ex.run_tool(_name, args, ctx)

    # The job on HPC3 runs the tool's own code, so whatever introspects this executor
    # (read_tool_source, describe_environment's source index) must land there, not on this
    # three-line dispatcher. Run 78a707cd79e9's ORA step read `_exec` twice, could not fetch
    # `run_enrichment` as a symbol of registry.py, and ended without ever calling the tool.
    _exec.__wrapped__ = tool.executor        # type: ignore[attr-defined]
    try:
        return dataclasses.replace(tool, executor=_exec)
    except TypeError:                       # not a (replaceable) dataclass — mutate in place
        tool.executor = _exec
        return tool


def _router(executors: dict[str, Any]) -> Callable[[Any, "HarnessTool"], "HarnessTool"]:
    """Route each tool whose manifest says ``runs_on: hpc:<line>`` to that line's executor, when
    the gateway built one. Each executor falls back in-process on its own (and ``run_lirical``
    reports ``not_installed``), so an unwired line keeps the in-process behaviour."""
    def route(manifest: Any, tool: "HarnessTool") -> "HarnessTool":
        executor = executors.get(manifest.runs_on)
        routed = _route_to_executor(tool, executor, (tool.name,)) if executor is not None else tool
        # A tool that runs a registry skill's method gets that skill with every call, on HPC3 or
        # in-process alike (agents/tool_skills.py).
        return attach_skill(manifest, routed)
    return route


def build_scientist_catalog(code_executor: Any = None, scgpt_runner: Any = None,
                            analysis_executor: Any = None,
                            variant_executor: Any = None,
                            phenotype_executor: Any = None,
                            literature_executor: Any = None,
                            hpc_shell: Any = None) -> list["HarnessTool"]:
    """Assemble the full ordered Scientist catalog. When scanpy is available, the lightweight
    smoke QC/DE tools are dropped in favour of the real scanpy analysis line (they remain only as a
    no-scanpy fallback).

    The four executors are the HPC3 job lines (``SlurmAnalysisExecutor``s) the gateway built for
    this session: ``analysis_executor`` runs the scanpy line in analysis.sif, ``variant_executor``
    offline VEP (the REST path stays the in-process fallback for small VCFs), ``phenotype_executor``
    LIRICAL, ``literature_executor`` PaperQA. A tool goes to the one its manifest names.

    A manifest that says ``research: false`` is left out: that tool runs only from its own console
    line (the corpus scan is an hour-long job the user starts, with a workspace of its own), so
    the PI cannot plan it and the Scientist cannot call it. The line reaches it by name through
    its executor, not through this catalog.

    ``scgpt_runner`` is the gateway-injected remote executor for the scGPT GPU batch job; the
    ``scgpt_annotate`` tool is always present (it self-reports not-enabled without one, so the System
    page can list it), like ``run_code`` without a sandbox.

    ``diagnose_disease`` composes ``run_lirical`` and ``deep_literature``; the catalog binds it to
    their ROUTED executors, after routing, so the adjudicated differential uses HPC3 exactly when the
    two tools it wraps do."""
    import importlib.util

    from .research_harness import default_catalog
    from .research_lab import make_run_code_tool, make_run_in_environment_tool

    executors = {"hpc:analysis": analysis_executor, "hpc:variant": variant_executor,
                 "hpc:phenotype": phenotype_executor, "hpc:literature": literature_executor}
    catalog: list[HarnessTool] = list(default_catalog())                  # finish + smoke QC/DE
    catalog += tool_catalog.build_tools({"scgpt_runner": scgpt_runner}, where=lambda m: m.research,
                                        route=_router(executors))
    # A failed snippet that lacked a program gets the HPC3 module that provides it (hpc/modules.py).
    note = ((lambda text: hpc_shell.missing_software_note(text, "run_code")) if hpc_shell is not None else None)
    catalog.append(make_run_code_tool(code_executor, software_note=note))   # CodeAct (needs the sandbox)
    # A skill's declared environment (SKILL.md ``image:``) — only when the executor can run images
    # on HPC3, so a local run's roster stays honest.
    env_tool = make_run_in_environment_tool(code_executor)
    if env_tool is not None:
        catalog.append(env_tool)
    if importlib.util.find_spec("scanpy") is not None:
        catalog = [t for t in catalog if t.name not in _SUPERSEDED_WHEN_SCANPY]
    # The HPC3 filesystem/shell line — appended LAST so it never displaces a typed tool in the
    # model's reading of the roster. These exist because the Scientist previously had NO way to
    # look at the cluster's filesystem: the only escape hatch was run_code, a Slurm batch job, so
    # `ls` cost minutes of queue and the model tended to guess at paths instead. Empty (not
    # broken tools) when no HPC session is bound, so a local run's roster stays honest.
    from ..hpc.shell import hpc_shell_catalog
    catalog.extend(hpc_shell_catalog(hpc_shell))
    # "What does this deployment HAVE, and where is it?" Nothing answered that, and the cost was
    # real: a step that had to verify scGPT's model directory could not read it, and a step that
    # needed gene sets went to the network while three .gmt libraries sat in the directory the
    # enrichment tools read from. Progressive disclosure, like the skill tools — the manifest is
    # fetched when a step needs it, never prepended to every turn.
    catalog.append(_make_describe_environment_tool(hpc_shell))
    return catalog


# A tool's declared requirement (its PyPI name) -> the module it imports as, where they differ.
_IMPORT_NAMES = {"paper-qa": "paperqa"}


def _importable(requirement: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(_IMPORT_NAMES.get(requirement, requirement)) is not None
    except (ImportError, ValueError):
        return False


def session_catalog(catalog: list["HarnessTool"], literature_executor: Any = None,
                    served_model: bool = True) -> list["HarnessTool"]:
    """The roster ONE SESSION is offered: ``catalog`` without the HPC3 literature tools it cannot run.

    ``deep_literature`` runs as a job in paperqa.sif on HPC3 and calls the session's served model
    from there, so the gateway builds its executor only for a session with a GPU allocation. Without
    one the tool is left unrouted and runs in the gateway process. That works on a development
    machine with ``paper-qa`` installed and nowhere else: the prod gateway has neither the package
    nor the /dfs3b index. Prod run BioAdmin/1e887ed2bf8e (2026-10-12) reasoned on the user's own API
    key, so it had no GPU: the PI planned a ``deep_literature`` step, the plan review rewrote it
    around that tool's contract, and the call answered ``dependency_missing`` in under a second.

    A literature executor can exist without a served model (a session on the user's own API key):
    ``search_corpus`` runs there, because its job calls no model. The tools whose manifest says
    ``served_model`` are offered only when ``served_model`` is true, so the rule is data, not a list
    of names.

    Only the session's roster changes. ``build_scientist_catalog()`` keeps listing the tool, because
    the System page and the step classifier (``research_lab._known_tool_names``) read that catalog as
    the tools that exist, and ``diagnose_disease`` stays bound to the executor it was built with."""
    def offered(t: "HarnessTool") -> bool:
        if t.name not in _HPC_LITERATURE_TOOLS:
            return True
        if literature_executor is not None:
            m = tool_catalog.manifest(t.name)
            return served_model or not (m and m.served_model)
        return all(_importable(r) for r in t.requires)

    return [t for t in catalog if offered(t)]


def _make_describe_environment_tool(hpc_shell: Any = None) -> "HarnessTool":
    """``describe_environment`` — the assets, the session's filesystem permissions, and where each
    tool is implemented. Derived from live code + settings, so it cannot drift."""
    from .research_harness import HarnessTool

    def _run(args: dict[str, Any], _ctx: Any) -> dict[str, Any]:
        from ..gateway.environment import environment_manifest, render_for_agent
        section = str(args.get("section") or "all").lower()
        if section not in ("all", "assets", "tools", "roots"):
            section = "all"
        ws = getattr(hpc_shell, "workspace", None)
        manifest = environment_manifest(
            read_roots=tuple(getattr(ws, "read_roots", ()) or ()),
            write_roots=tuple(getattr(ws, "write_roots", ()) or ()))
        return {"status": "ok", "section": section,
                "environment": render_for_agent(manifest, section)}

    return HarnessTool(
        name="describe_environment",
        description=(
            "What this deployment HAS and where it is: the container images, model weights "
            "(scGPT, vision review), gene-set (.gmt) libraries, reference data and package cache "
            "— each with its PATH and whether this session may read it — plus the session's "
            "readable/writable roots and the source location of every tool. Call it before "
            "concluding that an asset is unavailable, before downloading anything that might "
            "already be on disk, and when a step must verify a model or reference it has not "
            "been told the location of. `section`: all | assets | tools | roots."),
        parameters={"type": "object",
                    "properties": {"section": {"type": "string",
                                               "enum": ["all", "assets", "tools", "roots"]}}},
        executor=_run, category="backend")


# The FAST-PATH toolset (see ``agents/quick_chat.py``): the tools whose manifest says ``chat: true``.
# Being on that list is a product decision taken per tool, in its manifest, rather than a filter
# that would silently admit every future tool that happened to match.
#
# The bar: runs IN-PROCESS on the gateway, returns in seconds, needs no run workspace, no Slurm
# job, and no dataset binding. That excludes, on purpose:
#   * ``run_code`` / the scanpy + variant + phenotype lines — minutes-to-hours of HPC3 compute, and
#     a chat turn has no run bundle to write results into. Analysis belongs on the research path.
#   * ``make_schematic`` — it renders a figure ARTIFACT into ``<run>/artifacts/figures``, which a
#     chat turn does not have. Inline diagrams in chat go the other way: the model writes a
#     ```mermaid fence and the browser renders it (Feature B). The two are complementary, and the
#     schematic tool is untouched on the research path.
# ``deep_literature`` is the one chat tool that runs on HPC3 (its index is on /dfs3b).
def build_quickchat_catalog(literature_executor: Any = None, served_model: bool = True) -> list["HarnessTool"]:
    """The tools the answer-first chat loop may call: the ``chat: true`` manifests, built from the
    SAME factories as the research catalog (so a tool is never defined twice and cannot drift
    between paths).

    A chat tool that runs on an HPC3 line is routed to that line's executor; with no executor it
    is DROPPED, never run in-process. ``deep_literature`` reads the /dfs3b index the eyeserver
    cannot see, so running it on the gateway would fail to find the index."""
    executors = {"hpc:literature": literature_executor}

    def wanted(m: Any) -> bool:
        if m.served_model and not served_model:
            return False
        return m.chat and (not m.runs_on.startswith("hpc:") or executors.get(m.runs_on) is not None)

    return tool_catalog.build_tools(where=wanted, route=_router(executors))
