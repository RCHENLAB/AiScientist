"""The render-review job runs the LIVE reviewer from the synced source tree (aiscientist.tools.
vlreview_run) instead of the copy baked into vlreview.sif — so a detector added here reaches
production on the next code sync, with no image rebuild. deploy/vlreview/run_review.py stays as
the sif's fallback and MUST stay byte-identical."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_reviewer_is_byte_identical_to_the_deploy_copy():
    a = (ROOT / "deploy" / "vlreview" / "run_review.py").read_bytes()
    b = (ROOT / "src" / "aiscientist" / "reporting" / "vlreview_run.py").read_bytes()
    assert a == b, "cp deploy/vlreview/run_review.py src/aiscientist/reporting/vlreview_run.py"


def test_the_geometric_detectors_catch_what_the_vision_model_missed(tmp_path):
    """A production page had every parameter line running off the right edge and literal
    `**`/backticks on it, and Qwen2.5-VL-7B called it clean. Both are decidable from the PDF's
    own text and geometry, so both are decided deterministically, before the model."""
    fitz = __import__("pytest").importorskip("fitz")
    from aiscientist.reporting import vlreview_run as rr

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


def test_an_image_placed_as_a_strip_is_a_defect(tmp_path):
    """Run 1e887ed2bf8e: a 14,312 x 342 px workflow figure was printed 6.5 in wide and under 4 mm
    tall, and the review returned {"clean": true}. The box an image is placed in is in the PDF."""
    fitz = __import__("pytest").importorskip("fitz")
    from aiscientist.reporting import vlreview_run as rr

    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
    pix.clear_with(200)
    png = pix.tobytes("png")
    doc = fitz.open()
    strip_page = doc.new_page(width=612, height=792)
    strip_page.insert_image(fitz.Rect(72, 300, 540, 311.2), stream=png, keep_proportion=False)   # 42:1
    strip_page.insert_image(fitz.Rect(72, 100, 540, 195), stream=png, keep_proportion=False)     # 4.9:1
    fine_page = doc.new_page(width=612, height=792)
    fine_page.insert_image(fitz.Rect(72, 100, 261, 353), stream=png, keep_proportion=False)      # 1:1.3
    fine_page.insert_image(fitz.Rect(72, 400, 112, 404), stream=png, keep_proportion=False)      # a small mark
    pdf = tmp_path / "t.pdf"
    doc.save(pdf)
    doc = fitz.open(pdf)

    found = rr._strip_image_defects(doc.load_page(0), 1)
    assert len(found) == 1 and found[0]["type"] == "figure_strip" and found[0]["page"] == 1
    assert found[0]["severity"] == "high" and found[0]["fix"] is None   # re-rendering cannot fix it
    assert "1 image(s) placed as a strip (e.g. 6.5 x 0.16 in)" == found[0]["note"]
    assert rr._strip_image_defects(doc.load_page(1), 2) == []
