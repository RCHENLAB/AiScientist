"""The report text is data: it never reaches TeX as a command, and never pulls in a host file.

2026-10-09. ``build_pdf_report`` handed model-written markdown to ``pandoc ... --pdf-engine=xelatex``
with pandoc's default reader, and to pandoc's DOCX writer with no check on images. Two things
followed, both reproduced with a harmless canary file in the production report image:

- a TeX command in the text (written plainly, marked ``{=latex}``, or inside ``$...$``) ran in the
  engine, which opens any file its user can read and typesets it into the PDF;
- a markdown image pointing at any path was copied into the DOCX byte for byte, image or not.

On the gateway host that user is the service account, which reads every account's saved keys. The
render reached that host through a mock session, a failed HPC3 render (local fallback) and every
re-render of the visual review.

The tests marked ``needs_pandoc`` / ``needs_tex`` run the real programs and skip where they are
absent (CI has neither); each has a control that shows the old command line doing the thing the
fix stops, so a skip or a pass is never mistaken for "could not see it". The rest are offline.
"""

from __future__ import annotations

import ast
import asyncio
import os
import shutil
import struct
import subprocess
import sys
import types
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_slurm_report import FakeHPC, FakeHPCNoOutput  # noqa: E402

import aiscientist.gateway.app as gw  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402
from aiscientist.gateway.slurm_report import SlurmReportRenderer  # noqa: E402
from aiscientist.reporting import report  # noqa: E402

SRC = Path(report.__file__).resolve().parents[1]          # src/aiscientist
CANARY = "CANARYOUTSIDE7f3a"

needs_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
needs_tex = pytest.mark.skipif(
    shutil.which("pandoc") is None or shutil.which("xelatex") is None,
    reason="pandoc + xelatex not installed")


def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def run(tmp_path):
    """One run's artifacts folder with a real figure, and a canary file OUTSIDE it that stands for
    anything else the rendering process can read."""
    art = tmp_path / "runs" / "tester" / "r1" / "artifacts"
    (art / "figures").mkdir(parents=True)
    (art / "tables").mkdir()
    (art / "figures" / "umap.png").write_bytes(_png())
    (art / "tables" / "markers.csv").write_text("gene,score\nRHO,1\n", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text(CANARY + "\n", encoding="utf-8")
    try:
        os.symlink(canary, art / "figures" / "link.png")     # a link out of the run's folder
        linked = True
    except OSError:
        linked = False
    return types.SimpleNamespace(art=art, canary=canary, linked=linked)


def _commands(markdown: str, run, **kw) -> list[list[str]]:
    """The command lines ``build_pdf_report`` would run, without running them."""
    seen: list[list[str]] = []

    def capture(cmd, cwd, out_path, timeout_s):
        seen.append(list(cmd))
        return (False, "captured")

    report.build_pdf_report(markdown, run.art / "report", assets_root=run.art, render_fn=capture, **kw)
    return seen


def _latex(cmd: list[str], cwd: Path) -> str:
    """The LaTeX a PDF command line hands to the engine (same reader, same filter, no engine)."""
    i = cmd.index("-o")
    argv = [a for a in cmd[:i] + ["-t", "latex", "-s"] + cmd[i + 2:] if not a.startswith("--pdf-engine")]
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# Each way report text used to become a TeX command. ``{c}`` is the canary's absolute path.
TEX_ROUTES = {
    "in a paragraph": "A \\input{{{c}}} Z",
    "marked as latex": "A `\\input{{{c}}}`{{=latex}} Z",
    "a latex block": "A\n\n```{{=latex}}\n\\input{{{c}}}\n```\n\nZ",
    "inside a formula": "A $\\text{{\\input{{{c}}}}}$ Z",
    "inside a display formula": "A\n\n$$\\input{{{c}}}$$\n\nZ",
    "a formula spelling the backslash": "A $x^^5cinput{{{c}}}$ Z",
    "a formula opening an environment": "A $\\begin{{verbatim}}{c}\\end{{verbatim}}$ Z",
}


# -- the reader and the filter ---------------------------------------------------------------------

def test_every_render_reads_the_report_through_the_reader_and_the_filter(run):
    cmds = _commands("# Results\n\nbody", run, title="T")
    assert len(cmds) == 2                                       # PDF and DOCX
    for cmd in cmds:
        assert cmd[0] == "pandoc" and f"--from={report.PANDOC_READER}" in cmd
        lua = Path(cmd[cmd.index("--lua-filter") + 1]).read_text(encoding="utf-8")
        assert "RawBlock" in lua and "RawInline" in lua and "math_is_plain" in lua and "IMAGES" in lua
        assert "--sandbox" not in cmd      # it would drop every figure from the DOCX
    assert "-raw_tex" in report.PANDOC_READER and "-raw_attribute" in report.PANDOC_READER


@needs_pandoc
@pytest.mark.parametrize("route", sorted(TEX_ROUTES))
def test_report_text_does_not_reach_tex_as_a_command(run, route):
    md = "# Results\n\n" + TEX_ROUTES[route].format(c=run.canary)
    pdf_cmd = _commands(md, run)[0]
    tex = _latex(pdf_cmd, run.art)
    body = tex[tex.index("\\begin{document}"):]
    assert "\\input" not in body and "\\begin{verbatim}" not in body
    assert "\\textbackslash" in body or "\\^{}" in body         # it is there, as the text it is


@needs_pandoc
def test_control_the_old_command_line_passed_those_commands_through(run):
    """What the fix stops, on this pandoc: the same text through the default reader."""
    for route in ("in a paragraph", "marked as latex", "inside a formula"):
        md = TEX_ROUTES[route].format(c=run.canary)
        proc = subprocess.run(["pandoc", "-f", "markdown", "-t", "latex"], input=md,
                              capture_output=True, text=True, timeout=60)
        assert f"\\input{{{run.canary}}}" in proc.stdout, route


@needs_pandoc
def test_a_formula_in_the_title_is_checked_too(run):
    pdf_cmd = _commands("body", run, title=f"T $\\input{{{run.canary}}}$")[0]
    assert "\\input" not in _latex(pdf_cmd, run.art)


@needs_pandoc
def test_ordinary_math_and_the_runs_own_figure_still_render(run):
    md = ("# Results\n\nSignificant at $p < 0.05$ with $\\log_2$ fold change $\\geq 1$, "
          "$\\alpha = 10^{-5}$ and $\\rho \\approx 0.8 \\pm 0.1$.\n\n"
          "$$\\bar{x} = \\frac{1}{n}\\sum_{i=1}^{n} x_i, \\quad "
          "M = \\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}$$\n\n"
          "![UMAP of the clusters](figures/umap.png){width=85%}\n")
    tex = _latex(_commands(md, run)[0], run.art)
    for formula in ("\\(p < 0.05\\)", "\\(\\log_2\\)", "\\(\\geq 1\\)", "\\(\\alpha = 10^{-5}\\)",
                    "\\frac{1}{n}\\sum_{i=1}^{n} x_i", "\\begin{pmatrix}"):
        assert formula in tex, formula
    assert "{figures/umap.png}" in tex and "\\texttt{\\$" not in tex


def test_the_title_is_one_quoted_string(run):
    title = 'The "stress" signature\nheader-includes: x \\y'
    _commands("body", run, title=title)
    front = (run.art / "report" / "report.md").read_text(encoding="utf-8").split("\n\n")[0]
    assert front.splitlines()[0] == "---" and front.splitlines()[-1] == "---"
    assert len(front.splitlines()) == 3                # one key, whatever the title contains
    assert front.splitlines()[1].startswith('title: "The \\"stress\\" signature\\nheader-includes')


# -- images ----------------------------------------------------------------------------------------

def test_only_the_runs_own_image_files_may_be_embedded(run):
    (run.art / "figures" / "notes.txt").write_text("not an image", encoding="utf-8")
    (run.art / ".hidden").mkdir()
    (run.art / ".hidden" / "x.png").write_bytes(_png())
    assert report._embeddable_images(run.art) == ["figures/umap.png"]   # not the link, not the rest


def _image_report(run) -> str:
    c = run.canary
    return ("# Results\n\n![real](figures/umap.png)\n\n"
            f"![absolute]({c})\n\n![url](file://{c})\n\n"
            f"![upwards](../../../../elsewhere/{c.name})\n\n"
            "![link](figures/link.png)\n\n![table](tables/markers.csv)\n")


@needs_pandoc
def test_the_docx_embeds_no_file_from_outside_the_run(run):
    rep = report.build_pdf_report(_image_report(run), run.art / "report", assets_root=run.art,
                                  formats=("docx",))
    assert rep["status"] == "ok", rep
    with zipfile.ZipFile(rep["docx_path"]) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        assert len(media) == 1 and media[0].endswith(".png")          # the run's own figure
        assert not any(CANARY.encode() in z.read(n) for n in z.namelist())
    assert CANARY not in report.rendered_text(rep["docx_path"])


@needs_pandoc
def test_control_the_old_command_line_embedded_that_file(run):
    md = run.art / "old.md"
    md.write_text(_image_report(run), encoding="utf-8")
    out = run.art / "old.docx"
    subprocess.run(["pandoc", str(md), "-o", str(out), "--resource-path", str(run.art)],
                   cwd=run.art, capture_output=True, text=True, timeout=120)
    with zipfile.ZipFile(out) as z:
        assert any(CANARY.encode() in z.read(n) for n in z.namelist() if n.startswith("word/media/"))


# -- end to end, where a TeX engine exists ---------------------------------------------------------

def _pdf_text(path: str) -> str:
    if shutil.which("pdftotext"):
        return subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True).stdout
    fitz = pytest.importorskip("fitz")
    with fitz.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


@needs_tex
def test_the_rendered_pdf_does_not_contain_the_canary(run):
    md = "# Results\n\n" + "\n\n".join(t.format(c=run.canary) for t in TEX_ROUTES.values())
    rep = report.build_pdf_report(md + "\n\n" + _image_report(run), run.art / "report",
                                  assets_root=run.art, formats=("pdf",))
    assert rep["pdf_path"], rep
    text = _pdf_text(rep["pdf_path"])
    assert "Results" in text and CANARY not in text


# -- the host --------------------------------------------------------------------------------------

@pytest.fixture
def host(tmp_path, monkeypatch):
    """The gateway host, recording every program a render starts on it."""
    started: list[list[str]] = []

    def fake_run(cmd, **kw):
        started.append(list(cmd))
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"rendered")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    monkeypatch.setattr(report.shutil, "which", lambda name: f"/usr/bin/{name}")

    def conn(*, mock: bool = False, remote=None, on_hpc: bool = True):
        st = HPCSettings()
        st.report_on_hpc = on_hpc
        loop = asyncio.new_event_loop()
        c = gw.Connection(st, mock=mock, loop=loop, username="tester")
        loop.close()
        c.workspace = tmp_path / "runs" / "tester"
        c.executor = remote
        return c

    def tex_started() -> bool:
        return any(a.startswith("--pdf-engine") for cmd in started for a in cmd)

    return types.SimpleNamespace(conn=conn, started=started, tex_started=tex_started)


def _render(conn, art: Path) -> dict:
    return report.build_pdf_report("# Results\n\nbody", art / "report", assets_root=art,
                                   render_fn=gw._build_report_render_fn(conn))


ROUTES_TO_THE_HOST = {
    "a mock session": dict(mock=True),
    "report-on-HPC3 switched off": dict(remote=FakeHPC(), on_hpc=False),
    "no live HPC3 session": dict(remote=None),
    "the HPC3 render produced nothing": dict(remote=FakeHPCNoOutput(sacct="FAILED")),
}


@pytest.mark.parametrize("route", sorted(ROUTES_TO_THE_HOST))
def test_with_accounts_tex_never_starts_on_the_gateway_host(host, run, monkeypatch, route):
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr("aiscientist.gateway.slurm_job.time.sleep", lambda s: None)
    rep = _render(host.conn(**ROUTES_TO_THE_HOST[route]), run.art)
    assert not host.tex_started()
    assert rep["pdf_path"] is None and report.NO_HOST_TEX in rep["error"]
    assert rep["docx_path"] and Path(rep["md_path"]).exists()     # the DOCX and the .md are kept


@pytest.mark.parametrize("route", sorted(ROUTES_TO_THE_HOST))
def test_control_without_accounts_the_same_routes_typeset_locally(host, run, monkeypatch, route):
    monkeypatch.setattr(gw, "_AUTH_ENABLED", False)
    monkeypatch.setattr("aiscientist.gateway.slurm_job.time.sleep", lambda s: None)
    rep = _render(host.conn(**ROUTES_TO_THE_HOST[route]), run.art)
    assert host.tex_started() and rep["pdf_path"]


def test_with_accounts_a_live_session_renders_on_hpc3_and_its_fallback_has_no_tex(host, monkeypatch):
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    fn = gw._build_report_render_fn(host.conn(remote=FakeHPC()))
    assert isinstance(fn, SlurmReportRenderer) and fn.local_fallback is report.run_pandoc_without_tex
    assert gw._build_report_render_fn(host.conn(mock=True)) is report.run_pandoc_without_tex


def test_the_preamble_needs_no_package_the_report_image_lacks():
    """``\\usepackage{pdflscape}`` is absent from the production report image: with it required,
    every PDF job on HPC3 failed (22 of 22 manuscripts in one account's job logs) and the PDF was
    typeset by the fallback on the gateway host instead. Without the fallback there would be none."""
    header = report._table_header_tex(report.DEFAULT_FORMAT)
    assert "\\IfFileExists{pdflscape.sty}{\\usepackage{pdflscape}}{\\usepackage{lscape}}" in header
    assert "\n\\usepackage{pdflscape}" not in "\n" + header


def test_the_refusal_starts_nothing(host, tmp_path):
    ok, err = report.run_pandoc_without_tex(
        ["pandoc", "x.md", "-o", str(tmp_path / "x.pdf"), "--pdf-engine=xelatex"],
        tmp_path, tmp_path / "x.pdf", 10.0)
    assert (ok, err) == (False, report.NO_HOST_TEX) and host.started == []
    ok, _ = report.run_pandoc_without_tex(
        ["pandoc", "x.md", "-o", str(tmp_path / "x.docx")], tmp_path, tmp_path / "x.docx", 10.0)
    assert ok and len(host.started) == 1


# -- nothing renders around the boundary -----------------------------------------------------------

def _functions_with(tree: ast.AST, wanted) -> list[str]:
    """Names of the functions (or '<module>') that directly contain a node ``wanted`` accepts."""
    found: list[str] = []

    def visit(node: ast.AST, owner: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if wanted(node):
            found.append(owner)
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, "<module>")
    return found


def _is_pandoc_argv(node: ast.AST) -> bool:
    return (isinstance(node, (ast.List, ast.Tuple)) and node.elts
            and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "pandoc")


def test_every_pandoc_command_line_is_built_in_the_report_module():
    """A new feature that runs pandoc on report text must go through ``_markdown_cmd`` (the reader
    and the filter) or ``rendered_text`` (--sandbox): no other command line exists in the package."""
    where = {}
    for path in sorted(SRC.rglob("*.py")):
        owners = _functions_with(ast.parse(path.read_text(encoding="utf-8")), _is_pandoc_argv)
        if owners:
            where[path.relative_to(SRC).as_posix()] = sorted(owners)
    assert where == {"reporting/report.py": ["_markdown_cmd", "rendered_text"]}

    built = report._markdown_cmd(Path("r.md"), Path("r.docx"), Path("f.lua"), ["--toc"])
    assert built[:2] == ["pandoc", f"--from={report.PANDOC_READER}"] and "--lua-filter" in built
    source = (SRC / "reporting" / "report.py").read_text(encoding="utf-8")
    assert '["pandoc", "--sandbox", "--from=docx"' in source          # rendered_text


def test_every_report_render_in_the_gateway_goes_through_the_connections_renderer():
    """``build_pdf_report`` without ``render_fn`` renders with local pandoc and TeX whatever the
    session (the visual review's re-render did exactly that)."""
    def renders_report(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        direct = isinstance(node.func, ast.Name) and node.func.id == "build_pdf_report"
        threaded = (isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"
                    and node.args and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "build_pdf_report")
        return direct or threaded

    calls = []
    for path in sorted((SRC / "gateway").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if renders_report(node):
                calls.append((path.name, node.lineno, {k.arg for k in node.keywords}))
    assert len(calls) >= 3                                   # manuscript, technical report, re-render
    assert all("render_fn" in kws for _name, _line, kws in calls), calls
