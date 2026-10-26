"""A figure that would print as a strip is kept out of the rendered reports.

2026-10-12, run 1e887ed2bf8e: ``figures/workflow.png`` was 14,312 x 342 px (42:1). Scaled to the
page it printed 6.5 in wide and under 4 mm tall, the manuscript embedded it as Figure 2, and the
visual review returned ``{"clean": true}``. The shape of an image is in its header, so the check is
arithmetic: such a figure is not offered to the report writers, an embed of it is removed before
rendering, and what was left out is recorded in ``process/figure_checks.md``.

All offline: the PNG files here are a header and one pixel row.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from aiscientist.reporting import figure_checks as fc


def _png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b""))


@pytest.fixture
def art(tmp_path) -> Path:
    """One run's artifacts: the production strip, the same standing up, and two printable figures."""
    art = tmp_path / "runs" / "tester" / "r1" / "artifacts"
    figs = art / "figures"
    figs.mkdir(parents=True)
    (figs / "workflow.png").write_bytes(_png(14312, 342))
    (figs / "ladder.png").write_bytes(_png(300, 2700))
    (figs / "umap.png").write_bytes(_png(1600, 1200))
    (figs / "violin_qc.png").write_bytes(_png(2045, 414))          # 4.9:1, wide but printable
    (figs / "broken.png").write_bytes(b"not a png")
    return art


def test_png_size_reads_the_header_only(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(_png(14312, 342))
    assert fc.png_size(f) == (14312, 342)
    f.write_bytes(b"\x89PNG\r\n\x1a\n")                              # cut off after the signature
    assert fc.png_size(f) is None
    f.write_bytes(b"GIF89a" + b"\x00" * 40)
    assert fc.png_size(f) is None
    assert fc.png_size(tmp_path / "missing.png") is None


def test_the_limit_is_eight_to_one_either_way():
    assert fc.is_strip(801, 100) and fc.is_strip(100, 801)
    assert not fc.is_strip(800, 100) and not fc.is_strip(100, 800)
    assert not fc.is_strip(0, 100)
    assert fc.aspect_text(14312, 342) == "41.8:1" and fc.aspect_text(300, 2700) == "1:9.0"


def test_strips_are_found_and_the_rest_stays_embeddable(art):
    strips = fc.strip_figures(art)
    assert [s["figure"] for s in strips] == ["figures/ladder.png", "figures/workflow.png"]
    assert strips[1] == {"figure": "figures/workflow.png", "width": 14312, "height": 342,
                         "aspect": "41.8:1"}
    # A file whose size cannot be read is kept: the check refuses only what it has measured.
    assert [f.name for f in fc.embeddable_figures(art)] == ["broken.png", "umap.png", "violin_qc.png"]
    assert fc.strip_figures(art.parent / "no_such_run") == []
    assert fc.embeddable_figures(art.parent / "no_such_run") == []


def test_an_embed_of_a_strip_is_removed_and_nothing_else(art, tmp_path):
    (tmp_path / "outside.png").write_bytes(_png(9000, 100))
    md = (
        "## Results\n\n"
        "![Figure 1. UMAP of the retained cells (n = 16,750).](figures/umap.png)\n\n"
        "The workflow is summarised in Figure 2.\n\n"
        "![Figure 2. Workflow diagram for the CellQC run.](figures/workflow.png)\n\n"
        "![Tall one, cells [n = 5] and a \\] bracket](figures/ladder.png){width=85%}\n\n"
        "![QC violins](figures/violin_qc.png)\n\n"
        "![Not ours](../../../../outside.png) ![Remote](https://example.org/strip.png)\n\n"
        "![Missing](figures/none.png) ![Unreadable](figures/broken.png)\n\n"
        "## Discussion\n\nText.\n"
    )
    out, removed = fc.remove_strip_embeds(md, art)
    assert removed == ["figures/workflow.png", "figures/ladder.png"]
    assert "workflow.png" not in out and "ladder.png" not in out and "{width=85%}" not in out
    for kept in ("figures/umap.png", "figures/violin_qc.png", "outside.png", "example.org/strip.png",
                 "figures/none.png", "figures/broken.png", "summarised in Figure 2", "## Discussion"):
        assert kept in out
    assert "\n\n\n" not in out                                        # no hole where an embed was
    # Nothing to remove: the text comes back as it went in.
    plain = "## Results\n\n![Figure 1. UMAP.](figures/umap.png)\n\n\n\nText.\n"
    assert fc.remove_strip_embeds(plain, art) == (plain, [])
    assert fc.remove_strip_embeds("", art) == ("", [])


def test_the_note_is_written_and_a_stale_one_is_removed(art):
    strips = fc.strip_figures(art)
    note = fc.write_note(art, strips)
    assert note == art / "process" / "figure_checks.md"
    text = note.read_text(encoding="utf-8")
    assert "| `figures/workflow.png` | 14,312 x 342 | 41.8:1 | left out of the reports |" in text
    assert "figures/ladder.png" in text and "umap.png" not in text
    # The same run rendered again after its figure was fixed: the old note must not stay behind.
    assert fc.write_note(art, []) is None and not note.exists()
    assert fc.write_note(art, []) is None                              # and no error when absent


def test_the_technical_report_gets_a_diagnostics_block(art):
    block = fc.diagnostics_block(fc.strip_figures(art))
    assert block.startswith("### Figures left out of the reports")
    assert "| `figures/workflow.png` | 14,312 x 342 | 41.8:1 |" in block
    assert fc.diagnostics_block([]) == ""


# --- the gateway: who is offered which figures, and the last check before a render ---------------

gw = pytest.importorskip("aiscientist.gateway.app", reason="gateway needs fastapi")


def _emits():
    seen: list[tuple[str, str]] = []
    return seen, lambda level, _src, text: seen.append((level, text))


def test_the_run_records_what_it_left_out(art):
    seen, emit = _emits()
    diag = gw._check_figure_shapes(art, emit)
    assert "figures/workflow.png" in diag and "figures/ladder.png" in diag
    assert (art / "process" / "figure_checks.md").exists()
    assert len(seen) == 1 and seen[0][0] == "warning"
    assert "2 figure(s) left out of the reports" in seen[0][1] and "figures/workflow.png (41.8:1)" in seen[0][1]


def test_a_run_with_printable_figures_says_nothing(art):
    (art / "figures" / "workflow.png").write_bytes(_png(503, 676))
    (art / "figures" / "ladder.png").unlink()
    seen, emit = _emits()
    assert gw._check_figure_shapes(art, emit) == "" and seen == []
    assert not (art / "process" / "figure_checks.md").exists()


def test_the_writers_are_offered_the_printable_figures_only(art, monkeypatch):
    assert [f.name for f in gw._report_figures(art)] == ["broken.png", "umap.png", "violin_qc.png"]

    prompts: list[str] = []

    def writer(messages):
        prompts.append(messages[-1]["content"])
        return "# Title\n\n## Abstract\n\nText.\n\n![Figure 1. UMAP.](figures/umap.png)\n"

    report = gw._build_report("QC kept 16,750 of 18,681 barcodes.", art, writer, "Run QC.")
    offered = prompts[0].split("Figures available")[1].split("Data-table previews")[0]
    assert "figures/umap.png" in offered and "figures/violin_qc.png" in offered
    assert "workflow.png" not in offered and "ladder.png" not in offered
    # The file is still part of the run: the output index lists every figure.
    index = report.split("# Output Files Index")[1]
    assert "`figures/workflow.png`" in index and "`figures/umap.png`" in index

    review_prompts: list[str] = []
    gw._review_report("draft", art, lambda m: review_prompts.append(m[-1]["content"]) or "draft", "Run QC.")
    assert "figures/umap.png" in review_prompts[0] and "workflow.png" not in review_prompts[0]


def test_the_fallback_gallery_skips_a_strip(art):
    md = gw._assemble_report_md("Synthesis.", art)
    assert "](figures/umap.png)" in md and "workflow.png" not in md and "ladder.png" not in md


def test_the_last_check_before_rendering_removes_the_embed(art):
    seen, emit = _emits()
    md = "Text.\n\n![Figure 2. Workflow diagram.](figures/workflow.png)\n\n![Figure 1. UMAP.](figures/umap.png)\n"
    out = gw._drop_strip_embeds(md, art, emit, "report")
    assert "workflow.png" not in out and "](figures/umap.png)" in out
    assert seen == [("warning", "Removed 1 figure embed(s) from the report — too elongated to print: "
                                "figures/workflow.png.")]
    seen.clear()
    assert gw._drop_strip_embeds(out, art, emit, "report") == out and seen == []
