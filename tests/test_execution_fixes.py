"""Execution-layer defects found by auditing 17 prod runs / 1,402 tool calls (2026-09-28).

Each test replays the failing shape: a returned error that never tripped the stuck-guard, a
model-typed HPC3 path overriding the bound dataset, a tool-less analysis step sent to the literature
path on the word "reference", a hard-coded home directory nobody has, a reference root that does
not exist, and scGPT annotating a mouse dataset on 17 genes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from test_research_harness import _ctx, _scripted, _tool_call  # noqa: E402

from aiscientist.agents.research_harness import HarnessTool, ResearchHarness, default_catalog  # noqa: E402
from aiscientist.agents.research_lab import _is_literature_step  # noqa: E402
from aiscientist.gateway.executor import ExecResult  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402


# --- 1. a RETURNED error counts toward the stuck-guard ------------------------------------------

def test_a_tool_that_returns_the_same_error_bails_the_step():
    def fails(_a, _c):
        return {"status": "error", "error": "KeyError: 'log2fc' (line 12)"}

    tool = HarnessTool("run_code", "returns an error", {"type": "object", "properties": {}}, fails)
    harness = ResearchHarness(catalog=[tool, *[t for t in default_catalog() if t.name == "finish"]],
                              chat_fn=_scripted([_tool_call("run_code", {}) for _ in range(6)]))
    result = harness.run("do it", _ctx())
    assert result.stop_reason == "repeated_tool_errors"
    assert sum(1 for s in result.steps if s.get("tool") == "run_code") == 3


def test_different_returned_errors_are_debugging_not_stuck():
    n = {"i": 0}

    def fails(_a, _c):
        n["i"] += 1
        return {"status": "error", "error": ["SyntaxError", "KeyError", "ValueError", "TypeError"][n["i"] % 4]}

    tool = HarnessTool("run_code", "varied", {"type": "object", "properties": {}}, fails)
    harness = ResearchHarness(catalog=[tool, *[t for t in default_catalog() if t.name == "finish"]],
                              chat_fn=_scripted([_tool_call("run_code", {"i": i}) for i in range(4)]
                                                + [_tool_call("finish", {"answer": "done"})]))
    assert harness.run("do it", _ctx()).stop_reason != "repeated_tool_errors"


# --- 2. inspect_dataset: the bound dataset wins over a path this host does not have -------------

def test_bound_dataset_wins_over_a_model_typed_hpc3_path(monkeypatch, tmp_path):
    from aiscientist.tools.inspect_dataset import tool as dataset_inspect
    bound = tmp_path / "bound.h5ad"
    bound.write_bytes(b"x")
    seen = []
    monkeypatch.setattr(dataset_inspect, "inspect_dataset", lambda path, chat_fn=None: seen.append(path) or {})
    tool = next(t for t in [dataset_inspect.make_inspect_dataset_tool()] if t.name == "inspect_dataset")
    ctx = SimpleNamespace(decisions={"dataset_path": str(bound)}, tunnel_port=None, model="m")
    tool.executor({"path": "/dfs3b/ruic20_lab/software/AiScientist/uploads/u/Ddx41.h5ad"}, ctx)
    other = tmp_path / "other.h5ad"
    other.write_bytes(b"y")
    tool.executor({"path": str(other)}, ctx)                 # a real local file the model points at
    assert seen == [str(bound), str(other)]


# --- 3. literature routing reads the step's headline -------------------------------------------

def test_tool_less_analysis_steps_are_not_literature_steps():
    # rounds 12 and 24 of run 3c5fbc8608a7, which were routed to the literature path
    assert not _is_literature_step("**Reconcile transferred and original annotations** — align predictions by "
                                   "barcode; compare against the reference taxonomy and reference labels.")
    assert not _is_literature_step("**Reconcile evidence and generate the final report** — build a claim-to-"
                                   "evidence table with QC, pathways and literature context.")
    assert not _is_literature_step("Reference-based label transfer against the retina atlas")
    assert _is_literature_step("**Ground provisional mechanisms in literature** — investigate the candidates.")
    assert _is_literature_step("Search the literature for DDX41 in the retina")


# --- 4. the session's real home and the lab's real reference root ------------------------------

def test_shell_roots_use_the_real_home_and_reference(monkeypatch):
    from aiscientist.gateway import app as gw

    class Ex:
        username = "yijus12"

        def exec(self, cmd, timeout=None):
            out = "/data/homezvol3/yijus12" if "$HOME" in cmd else ""
            return ExecResult(command=cmd, exit_status=0, stdout=out, stderr="")

    conn = SimpleNamespace(executor=Ex(), mock=False, settings=HPCSettings(), owner="BioAdmin",
                           emit=lambda *a, **k: None, hpc_pysrc=None, id="c1")
    monkeypatch.setattr(gw, "_hpc_user", lambda c: "yijus12")
    assert gw._hpc_home(conn) == "/data/homezvol3/yijus12"
    captured = {}

    class FakeShell:
        def __init__(self, **kw):
            captured.update(kw)

    import aiscientist.hpc.shell as hs
    monkeypatch.setattr(hs, "HpcShell", FakeShell)
    try:
        gw._build_hpc_shell(conn, None)
    except Exception:  # noqa: BLE001 - only the roots handed to the shell matter here
        pass
    ws = captured.get("workspace")
    assert ws is not None, "the shell was never built — the roots were not checked"
    if ws is not None:
        assert "/data/homezvol3/yijus12" in ws.read_roots and all("homezvol0" not in r for r in ws.read_roots)
        assert f"{HPCSettings().lab_storage}/software/reference" in ws.read_roots
        assert not any(r.endswith("AiScientist/reference") for r in ws.read_roots)


def test_unknown_home_is_left_out_not_guessed():
    from aiscientist.gateway import app as gw

    class Ex:
        def exec(self, cmd, timeout=None):
            raise OSError("channel closed")

    assert gw._hpc_home(SimpleNamespace(executor=Ex())) == ""
