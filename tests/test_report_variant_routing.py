"""Report-generation task-type routing + authoritative-counts binding + render-residue cleanup.

These guard the fixes for the variant-annotation report defects: a VCF run must NOT be told to write
scanpy Methods, its counts must come from the tool (not the model's free prose — the '165 high-priority'
/ '6-vs-12 citations' hallucinations), and render residue must be stripped deterministically.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway import app as gw  # noqa: E402


def _variant_result():
    """A fake lab result whose rounds carry an authoritative annotate_variants tool result."""
    res = {
        "status": "ok", "tool": "annotate_variants", "n_input_variants": 172,
        "n_pass": 172, "n_nonpass": 328, "n_pathogenic": 0, "n_high_priority": 0, "n_rare": 100,
        "by_consequence": {"intron_variant": 80}, "by_clinical_significance": {"not_in_clinvar": 172},
    }
    return types.SimpleNamespace(
        rounds=[{"scientist_result": {"steps": [{"tool": "annotate_variants", "result": res}]}}])


def test_task_kind_variant_from_tool_result(tmp_path):
    assert gw._report_task_kind(tmp_path, _variant_result()) == "variant"


def test_task_kind_variant_from_artifacts(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "annotated_results_summary.json").write_text("{}")
    assert gw._report_task_kind(tmp_path, None) == "variant"


def test_task_kind_single_cell_default(tmp_path):
    assert gw._report_task_kind(tmp_path, None) == "single_cell"


def test_variant_facts_block_binds_authoritative_counts(tmp_path):
    (tmp_path / "process").mkdir()
    (tmp_path / "process" / "literature_references.json").write_text(
        json.dumps({"citations": [{}] * 12}))
    block = gw._variant_facts_block(tmp_path, _variant_result())
    assert "AUTHORITATIVE COUNTS" in block
    assert "172 PASS, 328 non-PASS" in block            # #2: real FILTER counts, not "all pass"
    assert "high-priority shortlist: 0" in block         # #3: kills the fabricated "165"
    assert "ClinVar Pathogenic/Likely-Pathogenic: 0" in block
    assert "literature citations: 12" in block           # #3: kills the "6"


def test_variant_facts_block_empty_for_non_variant(tmp_path):
    assert gw._variant_facts_block(tmp_path, None) == ""


def test_manuscript_prompt_routes_by_kind():
    variant = gw._report_writer_system("variant")
    single = gw._report_writer_system("single_cell")
    # variant manuscript covers VEP/ClinVar and explicitly forbids the scanpy stages
    assert "VEP annotation" in variant and "ClinVar" in variant
    assert "do NOT describe" in variant and "PCA" in variant   # the negative scanpy instruction
    # single-cell keeps the scanpy Methods scaffold
    assert "HVG selection" in single and "neighbor graph (n_neighbors)" in single
    # AUTHORITATIVE COUNTS hard-rule present in both
    assert "AUTHORITATIVE COUNTS" in variant and "AUTHORITATIVE COUNTS" in single


def test_tech_prompt_routes_by_kind():
    variant = gw._tech_report_writer_system("variant")
    single = gw._tech_report_writer_system("single_cell")
    assert "variant-annotation" in variant and "n_pass / n_nonpass" in variant
    assert "n_neighbors" in single and "n_neighbors" not in variant


def test_strip_render_residue_fixes_all_three():
    md = (
        "## Results\n\n"
        "[Figure 1. Workflow schematic for variant annotation.]\n\n"
        "![Figure 1. Workflow schematic.](figures/workflow.png)\n\n\n\n"
        "Text with &lt;i&gt;BRCA2&lt;/i&gt; and <i>TP53</i>.\n\n\n\n"
        "## Refs\nKeep &amp; this entity.\n"
    )
    out = gw._strip_render_residue(md)
    assert "[Figure 1. Workflow schematic for variant" not in out            # stray caption line gone
    assert "![Figure 1. Workflow schematic.](figures/workflow.png)" in out    # real image kept
    assert "<i>" not in out and "&lt;i&gt;" not in out                        # tags stripped
    assert "BRCA2" in out and "TP53" in out                                   # gene text kept
    assert "&amp;" in out                                                     # other entities safe
    assert "\n\n\n" not in out                                               # blank runs collapsed
