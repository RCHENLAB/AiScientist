"""One label per figure in the PDF.

2026-10-12, run 1e887ed2bf8e: the manuscript writes ``![Figure 1. Violin plot …](figures/x.png)``
(its prose refers to "Figure 1"), and LaTeX labels every figure itself, so the PDF read
"Figure 1: Figure 1. Violin plot …". The render filter now takes the written label off the caption
for LaTeX and has LaTeX print that same number. A caption without a label of its own (the technical
report's) is numbered by LaTeX as before; a DOCX caption is printed as written.

The tests marked ``needs_pandoc`` / ``needs_engine`` run the real programs and skip where they are
absent (CI has neither).
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from aiscientist.reporting import report

needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
ENGINE = next((e for e in ("xelatex", "tectonic") if shutil.which(e)), None)
needs_engine = pytest.mark.skipif(shutil.which("pandoc") is None or ENGINE is None,
                                  reason="pandoc + a TeX engine not installed")

MANUSCRIPT = """## Results

![Figure 1. Violin plot of per-cell QC metrics (n = 16,750).](figures/umap.png)

The workflow used for this run is summarised in Figure 2.

![**Figure 2.** Workflow diagram for the run.](figures/workflow.png)

![Figure S1: A supplementary panel.](figures/umap.png)

![Fig. 4 — Written with a dash.](figures/umap.png)

![Per-cell QC metrics, no label of its own](figures/workflow.png)

![Figure 6 shows a sentence, not a label](figures/workflow.png)

![Figure 7.](figures/workflow.png)

![Figure 8\\input{/etc/hostname}. A number that is not a number.](figures/umap.png)
"""


def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def art(tmp_path) -> Path:
    art = tmp_path / "artifacts"
    (art / "figures").mkdir(parents=True)
    for name in ("umap.png", "workflow.png"):
        (art / "figures" / name).write_bytes(_png())
    return art


def _convert(art: Path, to: str, suffix: str) -> Path:
    """The report's own reader, filter and command line, stopped at ``to`` instead of a PDF."""
    md = art / "in.md"
    md.write_text(MANUSCRIPT, encoding="utf-8")
    lua = art / "filter.lua"
    lua.write_text(report._lua_filter(report._resolve_format(None), report._embeddable_images(art)),
                   encoding="utf-8")
    out = art / f"out.{suffix}"
    cmd = report._markdown_cmd(md, out, lua, ["--to", to, "--resource-path", str(art)])
    done = subprocess.run(cmd, cwd=art, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return out


def test_the_label_fix_runs_after_the_text_is_made_data():
    # Pass 1 drops every raw node; the label fix adds its own raw LaTeX, so it must come after.
    lua = report._lua_filter(report._resolve_format(None), [])
    assert lua.rstrip().endswith("return { sanitize, { Table = fit_table, Figure = relabel_figure } }")
    # What goes into raw LaTeX is matched as digits with an optional S / one letter, nothing else.
    assert "'^(S?%d%d?%d?%a?)([.:]?)$'" in lua


@needs_pandoc
def test_latex_prints_the_written_number_once(art):
    tex = _convert(art, "latex", "tex").read_text(encoding="utf-8")
    captions = re.findall(r"\\caption\{(.*)\}", tex)
    assert captions[:4] == ["Violin plot of per-cell QC metrics (n = 16,750).",
                            "Workflow diagram for the run.",
                            "A supplementary panel.",
                            "Written with a dash."]
    # LaTeX is told which number to print for each of them, and to count on by itself afterwards.
    assert re.findall(r"\\renewcommand\{\\thefigure\}\{(\w+)\}", tex) == ["1", "2", "S1", "4"]
    assert tex.count(r"\renewcommand{\thefigure}{\arabic{figure}}") == 4
    for number in ("1", "2", "S1", "4"):
        block = tex.split(rf"\renewcommand{{\thefigure}}{{{number}}}")[1]
        assert block.index(r"\caption{") < block.index(r"\renewcommand{\thefigure}{\arabic{figure}}")
    # Left alone: no label of its own, a sentence that starts with "Figure 6", a label and nothing else.
    assert captions[4:7] == ["Per-cell QC metrics, no label of its own",
                             "Figure 6 shows a sentence, not a label", "Figure 7."]
    # A "number" carrying a command is not a number: nothing of it is passed to TeX as a command.
    assert "Figure 8" in captions[7] and r"\textbackslash" in captions[7]
    assert r"{8\input" not in tex and r"\input{/etc/hostname}" not in tex


@needs_pandoc
def test_docx_keeps_the_caption_as_written(art):
    docx = _convert(art, "docx", "docx")
    plain = subprocess.run(["pandoc", "--sandbox", "--from=docx", str(docx), "--to=plain"],
                           capture_output=True, text=True, timeout=120).stdout
    assert "Figure 1. Violin plot of per-cell QC metrics" in plain
    assert "Figure 2. Workflow diagram for the run." in plain
    assert "Figure 1: Figure 1" not in plain


@needs_engine
def test_the_pdf_caption_has_one_label(art):
    fitz = pytest.importorskip("fitz")

    def render(cmd, cwd, out_path, timeout_s):
        cmd = [f"--pdf-engine={ENGINE}" if c.startswith("--pdf-engine=") else c for c in cmd]
        return report._run_pandoc(cmd, cwd, out_path, 600)

    out = report.build_pdf_report(MANUSCRIPT, art / "report", title="T", assets_root=art,
                                  formats=("pdf",), render_fn=render)
    assert out["status"] == "ok", out.get("error")
    text = " ".join(" ".join(page.get_text() for page in fitz.open(out["pdf_path"])).split())
    assert "Figure 1: Violin plot of per-cell QC metrics" in text
    assert "Figure 2: Workflow diagram for the run." in text
    assert "Figure S1: A supplementary panel." in text
    assert "Figure 1: Figure 1." not in text and "Figure 2: Figure 2." not in text
    # The unlabelled figure is the fifth in the document and LaTeX numbers it by position.
    assert "Figure 5: Per-cell QC metrics, no label of its own" in text
