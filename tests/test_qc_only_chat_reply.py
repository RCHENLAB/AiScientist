"""A run that only did quality control: answered in the chat, or with the report the model writes.

Prod run 6275df1213ab, 2026-10-14: "Do only cellqc quality controll part and generate the report and
result to me" planned one step, ran CellQC, and then wrote up like a study: a model-written report
(Summary, Methods, Results, Limitations, Conclusion) as the chat reply, a claim audit asking of each QC
count whether it claimed causation in a two-arm comparison, a rendered PDF/DOCX, a References file and
a verification statement about literature and cell-type labels.

Yijun, 2026-10-16 and 2026-10-18: with no written report asked for, the answer is a short reply in the
chat and the downloaded folder is still complete; with one asked for, the model writes it.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402

_CELLQC = {"status": "ok", "cells_before": 15872, "cells_after": 14553, "genes_after": 33696,
           "thresholds": {"min_counts": 500, "min_features": 300, "max_pct_mt": 10.0},
           "ambient": {"applied": "soupx", "compared": "decontx"},
           "doublets": {"decider": "doubletfinder", "callers": ["doubletfinder", "scdblfinder"]},
           "warnings": ["Sample2_Ddx41: the two doublet callers agree poorly (kappa 0.14), so the "
                        "choice of decider drives which cells are removed."],
           "libraries": [{"sample": "Sample2_Ddx41", "cells_cellranger": 15872,
                          "cells_after_filter": 15482, "cells_after_doublets": 14553,
                          "frac_retained": 0.9169, "doublets_removed": 929, "doublet_kappa": 0.1448,
                          "ambient_soupx_contamination_mean": 0.016, "median_pct_mt_kept": 0.6897,
                          "removed_only_by": {"min_counts": 2, "min_features": 28, "max_pct_mt": 317,
                                              "several_thresholds": 43}}]}
_STEP = "**CellQC quality control** — Run `run_cellqc` on the Sample2_Ddx41 library."
# What the writer returns when a report is asked for: its own layout, the tool's numbers.
_WRITTEN = ("# Quality control of Sample2_Ddx41\n\n"
            "CellQC kept 14,553 of 15,872 cells (91.7%).\n\n"
            "| Stage | Cells |\n|---|---:|\n| Cell Ranger | 15,872 |\n| After filters | 15,482 |\n"
            "| After doublets | 14,553 |\n")


@pytest.fixture(autouse=True)
def _clean_conns():
    yield
    gw_app.CONNECTIONS.clear()


def _conn(tmp_path):
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=asyncio.new_event_loop(), username="tester")
    conn.workspace = tmp_path / "tester"
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _run(tmp_path, monkeypatch, question: str, final_answer: str, extra_steps=()):
    """``_run_lab`` end to end with the research lab replaced by one CellQC step. Returns
    ``(conn, req, payloads, art, rendered)``; ``rendered`` collects what was sent to pandoc."""
    from aiscientist.agents import research_lab as rl
    from aiscientist.agents.research_lab import CriticVerdict, LabResult, LabRound
    from aiscientist.reporting import report as reporting
    from aiscientist.tools import api as tools_api

    conn = _conn(tmp_path)

    class _FakeLab:
        def __init__(self, ctx, config, **_k):
            self.guidance, self.protocols, self.self_sourced = None, [], ""
            self.execution_profile, self.later, self.proposed_steps = {}, [], []
            self.request_breadth = "specific"

        def run(self, question, on_event=None, plan_review=None, **_kw):
            steps = [{"tool": "run_cellqc", "ok": True, "args": {"species": "mouse"}, "result": _CELLQC},
                     *extra_steps, {"tool": "finish", "ok": True, "result": {}}]
            rnd = LabRound(1, 0, _STEP, "Scientist",
                           {"final_answer": "CellQC kept 14,553 of 15,872 barcodes.", "steps": steps},
                           CriticVerdict("accept", 0.8, ""))
            return LabResult(question, [_STEP], [rnd], True, 1, final_answer)

    def _not_this(*_a, **_k):
        raise AssertionError("a QC-only run must not build a manuscript, a technical report or the "
                             "code-built short report")

    def _schematic(agenda, out_dir, **_k):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "workflow.dot").write_text("digraph {}", encoding="utf-8")
        return {"status": "dependency_missing"}

    rendered: list[str] = []

    def _render(md, out_dir, **_k):
        rendered.append(md)
        out_dir.mkdir(parents=True, exist_ok=True)
        for ext in ("md", "pdf", "docx"):
            (out_dir / f"report.{ext}").write_text(md, encoding="utf-8")
        return {"status": "ok", "md_path": str(out_dir / "report.md"),
                "pdf_path": str(out_dir / "report.pdf"), "docx_path": str(out_dir / "report.docx")}

    monkeypatch.setattr(rl, "ResearchLab", _FakeLab)
    monkeypatch.delenv("AISCIENTIST_PLAN_YAML", raising=False)
    for name in ("_build_report", "_build_brief_report", "_review_and_finalize_report",
                 "_build_technical_report"):
        monkeypatch.setattr(gw_app, name, _not_this)
    monkeypatch.setattr(reporting, "build_pdf_report", _render)          # imported inside _run_lab
    monkeypatch.setattr(tools_api, "render_workflow_schematic", _schematic)
    payloads: list[dict] = []
    monkeypatch.setattr(conn, "_publish", lambda payload: payloads.append(payload))

    req = gw_app.LabRequest(connection_id=conn.id, conversation_id="conv-1", question=question)
    conn.begin_run("conv-1")
    asyncio.run(gw_app._run_lab(conn, req))
    art = conn.workspace / conn.last_run_by_conversation["conv-1"] / "artifacts"
    return conn, req, payloads, art, rendered


def _reply(payloads) -> str:
    return "".join(p.get("token", "") for p in payloads if p.get("type") == "chat_token")


def test_no_report_asked_for_the_answer_is_in_the_chat_and_the_folder_is_complete(tmp_path, monkeypatch):
    conn, req, payloads, art, rendered = _run(
        tmp_path, monkeypatch, "Only run CellQC quality control on this folder, no report needed.",
        "CellQC kept 14,553 of 15,872 barcodes.")

    reply = _reply(payloads)
    assert reply.startswith("**Quality control done** — `run_cellqc` on Sample2_Ddx41.")
    assert "- **14,553 of 15,872 cells kept (91.7%)**; 33,696 genes." in reply
    assert "Filters removed 390: 317 only for mitochondrial RNA above 10%" in reply
    assert "Doublets removed: 929 of the 15,482 filtered cells (6.0%), called by DoubletFinder." in reply
    assert len(reply.strip().splitlines()) <= 10
    said = [str(p.get("text") or "") for p in payloads]
    assert any("✅ Done — the summary is below" in s for s in said)
    assert not any("Report ready" in s or "Writing the" in s or "Rendering the report" in s
                   or "literature" in s.lower() for s in said)
    assert rendered == []                                   # nothing went to pandoc

    # The folder is complete: the plan, the summary, the workflow figure, the run record.
    assert sorted(p.name for p in (art / "report").iterdir()) == ["plan.md", "summary.md"]
    assert (art / "report" / "summary.md").read_text(encoding="utf-8") == reply
    assert (art / "figures" / "workflow.dot").exists()
    assert (art / "process" / "run_state.json").exists()
    assert not (art / "process" / "literature_references.json").exists()
    assert "read clean" not in (art / "process" / "capabilities.log").read_text(encoding="utf-8")
    # The next message in this chat still continues from this run ("now cluster these cells").
    state = json.loads((art / "process" / "run_state.json").read_text(encoding="utf-8"))
    assert state["agenda"] == [_STEP]
    target = gw_app._followup_target(conn, req)
    assert target is not None and target[0] == conn.last_run_by_conversation["conv-1"]


def test_a_report_asked_for_is_the_one_the_model_wrote(tmp_path, monkeypatch):
    conn, req, payloads, art, rendered = _run(
        tmp_path, monkeypatch,
        "Do only cellqc quality controll part and generate the report and result to me", _WRITTEN)

    # The document is the writer's report as it wrote it, then the files code knows.
    assert len(rendered) == 1
    doc = rendered[0]
    assert "CellQC kept 14,553 of 15,872 cells (91.7%)." in doc
    assert "| After doublets | 14,553 |" in doc
    assert "## Files" in doc and "- `report/verification.md` — how each number here was checked." in doc
    for word in ("References", "literature", "Discussion"):
        assert word not in doc, word
    # The statement checks the numbers and lists no labels or literature it had nothing to check.
    statement = (art / "report" / "verification.md").read_text(encoding="utf-8")
    assert "Quality-control accounting" in statement
    assert "literature" not in statement and "Cell-type labels" not in statement
    assert not (art / "report" / "summary.md").exists()
    # The chat gets the numbers and where the written report is.
    reply = _reply(payloads)
    assert reply.startswith("**Quality control done** — `run_cellqc` on Sample2_Ddx41.")
    assert reply.rstrip().endswith("The written report is `report/report.pdf` (and "
                                   "`report/report.docx`) in the Downloads panel.")
    said = [str(p.get("text") or "") for p in payloads]
    assert any("Writing the quality-control report" in s for s in said)
    assert any("Report ready" in s for s in said)
    target = gw_app._followup_target(conn, req)
    assert target is not None and target[0] == conn.last_run_by_conversation["conv-1"]


def test_a_read_only_check_after_qc_does_not_turn_the_report_into_a_chat_reply(tmp_path, monkeypatch):
    """Prod run 57c6b0c6ba0c: after CellQC the agent ran a script on HPC3 that only read the checkpoint
    and tables/cellqc_metrics.csv (through AISCIENTIST_ARTIFACTS). It was counted as writing, the run
    took the code-built short report, and the writer's QC report went to the chat instead."""
    check = {"tool": "run_code", "ok": True, "args": {"code": (
        "art=os.environ['AISCIENTIST_ARTIFACTS']\n"
        "df=pd.read_csv(os.path.join(art, 'tables/cellqc_metrics.csv'))\nprint(df.head())\n")},
        "result": {"status": "ok", "execution_mode": "hpc_slurm", "stdout": "..."}}
    conn, req, payloads, art, rendered = _run(
        tmp_path, monkeypatch,
        "Only run CellQC quality control on this Cell Ranger folder and write me a report.", _WRITTEN,
        extra_steps=[check])
    assert len(rendered) == 1 and "CellQC kept 14,553 of 15,872 cells (91.7%)." in rendered[0]
    reply = _reply(payloads)
    assert reply.startswith("**Quality control done**") and len(reply.strip().splitlines()) <= 12
    assert "CellQC kept 14,553 of 15,872 cells (91.7%)." not in reply
    # a check of the tested tool's output is not untested code (issue 13): no notice, no code file
    assert "Untested code" not in reply and "Untested code" not in rendered[0]
    assert not (art / "process" / "untested_code.md").exists()
