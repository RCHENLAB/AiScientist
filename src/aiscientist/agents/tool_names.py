"""Scientific identity for policy/report checks, without changing execution names.

Only a reviewed analysis server's configured planner names acquire these identities.
Unknown prefixes, foreign servers and unregistered aliases remain unchanged. Callers
must retain the actual tool name for dispatch, explicit user scope and provenance.
"""
from __future__ import annotations

# Domain ownership is deliberate: a foreign server cannot become QC by using its wire name.
_ANALYSIS_TOOLS = {
    "cellqc": frozenset({"run_cellqc", "cellqc_job_status"}),
    "scrna-analysis": frozenset({
        "run_scanpy_qc", "run_clustering", "run_de", "run_enrichment", "run_depth_matched_de",
        "run_gsea_prerank", "run_doublet_detection", "run_integration", "run_pseudobulk_de",
        "run_composition", "run_marker_annotation", "scrna_job_status"}),
    "scgpt-annotation": frozenset({"scgpt_annotate", "scgpt_job_status"}),
    "variant-annotation": frozenset({"annotate_variants", "variant_job_status"}),
    "literature": frozenset({"literature_search", "search_corpus", "deep_literature",
                             "extract_haploinsufficient_genes", "literature_job_status"}),
    "phenotype-diagnosis": frozenset({"map_phenotype_to_hpo", "run_lirical", "diagnose_disease",
                                      "phenotype_job_status"}),
    "data-utilities": frozenset({"inspect_dataset", "make_schematic", "data_utilities_job_status"}),
}
STATUS_TOOLS = frozenset(name for names in _ANALYSIS_TOOLS.values() for name in names
                         if name.endswith("_job_status"))


def canonical_tool_name(name: object) -> str:
    """Return a registered migrated tool's original name for semantic comparisons only."""
    name = str(name or "")
    if not name.startswith("mcp_"):
        return name
    from .mcp_bridge import configured_servers
    for server in configured_servers():
        if server.adapter != "analysis":
            continue
        owned = _ANALYSIS_TOOLS.get(server.name, ())
        for tool in server.tools:
            wire = tool["name"]
            if wire in owned and server.planner_name(wire) == name:
                return wire
    return name
