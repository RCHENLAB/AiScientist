"""The single source of truth for the Scientist's toolset.

Before this, the catalog was hand-assembled in three places (the gateway's _run_lab,
the System-page introspection, and the lab's default) — which could silently drift.
Now every tool/group is declared ONCE here, and everything builds from it:

    build_scientist_catalog(code_executor=...) -> list[HarnessTool]

A new research line registers its ``*_catalog()`` in TOOL_PROVIDERS below — nothing
else changes. Each HarnessTool already self-describes (``category`` / ``requires`` /
``reads_private_data``), so the System page renders straight off this list.

The ``code_executor`` is injected at build time because the CodeAct ``run_code`` tool
needs the per-run sandbox; all other providers ignore it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .research_harness import HarnessTool


def _provider_imports() -> list[Callable[[Any], list["HarnessTool"]]]:
    """The declarative registry: each entry contributes tools to the catalog, in order.
    Imported lazily so this module has no import-time cycle with the tool modules."""
    from .research_harness import default_catalog
    from .research_lab import make_run_code_tool
    from ..tools.dataset_inspect import make_inspect_dataset_tool
    from ..tools.hpo_terms.mapper import make_hpo_mapping_tool
    from ..tools.literature_search import make_literature_search_tool
    from ..tools.paperqa_search import make_paperqa_tool
    from ..tools.phenotype_dx import make_diagnose_disease_tool, make_phenotype_differential_tool
    from ..tools.schematic import make_schematic_tool
    from ..tools.scrna_pack import scrna_catalog
    from ..tools.variant_annotation import make_variant_annotation_tool

    return [
        lambda _ctx: default_catalog(),                       # smoke QC/DE + finish
        lambda _ctx: [make_inspect_dataset_tool()],           # general file ingest/triage (skim any upload)
        lambda _ctx: scrna_catalog(),                         # real scanpy/gseapy analysis line
        lambda _ctx: [make_literature_search_tool()],         # real citations (Europe PMC)
        lambda _ctx: [make_paperqa_tool()],                   # deep cited answers (PaperQA2, local Qwen)
        lambda _ctx: [make_variant_annotation_tool()],        # VCF → VEP + ClinVar annotation (genomics)
        lambda _ctx: [make_hpo_mapping_tool()],               # clinician free text → validated HPO IDs
        lambda _ctx: [make_phenotype_differential_tool()],    # HPO + VCF → per-disease confidence (LIRICAL)
        lambda _ctx: [make_diagnose_disease_tool()],          # LIRICAL + literature → ONE adjudicated dx
        lambda _ctx: [make_schematic_tool()],                 # deterministic figures
        lambda ctx: [make_run_code_tool(ctx)],                # CodeAct (needs the sandbox)
    ]


# Lightweight smoke tools that the REAL scanpy analysis line supersedes. They exist as a
# fallback for hosts WITHOUT scanpy; when scanpy is installed, keeping them would force the
# PI/Scientist to choose between a real tool and a placeholder twin for the same step, which
# muddies planning and weakens results — so we drop the twins whenever the real tool can run.
_SUPERSEDED_WHEN_SCANPY = ("run_qc", "run_de_markers")

# The real scanpy analysis-line tools that can be offloaded to HPC3 as Slurm jobs (Phase 4).
# The smoke fallbacks (_SUPERSEDED_WHEN_SCANPY) are NOT routed — the in-container CLI only knows
# these four.
_HPC_ANALYSIS_TOOLS = ("run_scanpy_qc", "run_clustering", "run_de", "run_enrichment")

# The variant-line tool that can be offloaded to HPC3 as an OFFLINE VEP Slurm job (variant_cli).
_HPC_VARIANT_TOOLS = ("annotate_variants",)

# The phenotype-line tool that can be offloaded to HPC3 as a LIRICAL Slurm job (phenotype_cli).
_HPC_PHENOTYPE_TOOLS = ("run_lirical",)

# The literature-line tool that can be offloaded to HPC3 (deep_literature = PaperQA). Unlike the
# other lines it is offloaded NOT for compute but because the PubMedBERT index lives on /dfs3b,
# which the eyeserver gateway cannot read in place — so it runs in paperqa.sif on HPC3 (paperqa_cli).
_HPC_LITERATURE_TOOLS = ("deep_literature",)


def _route_to_executor(tool: "HarnessTool", executor: Any, names: tuple[str, ...]) -> "HarnessTool":
    """If ``tool.name`` is in ``names``, return a copy whose executor delegates to the HPC
    ``executor`` (which itself falls back in-process). Other tools pass through unchanged."""
    if tool.name not in names:
        return tool
    import dataclasses

    def _exec(args: dict, ctx: Any, _ex=executor, _name=tool.name) -> dict:
        return _ex.run_tool(_name, args, ctx)

    try:
        return dataclasses.replace(tool, executor=_exec)
    except TypeError:                       # not a (replaceable) dataclass — mutate in place
        tool.executor = _exec
        return tool


def _route_analysis(tool: "HarnessTool", analysis_executor: Any) -> "HarnessTool":
    """Route the real scanpy analysis-line tools to the HPC ``analysis_executor``."""
    return _route_to_executor(tool, analysis_executor, _HPC_ANALYSIS_TOOLS)


def _route_variant(tool: "HarnessTool", variant_executor: Any) -> "HarnessTool":
    """Route ``annotate_variants`` to the OFFLINE VEP ``variant_executor`` (REST fallback baked in)."""
    return _route_to_executor(tool, variant_executor, _HPC_VARIANT_TOOLS)


def _route_phenotype(tool: "HarnessTool", phenotype_executor: Any) -> "HarnessTool":
    """Route ``run_lirical`` to the HPC3 LIRICAL ``phenotype_executor`` (in-process not_installed
    fallback baked in)."""
    return _route_to_executor(tool, phenotype_executor, _HPC_PHENOTYPE_TOOLS)


def _route_literature(tool: "HarnessTool", literature_executor: Any) -> "HarnessTool":
    """Route ``deep_literature`` to the HPC3 PaperQA ``literature_executor`` (which reads the
    /dfs3b index in paperqa.sif and reaches the session's Qwen at the GPU node). The executor's
    in-process fallback returns dependency_missing when HPC is down, so a chat/lab turn degrades
    gracefully instead of erroring."""
    return _route_to_executor(tool, literature_executor, _HPC_LITERATURE_TOOLS)


def build_scientist_catalog(code_executor: Any = None, scgpt_runner: Any = None,
                            analysis_executor: Any = None,
                            variant_executor: Any = None,
                            phenotype_executor: Any = None,
                            literature_executor: Any = None) -> list["HarnessTool"]:
    """Assemble the full ordered Scientist catalog from the registry. When scanpy is
    available, the lightweight smoke QC/DE tools are dropped in favour of the real
    scanpy analysis line (they remain only as a no-scanpy fallback).

    ``scgpt_runner`` is the gateway-injected remote executor for the scGPT GPU batch job;
    the ``scgpt_annotate`` tool is always present (it self-reports not-enabled without one,
    so the System page can list it) — like ``run_code`` without a sandbox.

    ``analysis_executor`` (a ``SlurmAnalysisExecutor``), when provided, routes the real scanpy
    tools to run as HPC3 CPU batch jobs instead of in-process — with an in-process fallback baked
    into the executor, so behaviour is unchanged when it isn't wired or HPC is unavailable.

    ``variant_executor`` does the same for ``annotate_variants``: routes it to the OFFLINE VEP line
    on HPC3 (variant_cli), with the REST path as the in-process fallback for small VCFs / no-HPC.

    ``phenotype_executor`` does the same for ``run_lirical``: routes it to the LIRICAL line on HPC3
    (phenotype_cli), with an in-process ``not_installed`` fallback when it isn't wired / HPC is down."""
    import importlib.util

    from ..tools.scgpt_annotate import make_scgpt_annotate_tool

    catalog: list[HarnessTool] = []
    for provider in _provider_imports():
        catalog.extend(provider(code_executor))
    if importlib.util.find_spec("scanpy") is not None:
        catalog = [t for t in catalog if t.name not in _SUPERSEDED_WHEN_SCANPY]
    if analysis_executor is not None:
        catalog = [_route_analysis(t, analysis_executor) for t in catalog]
    if variant_executor is not None:
        catalog = [_route_variant(t, variant_executor) for t in catalog]
    if phenotype_executor is not None:
        catalog = [_route_phenotype(t, phenotype_executor) for t in catalog]
    if literature_executor is not None:
        catalog = [_route_literature(t, literature_executor) for t in catalog]
    catalog = _bind_diagnose_disease(catalog)
    catalog.append(make_scgpt_annotate_tool(scgpt_runner))   # foundation-model annotation (GPU)
    return catalog


def _bind_diagnose_disease(catalog: list["HarnessTool"]) -> list["HarnessTool"]:
    """Rebuild ``diagnose_disease`` bound to the FINAL executors of the two tools it composes.

    It must run LAST — after every routing pass — because it delegates to whatever ``run_lirical``
    and ``deep_literature`` ended up being. Binding it earlier would freeze the in-process versions
    and the adjudicated differential would silently stop using HPC3 while the two tools it wraps
    still did. Absent either tool (e.g. deep_literature dropped for want of a literature executor),
    it is rebuilt with what IS present and degrades to that track."""
    from ..tools.phenotype_dx import make_diagnose_disease_tool

    by_name = {t.name: t for t in catalog}
    if "diagnose_disease" not in by_name:
        return catalog
    lirical = by_name.get("run_lirical")
    literature = by_name.get("deep_literature")
    bound = make_diagnose_disease_tool(
        literature_fn=literature.executor if literature is not None else None,
        lirical_fn=lirical.executor if lirical is not None else None,
    )
    return [bound if t.name == "diagnose_disease" else t for t in catalog]


# The FAST-PATH toolset (see ``agents/quick_chat.py``). Deliberately a short, hand-picked list
# rather than a filter over the full catalog — "which tools are cheap enough for a chat turn" is a
# product decision, and a filter would silently admit every future tool that happened to match.
#
# The bar: runs IN-PROCESS on the gateway, returns in seconds, needs no run workspace, no Slurm
# job, and no dataset binding. That excludes, on purpose:
#   * ``run_code`` / the scanpy + variant + phenotype lines — minutes-to-hours of HPC3 compute, and
#     a chat turn has no run bundle to write results into. Analysis belongs on the research path.
#   * ``make_schematic`` — it renders a figure ARTIFACT into ``<run>/artifacts/figures``, which a
#     chat turn does not have. Inline diagrams in chat go the other way: the model writes a
#     ```mermaid fence and the browser renders it (Feature B). The two are complementary, and the
#     schematic tool is untouched on the research path.
_QUICKCHAT_TOOLS = ("literature_search", "map_phenotype_to_hpo", "deep_literature")


def build_quickchat_catalog(literature_executor: Any = None) -> list["HarnessTool"]:
    """The tools the answer-first chat loop may call. Built from the SAME provider functions as the
    research catalog (so a tool is never defined twice and cannot drift between paths), then
    narrowed to ``_QUICKCHAT_TOOLS``."""
    from ..tools.hpo_terms.mapper import make_hpo_mapping_tool
    from ..tools.literature_search import make_literature_search_tool
    from ..tools.paperqa_search import make_paperqa_tool

    catalog = [make_literature_search_tool(), make_hpo_mapping_tool(), make_paperqa_tool()]
    tools = [t for t in catalog if t.name in _QUICKCHAT_TOOLS]
    # deep_literature (PaperQA) reads the /dfs3b index the eyeserver can't see, so in the fast path
    # it must be OFFLOADED to HPC3 exactly like the research path. When a literature_executor is
    # injected, route it; otherwise drop it (a chat turn must never try to run it in-process on the
    # gateway, which would fail to find the index).
    if literature_executor is not None:
        tools = [_route_literature(t, literature_executor) for t in tools]
    else:
        tools = [t for t in tools if t.name not in _HPC_LITERATURE_TOOLS]
    return tools
