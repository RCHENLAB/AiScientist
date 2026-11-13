"""The single source of truth for the Scientist's toolset.

Two kinds of tool meet here:

* **Domain tools** live in ``aiscientist.tools``, one folder per tool, each with a ``TOOL.md`` whose
  front matter is its manifest. ``tools.catalog`` discovers them, and nothing in this module names
  one: a new tool is a new folder. Its manifest says where it runs (``runs_on``), and that is all
  the routing below needs.
* **Platform tools** expose the platform's own machinery, so they are assembled here: ``finish`` and
  loop control (``research_harness.default_catalog``), CodeAct ``run_code`` (the per-run
  sandbox), the HPC3 shell family (``hpc.shell``) and ``describe_environment``.

    build_scientist_catalog(code_executor=..., ...) -> list[HarnessTool]

Each HarnessTool self-describes (``category`` / ``requires`` / ``reads_private_data``), so the
System page renders straight off this list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..tools import catalog as tool_catalog
from .tool_skills import attach_skill

if TYPE_CHECKING:
    from .research_harness import HarnessTool


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


def build_scientist_catalog(code_executor: Any = None,
                            hpc_shell: Any = None) -> list["HarnessTool"]:
    """Assemble platform tools and registered native scientific capabilities.

    Native scientific tools are appended by the registered MCP bridge; this catalog contains
    the platform tools and any remaining local manifests.

    A manifest that says ``research: false`` is left out: that tool runs only from its own console
    line (the corpus scan is an hour-long job the user starts, with a workspace of its own), so
    the PI cannot plan it and the Scientist cannot call it. The line reaches it by name through
    its executor, not through this catalog.

    Cross-server scientific composition belongs to the standalone MCP packages."""
    from .research_harness import default_catalog
    from .research_lab import make_run_code_tool, make_run_in_environment_tool

    executors = {}
    catalog: list[HarnessTool] = list(default_catalog())                  # finish; scientific operations come from registered MCP servers
    catalog += tool_catalog.build_tools(where=lambda m: m.research,
                                        route=_router(executors))
    # A failed snippet that lacked a program gets the HPC3 module that provides it (hpc/modules.py).
    note = ((lambda text: hpc_shell.missing_software_note(text, "run_code")) if hpc_shell is not None else None)
    # Lab environments the agent built with request_environment (hpc/environments.py), bound by id.
    lab_envs = (hpc_shell.resolve_environments
                if hpc_shell is not None and getattr(hpc_shell, "environments", None) is not None else None)
    catalog.append(make_run_code_tool(code_executor, software_note=note,   # CodeAct (needs the sandbox)
                                      environments=lab_envs))
    # A skill's declared environment (SKILL.md ``image:``) — only when the executor can run images
    # on HPC3, so a local run's roster stays honest.
    env_tool = make_run_in_environment_tool(code_executor, environments=lab_envs)
    if env_tool is not None:
        catalog.append(env_tool)
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
    # MCP server tools come last. Whatever goes wrong with them leaves them out of this catalog
    # and is logged; it never stops a run or the System page.
    from .mcp_bridge import build_mcp_tools
    try:
        catalog.extend(build_mcp_tools(code_executor, env_tool, existing_names={t.name for t in catalog}))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("MCP tools left out of the catalog")
    return catalog


# A tool's declared requirement (its PyPI name) -> the module it imports as, where they differ.
_IMPORT_NAMES: dict[str, str] = {}


def _importable(requirement: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(_IMPORT_NAMES.get(requirement, requirement)) is not None
    except (ImportError, ValueError):
        return False


def session_catalog(catalog: list["HarnessTool"]) -> list["HarnessTool"]:
    """Native MCP availability and model binding are handled by the per-run bridge."""
    return list(catalog)


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
def build_quickchat_catalog() -> list["HarnessTool"]:
    """Local platform chat tools; scientific literature uses the dedicated native run route."""
    return tool_catalog.build_tools(where=lambda m: m.chat and not m.runs_on.startswith("hpc:"),
                                    route=_router({}))
