"""Tests for the deterministic report renderer (PDF + DOCX; no AI, no real pandoc)."""

from __future__ import annotations

import types
from pathlib import Path

from bioagent.tools import report


def test_markdown_only_when_pandoc_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda _name: None)
    out = report.build_pdf_report("# Hello\n\nbody text", tmp_path, title="My Report")
    assert out["status"] == "markdown_only" and out["pdf_path"] is None and out["docx_path"] is None
    md = Path(out["md_path"]).read_text(encoding="utf-8")
    assert "# Hello" in md and 'title: "My Report"' in md


def test_ok_renders_both_pdf_and_docx(tmp_path, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda _name: "/usr/bin/pandoc")

    def fake_run(cmd, cwd=None, capture_output=True, text=True, timeout=None):  # noqa: ANN001
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"%fake")                 # emulate pandoc producing the file
        if out_path.suffix == ".pdf":
            assert "--pdf-engine=xelatex" in cmd        # PDF uses xelatex
        assert "--resource-path" in cmd                 # figures resolve from the bundle root
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    out = report.build_pdf_report("# Hi", tmp_path)
    assert out["status"] == "ok"
    assert Path(out["pdf_path"]).exists() and Path(out["docx_path"]).suffix == ".docx"


def test_pdf_failed_when_every_format_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda _name: "/usr/bin/pandoc")
    monkeypatch.setattr(report.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="! LaTeX Error: x"))
    out = report.build_pdf_report("# Hi", tmp_path)
    assert out["status"] == "pdf_failed" and out["pdf_path"] is None and out["docx_path"] is None
    assert "LaTeX Error" in out["error"]
    assert Path(out["md_path"]).exists()   # the .md remains even when rendering fails


def test_docx_only_format_skips_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda _name: "/usr/bin/pandoc")

    def fake_run(cmd, cwd=None, capture_output=True, text=True, timeout=None):  # noqa: ANN001
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"%fake")
        assert "--pdf-engine=xelatex" not in cmd        # only docx requested
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    out = report.build_pdf_report("# Hi", tmp_path, formats=("docx",))
    assert out["status"] == "ok" and out["pdf_path"] is None and Path(out["docx_path"]).exists()
