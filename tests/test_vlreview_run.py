"""The render-review job runs the LIVE reviewer from the synced source tree (bioagent.tools.
vlreview_run) instead of the copy baked into vlreview.sif — so a detector added here reaches
production on the next code sync, with no image rebuild. deploy/vlreview/run_review.py stays as
the sif's fallback and MUST stay byte-identical."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_reviewer_is_byte_identical_to_the_deploy_copy():
    a = (ROOT / "deploy" / "vlreview" / "run_review.py").read_bytes()
    b = (ROOT / "src" / "bioagent" / "reporting" / "vlreview_run.py").read_bytes()
    assert a == b, "cp deploy/vlreview/run_review.py src/bioagent/reporting/vlreview_run.py"


def test_the_geometric_detectors_catch_what_the_vision_model_missed(tmp_path):
    """A production page had every parameter line running off the right edge and literal
    `**`/backticks on it, and Qwen2.5-VL-7B called it clean. Both are decidable from the PDF's
    own text and geometry, so both are decided deterministically, before the model."""
    fitz = __import__("pytest").importorskip("fitz")
    from bioagent.reporting import vlreview_run as rr

    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    # a monospace line far too long for the page, plus literal markup
    page.insert_text((10, 40), "- `min_genes` = `200` — drop a cell " * 4, fontname="cour", fontsize=10)
    page.insert_text((10, 80), "**chosen for this run** and `padj` and `lfc` and `x`", fontsize=10)
    pdf = tmp_path / "t.pdf"
    doc.save(pdf)
    doc = fitz.open(pdf)
    p = doc.load_page(0)
    clipped = rr._clipping_defects(p, 1)
    markup = rr._unrendered_markup_defects(p, 1)
    assert clipped and clipped[0]["type"] == "text_clipped"
    assert markup and markup[0]["type"] == "unrendered_markup"
    assert rr._bbox_overlap_defects(p, 1) == []          # nothing overlaps here
