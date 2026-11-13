"""Everything the platform may use from the tools package, besides the catalog and ``sdk``.

The platform (gateway, agents) needs a few things from the tools beyond calling them: a dataset
triage it runs at upload time, the genome-build sniffer, the scRNA line's declared parameters for
the manuscript's Methods, the module paths of the in-container job entry points, and so on. Before
this module it imported them straight from the tool modules, private names included
(``_genesets_dir``, ``_looks_like_celltype_column``), so moving a helper inside the tools broke the
gateway.

Now the platform imports them from here and nowhere else (``tests/test_repo_boundaries.py``
enforces it). The tools can be reorganised freely; only the table below follows them. Names are
resolved lazily on first access, so ``import aiscientist.tools.api`` imports no tool module, and a
test that monkeypatches the underlying attribute is seen by the next lookup.
"""

from __future__ import annotations

import importlib
from typing import Any

# Job entry points, run as ``python -m <module>`` inside each HPC3 image.
ANALYSIS_ENTRYPOINT = "aiscientist.tools.worker_cli"        # analysis.sif: the scRNA tool line

# public name -> (module, attribute)
_EXPORTS: dict[str, tuple[str, str]] = {
    # the dataset profile the gateway computes at run start (in-process, or on HPC3 through the
    # analysis line's `preflight` step), and the smoke QC/DE stand-ins used without scanpy
    "run_dataset_smoke_analysis": ("aiscientist.tools._lib.dataset_preflight", "run_dataset_preflight"),
    # dataset triage (inspect_dataset), also run by the gateway at upload / run start
    "describe_dataset": ("aiscientist.tools._lib.dataset_inspection", "describe_dataset"),
    "peek_dataset": ("aiscientist.tools._lib.dataset_inspection", "peek_dataset"),
    # variants (annotate_variants)
    "detect_assembly": ("aiscientist.tools._lib.variant_assembly", "detect_assembly"),
    "load_gene_panel": ("aiscientist.agents.reference_panels", "load_gene_panel"),
    # figures (make_schematic), also used for the report's workflow figure
    "render_dot": ("aiscientist.tools._lib.schematics", "render_dot"),
    "workflow_schematic_dot": ("aiscientist.tools._lib.schematics", "workflow_schematic_dot"),
    "render_workflow_schematic": ("aiscientist.tools._lib.schematics", "render_workflow_schematic"),
    # literature
    "focus_literature_query": ("aiscientist.literature_contract", "focus_literature_query"),
    "QUERY_STOPWORDS": ("aiscientist.literature_contract", "QUERY_STOPWORDS"),
    # the corpus scan's rules as one fingerprint: a stored scan is reused only under the same rules
    # a task spec for a property the scan has no rules for: the drafting prompt, its check, and the
    # folder the scan writes that spec's tables to
    "CORPUS_SCAN_SPEC_SYSTEM": ("aiscientist.literature_contract", "SPEC_SYSTEM"),
    "parse_corpus_scan_spec": ("aiscientist.literature_contract", "parse_spec"),
    "corpus_scan_spec_folder": ("aiscientist.literature_contract", "spec_folder"),
    # the scRNA analysis line
    # curated marker references (run_marker_annotation), also read by the report's marker-claim check
    "available_references": ("aiscientist.tools._lib.marker_references", "available_references"),
    "load_marker_reference": ("aiscientist.tools._lib.marker_references", "load_reference"),
    # Cell Ranger deliveries in an uploaded folder: what they are, and the QC route they imply
    "describe_cellranger_layout": ("aiscientist.tools._lib.cellranger", "describe_cellranger_layout"),
    "merge_cellranger_layouts": ("aiscientist.tools._lib.cellranger", "merge_cellranger_layouts"),
    "cellranger_matrix_facts": ("aiscientist.tools._lib.cellranger", "matrix_facts"),
    "cellranger_layout_hint": ("aiscientist.tools._lib.cellranger", "cellranger_layout_hint"),
    "profile_cellranger_libraries": ("aiscientist.tools._lib.cellranger", "profile_cellranger_libraries"),
    "CELLRANGER_FIND_NAMES": ("aiscientist.tools._lib.cellranger", "FIND_NAMES"),
    "CELLRANGER_FIND_MAXDEPTH": ("aiscientist.tools._lib.cellranger", "FIND_MAXDEPTH"),
    "genesets_dir": ("aiscientist.tools._lib.analysis_metadata", "_genesets_dir"),
    "looks_like_celltype_column": ("aiscientist.tools._lib.analysis_metadata", "_looks_like_celltype_column"),
    "looks_like_condition_column": ("aiscientist.tools._lib.analysis_metadata", "_looks_like_condition_column"),
    "DECLARED_PARAMS": ("aiscientist.tools._lib.analysis_metadata", "PARAMS"),
    "TOOL_SUMMARY": ("aiscientist.tools._lib.analysis_metadata", "TOOL_SUMMARY"),
    # HPC3 job runtime
    "run_analysis_tool": ("aiscientist.tools.worker_cli", "run_tool"),
    "INSTALL_DEPENDENCY": ("aiscientist.tools.worker_cli", "INSTALL_DEPENDENCY"),
    # the processed dataset a run hands on (built from its checkpoints at the end of the run)
    "EXPORT_RESULT_DATASET": ("aiscientist.tools.worker_cli", "EXPORT_RESULT_DATASET"),
    "export_result_dataset": ("aiscientist.tools._lib.result_dataset", "export"),
    "result_dataset_name": ("aiscientist.tools._lib.result_dataset", "result_name"),
    "RESULT_DATASET_POINTER": ("aiscientist.tools._lib.result_dataset", "POINTER"),
    # CellQC's complete result/ directory, kept with the run (run_cellqc writes the pointer)
    "CELLQC_RESULT_POINTER": ("aiscientist.tools._lib.result_dataset", "CELLQC_POINTER"),
    "cellqc_result_name": ("aiscientist.tools._lib.result_dataset", "cellqc_result_name"),
    # ... and the next run of the conversation starts from (written back as its checkpoints)
    "ADOPT_RESULT_DATASET": ("aiscientist.tools.worker_cli", "ADOPT_RESULT_DATASET"),
    "adopt_result_dataset": ("aiscientist.tools._lib.result_dataset", "adopt"),
    "resolve_run_dependency": ("aiscientist.tools.run_deps", "resolve"),
    "DEPS_DIRNAME": ("aiscientist.tools.run_deps", "DEPS_DIRNAME"),
    "SITECUSTOMIZE": ("aiscientist.tools.run_deps", "SITECUSTOMIZE"),
    # tool factories, for the registry
}

__all__ = sorted([*_EXPORTS, "ANALYSIS_ENTRYPOINT"])


def __getattr__(name: str) -> Any:
    try:
        module, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"aiscientist.tools.api has no attribute {name!r}") from None
    return getattr(importlib.import_module(module), attr)


def __dir__() -> list[str]:
    return list(__all__)
