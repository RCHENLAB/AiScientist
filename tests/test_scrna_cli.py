"""In-container analysis CLI (Phase 4): dispatch of one step to the same tool code the eyeserver
uses. Dispatch is monkeypatched so these run without scanpy/gseapy or a real .h5ad.
"""

from __future__ import annotations

import json

from bioagent.tools import scrna_cli


def test_run_tool_dispatches_to_scrna_pack_with_ctx(monkeypatch):
    seen = {}

    def fake_qc(args, ctx):
        seen["args"] = args
        seen["ws"] = str(ctx.workspace)
        seen["ds"] = ctx.decisions.get("dataset_path")
        return {"status": "ok", "n": 1}

    monkeypatch.setattr("bioagent.tools.scrna_pack.run_scanpy_qc", fake_qc)
    out = scrna_cli.run_tool("run_scanpy_qc", "/dfs/run", "/dfs/ds.h5ad", {"min_genes": 100})
    assert out == {"status": "ok", "n": 1}
    assert seen == {"args": {"min_genes": 100}, "ws": "/dfs/run", "ds": "/dfs/ds.h5ad"}


def test_unknown_tool_is_error():
    assert scrna_cli.run_tool("nope", "/w", None, {})["status"] == "error"


def test_preflight_dispatches_to_smoke_analysis(monkeypatch):
    called = {}

    def fake_pre(path, out_dir):
        called["path"] = str(path)
        called["out"] = str(out_dir)
        return {"status": "ok", "result": {}}

    monkeypatch.setattr("bioagent.tools.datasets.run_dataset_smoke_analysis", fake_pre)
    out = scrna_cli.run_tool("preflight", "/dfs/run", "/dfs/ds.h5ad", {})
    assert out["status"] == "ok"
    assert called["path"] == "/dfs/ds.h5ad"
    assert called["out"].endswith("/run/artifacts/data")


def test_load_args_inline_and_file(tmp_path):
    assert scrna_cli._load_args('{"a": 1}') == {"a": 1}
    p = tmp_path / "args.json"
    p.write_text('{"b": 2}')
    assert scrna_cli._load_args(str(p)) == {"b": 2}


def test_main_emits_result_marker(monkeypatch, capsys):
    monkeypatch.setattr("bioagent.tools.scrna_pack.run_de", lambda a, c: {"status": "ok", "de": 3})
    rc = scrna_cli.main(["--tool", "run_de", "--workspace", "/w", "--args", "{}"])
    line = capsys.readouterr().out.strip()
    assert rc == 0 and line.startswith(scrna_cli.RESULT_MARKER)
    assert json.loads(line[len(scrna_cli.RESULT_MARKER):]) == {"status": "ok", "de": 3}
