"""Offline tests for deterministic schematic figures (tools/schematic).

graphviz `dot` / mermaid `mmdc` are system binaries that need not be present here:
these tests cover the pure DOT builder and the graceful "renderer absent" path, plus
the simulated-success path (monkeypatching subprocess), and the harness tool shape.
"""

from __future__ import annotations

import types

from bioagent.tools import schematic


def test_workflow_dot_is_deterministic_and_ordered():
    steps = ["QC", "Clustering", "DE per cluster", "Enrichment"]
    a = schematic.workflow_schematic_dot(steps, title="Research workflow")
    b = schematic.workflow_schematic_dot(steps, title="Research workflow")
    assert a == b                                   # deterministic: same input → same DOT
    assert a.startswith("digraph workflow {") and a.rstrip().endswith("}")
    assert "n0 -> n1;" in a and "n2 -> n3;" in a    # ordered edges
    assert '"DE per cluster"' in a                  # labels preserved
    assert 'label="Research workflow"' in a


def test_dot_label_quotes_are_escaped():
    dot = schematic.workflow_schematic_dot(['say "hi"'])
    assert '\\"hi\\"' in dot                         # embedded quotes escaped, DOT stays valid


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


def test_render_d2_graceful_when_binary_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(schematic.shutil, "which", lambda _n: None)
    out = schematic.render_d2("a -> b", tmp_path, basename="wf")
    assert out["status"] == "dependency_missing" and out["dependency"] == "d2"
    assert (tmp_path / "wf.d2").read_text(encoding="utf-8") == "a -> b"   # source always kept


def test_make_schematic_d2_engine_outputs_svg(tmp_path, monkeypatch):
    monkeypatch.setattr(schematic.shutil, "which", lambda _n: "/usr/local/bin/d2")
    monkeypatch.setattr(schematic.subprocess, "run",
                        lambda cmd, **k: (open(cmd[-1], "w").write("<svg/>"),
                                          types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1])
    tool = schematic.make_schematic_tool()
    out = tool.executor({"source": "QC -> DE", "engine": "d2", "basename": "wf"},
                        types.SimpleNamespace(workspace=tmp_path))
    assert out["status"] == "ok" and out["figure"].endswith(".svg")   # D2 → native SVG


def test_make_schematic_tool_rejects_empty_source(tmp_path):
    tool = schematic.make_schematic_tool()
    out = tool.executor({"source": "  "}, types.SimpleNamespace(workspace=tmp_path))
    assert out["status"] == "error"
