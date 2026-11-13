"""Report-generation task-type routing + authoritative-counts binding + render-residue cleanup.

These guard the fixes for the variant-annotation report defects: a VCF run must NOT be told to write
scanpy Methods, its counts must come from the tool (not the model's free prose — the '165 high-priority'
/ '6-vs-12 citations' hallucinations), and render residue must be stripped deterministically.
"""

from __future__ import annotations

import json
import types

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw  # noqa: E402


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


@pytest.fixture
def native_variant(monkeypatch):
    from aiscientist.agents import mcp_bridge

    server = types.SimpleNamespace(name="variant-annotation", adapter="analysis",
                                   tools=({"name": "annotate_variants"},),
                                   planner_name=lambda wire: "mcp_" + wire)
    monkeypatch.setattr(mcp_bridge, "configured_servers", lambda: (server,))
    native = {"status": "ok", "state": "COMPLETED", "analysis_id": "case1", "task_id": "attempt1",
              "full_result": "mcp/variant-annotation/case1/annotate_variants.json",
              "response_omitted": True}
    out = {**native, "result": native, "mcp": {"name": "variant-annotation"}}
    result = types.SimpleNamespace(rounds=[{"scientist_result": {"steps": [
        {"tool": "mcp_annotate_variants", "ok": True, "result": out}]}}])
    return result, native


def _owned_variant_file(art, relative, value):
    import hashlib

    path = art / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    identity = {"analysis_id": "case1", "task_id": "attempt1"}
    manifest = art / "mcp/.compatibility.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    record = json.loads(manifest.read_text()) if manifest.exists() else {
        "current": {"variant-annotation": identity}, "files": {}}
    record["files"][relative] = {"server": "variant-annotation", **identity,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest.write_text(json.dumps(record))
    return path, manifest


def test_native_variant_routes_without_legacy_name(native_variant, tmp_path):
    result, native = native_variant
    native.update(by_consequence={"intron_variant": 2}, n_input_variants=2, n_pathogenic=1)
    assert gw._report_task_kind(tmp_path, result) == "variant"
    block = gw._variant_facts_block(tmp_path, result)
    assert "variants annotated: 2" in block and "Pathogenic/Likely-Pathogenic: 1" in block
    assert "high-priority shortlist: unknown" in block


def test_bounded_native_counts_recover_only_owned_summary(native_variant, tmp_path):
    result, _ = native_variant
    _owned_variant_file(tmp_path, "data/annotated_results_summary.json", {
        "total_variants": 172, "consequence_distribution": {"intron_variant": 80},
        "n_pathogenic_clinvar": 3, "n_high_priority_rare_deleterious": 7,
        "n_rare_variants_af_lt_0.1": 100})
    block = gw._variant_facts_block(tmp_path, result)
    assert "variants annotated: 172" in block
    assert "Pathogenic/Likely-Pathogenic: 3" in block
    assert "high-priority shortlist: 7" in block
    assert "rare variants (below AF cutoff): 100" in block
    assert "coverage/truncation: unknown" in block
    assert "do not establish whole-VCF coverage" in block


@pytest.mark.parametrize("mutation", ["changed_file", "wrong_task", "wrong_owner", "stale_current",
                                       "symlink", "oversize", "missing_manifest"])
def test_unverified_summary_does_not_become_authoritative(native_variant, tmp_path, mutation):
    result, _ = native_variant
    path, manifest = _owned_variant_file(tmp_path, "data/annotated_results_summary.json",
                                         {"total_variants": 999, "n_pathogenic_clinvar": 999})
    record = json.loads(manifest.read_text())
    if mutation == "changed_file":
        path.write_text('{"total_variants": 1000}')
    elif mutation == "wrong_task":
        record["files"]["data/annotated_results_summary.json"]["task_id"] = "other"
    elif mutation == "wrong_owner":
        record["files"]["data/annotated_results_summary.json"]["server"] = "foreign"
    elif mutation == "stale_current":
        record["current"]["variant-annotation"]["task_id"] = "later"
    elif mutation == "symlink":
        outside = tmp_path / "caller.json"
        path.rename(outside)
        path.symlink_to(outside)
    elif mutation == "oversize":
        import hashlib
        path.write_text(json.dumps({"total_variants": 999, "extra": "x" * (4 * 1024 * 1024)}))
        record["files"]["data/annotated_results_summary.json"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if mutation == "missing_manifest":
        manifest.unlink()
    else:
        manifest.write_text(json.dumps(record))
    block = gw._variant_facts_block(tmp_path, result)
    assert "variants annotated: unknown" in block
    assert "Pathogenic/Likely-Pathogenic: unknown" in block
    assert "999" not in block and "1000" not in block


def test_namespaced_full_result_also_requires_ownership_hash(native_variant, tmp_path):
    result, native = native_variant
    path = tmp_path / native["full_result"]
    path.parent.mkdir(parents=True)
    value = {"status": "ok", "tool": "annotate_variants", "n_input_variants": 80,
             "n_pass": 80, "by_consequence": {"missense_variant": 80}, "n_pathogenic": 2}
    path.write_text(json.dumps(value))
    assert "variants annotated: unknown" in gw._variant_facts_block(tmp_path, result)
    _owned_variant_file(tmp_path, native["full_result"], value)
    block = gw._variant_facts_block(tmp_path, result)
    assert "variants annotated: 80" in block and "Pathogenic/Likely-Pathogenic: 2" in block
    assert "80 PASS, unknown non-PASS" in block and "(all records PASS)" not in block


def test_full_result_cannot_escape_its_analysis(native_variant, tmp_path):
    result, native = native_variant
    native["full_result"] = "../other/annotate_variants.json"
    assert "variants annotated: unknown" in gw._variant_facts_block(tmp_path, result)


def test_foreign_tool_or_server_cannot_supply_variant_counts(native_variant, tmp_path):
    result, _ = native_variant
    step = result.rounds[0]["scientist_result"]["steps"][0]
    step["tool"] = "mcp_foreign_annotate_variants"
    assert gw._annotate_variants_result(result, tmp_path) is None
    step["tool"] = "mcp_annotate_variants"
    step["result"]["mcp"]["name"] = "foreign"
    assert gw._annotate_variants_result(result, tmp_path) is None


def test_owned_full_result_preserves_annotation_cap(native_variant, tmp_path):
    result, native = native_variant
    _owned_variant_file(tmp_path, native["full_result"], {
        "status": "ok", "tool": "annotate_variants", "n_input_variants": 50000,
        "n_variants": 500, "n_annotated": 500, "truncated": True,
        "by_consequence": {"missense_variant": 500}})
    block = gw._variant_facts_block(tmp_path, result)
    assert "variants annotated: 500" in block
    assert "TRUNCATED: only the first 500 variants" in block and "not the whole VCF" in block
    assert "coverage/truncation: unknown" not in block
