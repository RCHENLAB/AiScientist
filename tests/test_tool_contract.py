"""The names the platform and the pipelines rely on, checked against the tools that exist.

After the split, the tools live in AiScientist-tools, while this repository keeps the logic that
keys on tool names (the planner's step heuristics, the report's "What was run" section, the
follow-up router) and depends on them. A tool renamed over there would silently switch that logic
off here, and a pipeline that names a tool that no longer exists teaches the agent to call nothing.
These are the cross-repository contracts, so they are tested here. The skills of the pinned
aiscientist-skills release are held to the same contract by tests/test_pinned_release.py.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from aiscientist.tools import catalog

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "aiscientist"

# Domain tools the platform's own code names as exact string literals, and where.
PLATFORM_RELIES_ON = {
    "annotate_variants": "gateway/app.py: VCF staging and the variant executor",
    "deep_literature": "gateway/app.py, agents/quick_chat.py, agents/research_lab.py: literature steps",
    "search_corpus": "agents/quick_chat.py, agents/research_lab.py, agents/registry.py: literature steps in a session with no served model",
    "extract_haploinsufficient_genes": "gateway/app.py: the corpus-scan line (route \"corpus\")",
    "inspect_dataset": "agents/research_lab.py: dataset triage steps",
    "literature_search": "gateway/app.py, agents/research_lab.py: literature steps and citations",
    "make_schematic": "agents/research_lab.py: figure steps",
    "run_clustering": "agents/research_lab.py: the label-reuse / re-cluster decision",
    "run_composition": "agents/research_lab.py: condition-study steps",
    "run_cellqc": "gateway/app.py, agents/research_lab.py: QC steps (the Cell Ranger route)",
    "run_de": "agents/research_lab.py: DE producers and their tested-count checks",
    "run_depth_matched_de": "agents/research_lab.py: depth checks",
    "run_doublet_detection": "agents/research_lab.py: QC steps",
    "run_enrichment": "agents/research_lab.py: enrichment steps",
    "run_gsea_prerank": "agents/research_lab.py: enrichment steps",
    "run_integration": "agents/scope.py: the plain word 'integration' in a user's limit",
    "run_marker_annotation": "agents/scope.py: the plain word 'annotation' in a user's limit",
    "run_pseudobulk_de": "agents/research_lab.py: DE producers",
    "run_scanpy_qc": "gateway/app.py, agents/research_lab.py: QC steps",
    "scgpt_annotate": "gateway/app.py, gateway/scgpt_runner.py, agents/research_lab.py: scGPT steps",
}

PLATFORM_TOOLS = {"finish", "run_qc", "run_de_markers", "run_code", "describe_environment",
                  "search_skills", "read_skill_reference", "read_tool_source",
                  "list_dir", "stat_path", "find_files", "read_text", "disk_usage", "run_shell",
                  "fetch_url", "install_package", "run_in_environment"}


def _platform_literals() -> set[str]:
    names = set(catalog.names())
    found: set[str] = set()
    for path in [*(SRC / "agents").glob("*.py"), *(SRC / "gateway").glob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
                found.add(node.value)
    return found


def test_every_tool_the_platform_names_exists():
    missing = set(PLATFORM_RELIES_ON) - set(catalog.names())
    assert not missing, (f"the platform keys logic on {sorted(missing)}, which no tool folder provides; "
                         "rename the references or restore the tools")


def test_the_platform_declares_every_tool_name_it_uses():
    undeclared = _platform_literals() - set(PLATFORM_RELIES_ON)
    assert not undeclared, (f"platform code now names {sorted(undeclared)}; add them to "
                            "PLATFORM_RELIES_ON so a rename in the tools package is caught")


_TOOLISH = re.compile(r"`((?:run|make|map|annotate|diagnose|inspect|deep|literature|scgpt|read|search|describe)"
                      r"_[a-z0-9_]+)(?:\(|`)")


# Skills this repository names: the preset pipelines' guidance, and the PI's example of planning a
# skill (agents/research_lab.py). They are not tools, and the names are the skills' earlier ones:
# each resolves to a skill of the pinned aiscientist-skills release by an alias, which
# tests/test_pinned_release.py checks when that release is installed.
NAMED_SKILLS = {
    "annotate_clusters_by_markers_v2": "celltype_annotation",
    "score_signature": "gene_signature_scoring",
    "mixscape_escape_filter": "perturbation_analysis",
    "perturbation_de_vs_control": "perturbation_analysis",
    "perturbation_edistance": "perturbation_analysis",
    "vcf_qc_stats": "phenotype_variant_diagnosis, variant_annotation",
    "crossvalidate_scgpt_vs_leiden": "scgpt_annotation",
    "build_variant_db_tiledbvcf": "variant_annotation",
    "clinical_variant_prioritization": "variant_annotation",
    "normalize_vcf": "variant_annotation",
    "variant_output_tables": "variant_annotation",
    "pairwise_de": "agents/research_lab.py: the PI's planning prompt",
}


def toolish_names(text: str) -> list[str]:
    """The tool-like names (``run_x`` and the like) a document puts in backticks."""
    return _TOOLISH.findall(text)


def test_pipelines_name_only_tools_that_exist():
    from aiscientist.agents.preset_pipelines import PIPELINES, _pipelines_dir

    # A pipeline's own name is a legitimate thing for these documents to mention (`scgpt_annotation`
    # is a pipeline, not a misspelt `scgpt_annotate`).
    known = set(catalog.names()) | PLATFORM_TOOLS | set(PIPELINES) | set(NAMED_SKILLS)
    bad = [f"{path}: `{name}`" for path in _pipelines_dir().rglob("*.md")
           for name in toolish_names(path.read_text(encoding="utf-8")) if name not in known]
    assert not bad, "pipelines name tools that do not exist:\n" + "\n".join(sorted(set(bad)))
