"""Offline tests for deterministic schematic figures (tools/schematic).

graphviz `dot` is a system binary that need not be present here:
these tests cover the pure DOT builder and the graceful "renderer absent" path, plus
the simulated-success path (monkeypatching subprocess), and the harness tool shape.
The tests marked ``needs_dot`` render with the real program and skip where it is absent.

2026-10-12, run 1e887ed2bf8e: the report's workflow figure was 14,312 x 342 px (42:1) and printed
as a strip under 4 mm tall, because each box held its step's whole paragraph on one line. A box now
holds the step's number and title, a long plan is folded into rows, and a rendered figure reports
its size.
"""

from __future__ import annotations

import re
import shutil
import struct
import types
import zlib

import pytest

from aiscientist.tools.make_schematic import tool as schematic

needs_dot = pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz not installed")

# The first step of run 1e887ed2bf8e, shortened: a bold title, then a paragraph.
LONG_STEP = ("**CellQC quality control** \u2014 Run `run_cellqc` on the Sample1_WT Cell Ranger folder, "
             "which holds one library with both raw and filtered matrices. " + "The barcode is the unit. " * 60)


def _plan(n: int) -> list[str]:
    return [f"**Step title number {i + 1}** \u2014 " + "a long description of what the step does. " * 12
            for i in range(n)]


def _png_header(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b""))


def _labels(dot: str) -> dict[int, str]:
    return {int(i): label for i, label in re.findall(r'n(\d+) \[label="((?:[^"\\]|\\.)*)"', dot)}


def _arrows(dot: str) -> set[tuple[int, int]]:
    """The arrows a reader sees, as (from, to): an edge written backwards is drawn backwards."""
    seen = set()
    for a, b, attrs in re.findall(r"n(\d+) -> n(\d+)(?: \[([^\]]*)\])?;", dot):
        if "invis" in attrs:
            continue
        seen.add((int(b), int(a)) if "dir=back" in attrs else (int(a), int(b)))
    return seen


def test_workflow_dot_is_deterministic_and_ordered():
    steps = ["QC", "Clustering", "DE per cluster", "Enrichment"]
    a = schematic.workflow_schematic_dot(steps, title="Research workflow")
    b = schematic.workflow_schematic_dot(steps, title="Research workflow")
    assert a == b                                   # deterministic: same input → same DOT
    assert a.startswith("digraph workflow {") and a.rstrip().endswith("}")
    assert _arrows(a) == {(0, 1), (1, 2), (2, 3)}   # one arrow per consecutive pair, in plan order
    assert _labels(a)[2] == "3. DE per cluster"     # numbered, label preserved
    assert 'label="Research workflow"' in a


def test_dot_label_quotes_are_escaped():
    dot = schematic.workflow_schematic_dot(['say "hi"'])
    assert '\\"hi\\"' in dot                         # embedded quotes escaped, DOT stays valid


def test_dot_label_backslashes_are_escaped():
    # In a DOT label a backslash starts an escape: "\N" would print the node's own name.
    dot = schematic.workflow_schematic_dot(["Read C:\\Newdata"])
    assert "C:\\\\Newdata" in dot


def test_a_box_holds_the_step_title_not_its_paragraph():
    dot = schematic.workflow_schematic_dot([LONG_STEP, "**Mouse retina literature grounding** - Run `deep_literature` ..."])
    labels = _labels(dot)
    assert labels[0] == "1. CellQC quality\\ncontrol"
    assert labels[1] == "2. Mouse retina\\nliterature grounding"
    assert "run_cellqc" not in dot and "*" not in dot and "`" not in dot
    # No line of any box is longer than the wrap width: the box cannot grow with the step's text.
    for label in labels.values():
        assert all(len(row) <= schematic._LABEL_WRAP for row in label.split("\\n"))


def test_a_step_without_a_bold_title_shows_its_first_clause_shortened():
    step = ("Using `run_code`, load the 23 per-cluster DE tables (de_leiden_0.csv through de_leiden_22.csv) "
            "from the artifacts directory, and for the 16 clusters labelled 'Bipolar cell' compute ...")
    assert schematic.step_title("3. **QC removal ledger** \u2014 Reconcile ...") == "QC removal ledger"
    assert schematic.step_title("Cluster the cells. Then annotate them.") == "Cluster the cells"
    rows = _labels(schematic.workflow_schematic_dot([step]))[0].split("\\n")
    assert rows[0].startswith("1. Using run_code, load")
    assert len(rows) == schematic._LABEL_LINES and rows[-1].endswith("\u2026")


def test_a_long_plan_is_folded_into_rows():
    assert [schematic.boxes_per_row(n) for n in (1, 3, 4, 8, 9, 12, 20, 27, 28, 40)] == \
        [1, 1, 2, 2, 3, 3, 3, 3, 4, 5]
    assert "rank=same" not in schematic.workflow_schematic_dot(_plan(3))          # one column
    dot = schematic.workflow_schematic_dot(_plan(8), per_row=3)
    rows = [[int(i) for i in re.findall(r"n(\d+);", group)]
            for group in re.findall(r"\{ rank=same; ([^}]*)\}", dot)]
    assert rows == [[0, 1, 2], [3, 4, 5], [6, 7]]
    # Read as a snake: every arrow still joins a step to the next one, and the second row is
    # written right to left (tail first), which is what puts step 4 under step 3.
    assert _arrows(dot) == {(i, i + 1) for i in range(7)}
    assert "n4 -> n3 [dir=back];" in dot and "n5 -> n4 [dir=back];" in dot
    assert "n2 -> n3;" in dot and "n5 -> n6;" in dot                               # the turns
    assert "n1 -> n4 [style=invis];" in dot and "n0 -> n5 [style=invis];" in dot   # columns held


def test_render_dot_graceful_when_binary_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(schematic.shutil, "which", lambda _n: None)
    out = schematic.render_dot("digraph { A -> B }", tmp_path, basename="wf")
    assert out["status"] == "dependency_missing" and out["dependency"] == "graphviz"
    assert out["path"] is None
    assert (tmp_path / "wf.dot").read_text(encoding="utf-8") == "digraph { A -> B }"  # source always kept


def test_render_dot_ok_when_dot_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(schematic.shutil, "which", lambda _n: "/usr/bin/dot")

    def fake_run(cmd, input=None, text=None, capture_output=None, timeout=None):  # noqa: ANN001
        out_path = cmd[cmd.index("-o") + 1]
        open(out_path, "wb").write(b"\x89PNG-fake")    # emulate dot producing the png
        assert cmd[0] == "dot" and cmd[1] == "-Tpng"
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(schematic.subprocess, "run", fake_run)
    out = schematic.render_dot("digraph { A -> B }", tmp_path, basename="wf")
    assert out["status"] == "ok" and out["path"].endswith("wf.png")
    assert "width" not in out and "warnings" not in out   # not a readable PNG: nothing is claimed


def _fake_dot(monkeypatch, size_of):
    """A `dot` that writes a PNG whose size is ``size_of(dot_source)``; returns the sources it got."""
    sources: list[str] = []
    monkeypatch.setattr(schematic.shutil, "which", lambda _n: "/usr/bin/dot")

    def fake_run(cmd, input=None, **_k):  # noqa: A002, ANN001
        sources.append(input)
        with open(cmd[cmd.index("-o") + 1], "wb") as fh:
            fh.write(_png_header(*size_of(input)))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(schematic.subprocess, "run", fake_run)
    return sources


def test_render_dot_reports_the_size_and_warns_about_a_strip(tmp_path, monkeypatch):
    _fake_dot(monkeypatch, lambda _src: (800, 600))
    out = schematic.render_dot("digraph { A -> B }", tmp_path, basename="ok")
    assert (out["width"], out["height"]) == (800, 600) and "warnings" not in out

    _fake_dot(monkeypatch, lambda _src: (14312, 342))            # the production figure
    out = schematic.render_dot("digraph { A -> B }", tmp_path, basename="strip")
    assert out["status"] == "ok" and (out["width"], out["height"]) == (14312, 342)
    assert "14312 x 342 px (41.8:1)" in out["warnings"][0] and "strip" in out["warnings"][0]

    _fake_dot(monkeypatch, lambda _src: (300, 2401))             # the same, standing up
    out = schematic.render_dot("digraph { A -> B }", tmp_path, basename="tall")
    assert "(1:8.0)" in out["warnings"][0]
    _fake_dot(monkeypatch, lambda _src: (300, 2400))             # exactly 8:1 is still a figure
    assert "warnings" not in schematic.render_dot("digraph { A -> B }", tmp_path, basename="edge")


def test_the_workflow_figure_is_laid_out_again_when_it_comes_out_too_tall(tmp_path, monkeypatch):
    # Three steps start as one column. Here that column renders 1:5 (as on a host with a much
    # narrower font), so it is drawn again two to a row, and that layout is the one kept.
    sources = _fake_dot(monkeypatch, lambda src: (800, 600) if "rank=same" in src else (200, 1000))
    out = schematic.render_workflow_schematic(_plan(3), tmp_path, title="Research workflow")
    assert out["status"] == "ok" and out["per_row"] == 2 and len(sources) == 2
    assert (out["width"], out["height"]) == (800, 600)
    assert (tmp_path / "workflow.dot").read_text(encoding="utf-8") == sources[-1]   # the kept layout
    # The resolution is written into the file, once: TeX then prints the figure at its drawn size.
    data = (tmp_path / "workflow.png").read_bytes()
    assert data.count(b"pHYs") == 1 and data[37:41] == b"pHYs"
    assert struct.unpack(">IIB", data[41:50]) == (7559, 7559, 1)                 # 192 dpi, per metre
    schematic._record_png_resolution(tmp_path / "workflow.png", 192)
    assert (tmp_path / "workflow.png").read_bytes() == data


def test_the_workflow_figure_never_loops_between_two_layouts(tmp_path, monkeypatch):
    # Too tall in one column, too wide in two: each layout is tried once and the last one stays.
    sources = _fake_dot(monkeypatch, lambda src: (5000, 600) if "rank=same" in src else (200, 1000))
    out = schematic.render_workflow_schematic(_plan(3), tmp_path)
    assert out["status"] == "ok" and len(sources) == 2 and out["per_row"] == 2


def test_the_workflow_figure_keeps_its_dot_source_without_graphviz(tmp_path, monkeypatch):
    monkeypatch.setattr(schematic.shutil, "which", lambda _n: None)
    out = schematic.render_workflow_schematic(_plan(5), tmp_path)
    assert out["status"] == "dependency_missing" and out["per_row"] == 2
    assert (tmp_path / "workflow.dot").exists()


@needs_dot
@pytest.mark.parametrize("n_steps", [1, 2, 3, 4, 5, 8, 12, 20, 30])
def test_the_rendered_workflow_figure_fits_a_page(tmp_path, n_steps):
    """Real graphviz, steps as long as a real plan's: the figure stays between 1:2 and 4:1."""
    out = schematic.render_workflow_schematic(_plan(n_steps), tmp_path, title="Research workflow")
    assert out["status"] == "ok" and "warnings" not in out
    ratio = out["width"] / out["height"]
    assert 0.5 <= ratio <= 4.0, (n_steps, out["width"], out["height"])
    assert out["width"] <= 2600                    # 13.5 in at 192 dpi: no box grew with its text


@needs_dot
def test_the_production_strip_is_gone(tmp_path):
    """Run 1e887ed2bf8e: three steps of 500 to 1,900 characters gave 14,312 x 342 px."""
    steps = [LONG_STEP, "**Mouse retina literature grounding** \u2014 " + "Run `deep_literature`. " * 25,
             "**Ambient rod-leak audit** \u2014 " + "Using the normalised checkpoint. " * 16]
    out = schematic.render_workflow_schematic(steps, tmp_path, title="Research workflow")
    assert out["per_row"] == 1 and out["width"] < 700 and out["height"] > out["width"]


def test_make_schematic_tool_renders_into_figure_gallery(tmp_path, monkeypatch):
    monkeypatch.setattr(schematic.shutil, "which", lambda _n: "/usr/bin/dot")
    monkeypatch.setattr(schematic.subprocess, "run",
                        lambda cmd, **k: (open(cmd[cmd.index("-o") + 1], "wb").write(b"PNG"),
                                          types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1])
    tool = schematic.make_schematic_tool()
    assert tool.name == "make_schematic" and tool.reads_private_data is False
    ctx = types.SimpleNamespace(workspace=tmp_path)
    out = tool.executor({"source": "digraph { A -> B }", "basename": "path/way!"}, ctx)
    assert out["status"] == "ok"
    # lands under artifacts/figures with a sanitized, namespaced filename
    assert out["figure"].startswith("figures/schematic_pathway")
    assert (tmp_path / "artifacts" / "figures").is_dir()


def test_make_schematic_draws_dot_only(tmp_path):
    """D2 and Mermaid were offered once, but neither binary was ever on the gateway: every such call
    failed. The model is now offered DOT alone, and an old caller asking for another engine is told
    to write DOT instead of getting a dependency error."""
    tool = schematic.make_schematic_tool()
    assert "engine" not in tool.parameters["properties"]
    for engine in ("d2", "mermaid"):
        out = tool.executor({"source": "a -> b", "engine": engine}, types.SimpleNamespace(workspace=tmp_path))
        assert out["status"] == "error" and "Graphviz DOT" in out["error"]
    assert not (tmp_path / "artifacts").exists()                  # nothing was written


def test_make_schematic_tool_rejects_empty_source(tmp_path):
    tool = schematic.make_schematic_tool()
    out = tool.executor({"source": "  "}, types.SimpleNamespace(workspace=tmp_path))
    assert out["status"] == "error"
