"""Everything the platform may use from the tools package, besides the catalog and ``sdk``.

The platform (gateway, agents) needs a few things from the tools beyond calling them: a dataset
triage it runs at upload time, the genome-build sniffer, the scRNA line's declared parameters for
the manuscript's Methods, the module paths of the in-container job entry points, and so on. Before
this module it imported them straight from the tool modules, private names included
(``_genesets_dir``, ``_looks_like_celltype_column``), so moving a helper inside the tools broke the
gateway.

Now the platform imports them from here and nowhere else (``tests/test_repo_boundaries.py``
enforces it). The tools can be reorganised freely; only the table below follows them. Names are
resolved lazily on first access, so ``import bioagent.tools.api`` imports no tool module, and a
test that monkeypatches the underlying attribute is seen by the next lookup.
"""

from __future__ import annotations

import importlib
from typing import Any

# Job entry points, run as ``python -m <module>`` inside each HPC3 image.
ANALYSIS_ENTRYPOINT = "bioagent.tools.scrna_cli"        # analysis.sif: the scRNA tool line
VARIANT_ENTRYPOINT = "bioagent.tools.variant_cli"       # vep.sif: annotate_variants (offline VEP)
PHENOTYPE_ENTRYPOINT = "bioagent.tools.phenotype_cli"   # lirical.sif: run_lirical
LITERATURE_ENTRYPOINT = "bioagent.tools.paperqa_cli"    # paperqa.sif: deep_literature

# public name -> (module, attribute)
_EXPORTS: dict[str, tuple[str, str]] = {
    # the dataset profile the gateway computes at run start (in-process, or on HPC3 through the
    # analysis line's `preflight` step), and the smoke QC/DE stand-ins used without scanpy
    "run_dataset_smoke_analysis": ("bioagent.tools.datasets", "run_dataset_smoke_analysis"),
    "build_single_cell_qc_execution": ("bioagent.tools.execution", "build_single_cell_qc_execution"),
    "build_de_marker_execution": ("bioagent.tools.execution", "build_de_marker_execution"),
    # dataset triage (inspect_dataset), also run by the gateway at upload / run start
    "describe_dataset": ("bioagent.tools.inspect_dataset.tool", "describe_dataset"),
    "peek_dataset": ("bioagent.tools.inspect_dataset.tool", "peek_dataset"),
    # variants (annotate_variants)
    "detect_assembly": ("bioagent.tools.annotate_variants.offline", "detect_assembly"),
    "annotate_variants_rest": ("bioagent.tools.annotate_variants.tool", "annotate_variants_rest"),
    "load_gene_panel": ("bioagent.tools.gene_panels", "load_gene_panel"),
    # figures (make_schematic), also used for the report's workflow figure
    "render_dot": ("bioagent.tools.make_schematic.tool", "render_dot"),
    "workflow_schematic_dot": ("bioagent.tools.make_schematic.tool", "workflow_schematic_dot"),
    # literature
    "focus_literature_query": ("bioagent.tools.literature_search.tool", "focus_literature_query"),
    "QUERY_STOPWORDS": ("bioagent.tools.literature_search.tool", "_QUERY_STOPWORDS"),
    # the scRNA analysis line
    "scrna_catalog": ("bioagent.tools.catalog", "scrna_catalog"),
    "genesets_dir": ("bioagent.tools._lib.scrna", "_genesets_dir"),
    "looks_like_celltype_column": ("bioagent.tools._lib.scrna", "_looks_like_celltype_column"),
    "looks_like_condition_column": ("bioagent.tools._lib.scrna", "_looks_like_condition_column"),
    "DECLARED_PARAMS": ("bioagent.tools._lib.scrna", "PARAMS"),
    "TOOL_SUMMARY": ("bioagent.tools._lib.scrna", "TOOL_SUMMARY"),
    # HPC3 job runtime
    "run_analysis_tool": ("bioagent.tools.scrna_cli", "run_tool"),
    "INSTALL_DEPENDENCY": ("bioagent.tools.scrna_cli", "INSTALL_DEPENDENCY"),
    "resolve_run_dependency": ("bioagent.tools.run_deps", "resolve"),
    "DEPS_DIRNAME": ("bioagent.tools.run_deps", "DEPS_DIRNAME"),
    "SITECUSTOMIZE": ("bioagent.tools.run_deps", "SITECUSTOMIZE"),
    # tool factories, for the registry
    "make_inspect_dataset_tool": ("bioagent.tools.inspect_dataset.tool", "make_inspect_dataset_tool"),
    "make_hpo_mapping_tool": ("bioagent.tools.map_phenotype_to_hpo.tool", "make_hpo_mapping_tool"),
    "make_literature_search_tool": ("bioagent.tools.literature_search.tool", "make_literature_search_tool"),
    "make_paperqa_tool": ("bioagent.tools.deep_literature.tool", "make_paperqa_tool"),
    "make_phenotype_differential_tool": ("bioagent.tools.run_lirical.tool", "make_phenotype_differential_tool"),
    "make_diagnose_disease_tool": ("bioagent.tools.diagnose_disease.tool", "make_diagnose_disease_tool"),
    "make_schematic_tool": ("bioagent.tools.make_schematic.tool", "make_schematic_tool"),
    "make_variant_annotation_tool": ("bioagent.tools.annotate_variants.tool", "make_variant_annotation_tool"),
    "make_scgpt_annotate_tool": ("bioagent.tools.scgpt_annotate.tool", "make_scgpt_annotate_tool"),
}

__all__ = sorted([*_EXPORTS, "ANALYSIS_ENTRYPOINT", "VARIANT_ENTRYPOINT", "PHENOTYPE_ENTRYPOINT",
                  "LITERATURE_ENTRYPOINT"])


def __getattr__(name: str) -> Any:
    try:
        module, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"bioagent.tools.api has no attribute {name!r}") from None
    return getattr(importlib.import_module(module), attr)


def __dir__() -> list[str]:
    return list(__all__)
