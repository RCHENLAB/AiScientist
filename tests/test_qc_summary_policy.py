"""The model-written final result has the same concise QC policy as the chat summary."""

from copy import deepcopy

import pytest

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import CriticVerdict, LabConfig, LabRound, ResearchLab
from aiscientist.tools.run_cellqc.tool import make_tool


@pytest.mark.parametrize("qc_only", [True, False])
def test_synthesis_writer_receives_default_qc_policy_without_losing_audit_data(monkeypatch, qc_only):
    monkeypatch.setenv("AISCIENTIST_CLAIM_AUDIT", "0")
    cellqc = {
        "status": "ok", "cells_before": 1000, "cells_after": 855,
        "libraries": [{"sample": "S1", "cells_after_filter": 900, "cells_after_doublets": 855,
                       "doublets_removed": 45, "doublet_kappa": 0.1}],
        "doublets": {"decider": "doubletfinder", "callers": ["doubletfinder", "scdblfinder"]},
        "warnings": ["S1: the two doublet callers agree poorly (kappa 0.10), so the choice of "
                     "decider drives which cells are removed.", "S1: no mitochondrial gene matched."],
    }
    original = deepcopy(cellqc)
    rounds = [LabRound(1, 1, "CellQC", "Scientist", {
        "final_answer": "45 doublets removed; the callers agree poorly (kappa 0.10).",
        "steps": [{"tool": "run_cellqc", "ok": True, "result": cellqc}],
    }, CriticVerdict("accept", 1.0, ""))]
    if not qc_only:
        rounds.append(LabRound(1, 2, "Clustering", "Scientist", {
            "final_answer": "Found three clusters.",
            "steps": [{"tool": "run_clustering", "ok": True,
                       "result": {"status": "ok", "n_clusters": 3}}],
        }, CriticVerdict("accept", 1.0, "")))
    calls = []

    def complete(messages, **_kwargs):
        calls.append(messages)
        return "# QC result\n\n855 cells kept; 45 doublets removed."

    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="offline-test"),
                      LabConfig(), complete_fn=complete)
    question = "Write a report on this QC run." if qc_only else "Analyze this dataset and write a report."
    lab._synthesize(question, rounds, lambda _event: None)
    writer = next(messages for messages in calls
                  if "Principal Investigator writing the final research report" in messages[0]["content"])
    system, user = writer[0]["content"], writer[1]["content"]
    assert "Default QC presentation:" in system
    assert "doublets removed" in system
    for detail in ("per-cell doublet scores", "caller comparisons", "agreement", "kappa", "disagreement counts"):
        assert detail in system
    assert "only when the user explicitly asks" in system
    assert "Do not hide failed QC steps" in system
    assert "available" in system and "technical details" in system
    assert ("This run did quality control only" in user) == qc_only
    # This is a presentation rule. The writer and audit record retain the actual evidence.
    assert '"doublets_removed": 45' in user and '"doublet_kappa": 0.1' in user
    assert "no mitochondrial gene matched" in user
    assert cellqc == original


def test_cellqc_catalog_description_leads_with_qc_outcome_and_detail_location():
    description = make_tool().description
    assert "doublets removed" in description
    assert "technical details" in description
    assert "download" in description
    assert "second opinion" not in description
    assert "DoubletFinder" not in description and "scDblFinder" not in description


@pytest.mark.parametrize("args", [{}, {"doublet_decider": "scdblfinder"}])
def test_cellqc_record_section_describes_removal_without_caller_comparison(args):
    from types import SimpleNamespace

    pytest.importorskip("aiscientist.gateway.app")
    from aiscientist.gateway.app import _pipeline_section

    result = SimpleNamespace(rounds=[{"scientist_result": {"steps": [
        {"tool": "run_cellqc", "ok": True, "args": args, "result": {"status": "ok"}},
    ]}}])
    section = _pipeline_section(result)
    assert "removed doublets" in section
    assert "DoubletFinder" not in section and "scDblFinder" not in section
    assert "second opinion" not in section and "kappa" not in section


def test_manuscript_previews_hide_doublet_details_but_technical_report_keeps_them(tmp_path):
    from types import SimpleNamespace
    from aiscientist.gateway.app import _build_report, _build_technical_report, _review_report
    from aiscientist.reporting.qc_summary import QC_SUMMARY_POLICY

    tables = tmp_path / "tables"
    tables.mkdir()
    metrics = tables / "cellqc_metrics.csv"
    metrics.write_text("sampleid,doublet_doubletfinder_ndoublet,doublet_scdblfinder_ndoublet,"
                       "concordance_doubletfinder_vs_scdblfinder_kappa,doublet_ncell_after\n"
                       "S1,45,65,0.1,855\n")
    cells = tables / "cells.csv"
    cells.write_text("barcode,doubletfinder_pANN,scdblfinder_score,scdblfinder_class,ISG_score\n"
                     "cell1,0.21,0.42,Singlet,1.5\n")
    (tables / "annotation.csv").write_text("label,kappa,score\nRod,0.8,3.2\n")
    original = {p: p.read_bytes() for p in tables.iterdir()}
    result = SimpleNamespace(rounds=[], agenda=[], final_answer="855 cells kept; 45 doublets removed.")
    seen = []

    def writer(messages, **_kwargs):
        seen.append(messages)
        assert QC_SUMMARY_POLICY in messages[0]["content"]
        prompt = messages[1]["content"]
        for detail in ("doubletfinder", "scdblfinder", "concordance_doublet"):
            assert detail not in prompt
        assert "doublet_ncell_after" in prompt and "855" in prompt
        assert "ISG_score" in prompt and "kappa" in prompt  # unrelated scientific metrics stay
        return "# Results\n\n855 cells kept; 45 doublets removed.\n\nISG score: 1.5."

    report = _build_report(result.final_answer, tmp_path, writer, "Analyze these cells", result)
    assert "tables/cellqc_metrics.csv" in report and "tables/cells.csv" in report

    def reviewer(messages, **_kwargs):
        seen.append(messages)
        assert QC_SUMMARY_POLICY in messages[0]["content"]
        return report

    assert _review_report(report, tmp_path, reviewer, "Analyze these cells") == report.strip()

    technical_calls = []

    def technical_writer(messages, **_kwargs):
        technical_calls.append(messages)
        return "# Technical report\n\nDetailed QC diagnostics."

    technical_report = _build_technical_report(result, tmp_path, technical_writer, "Analyze these cells")
    assert "ISG score: 1.5." in report  # the writer assertions must not trigger fallback
    assert len(seen) == 2
    assert len(technical_calls) == 1
    assert "Detailed QC diagnostics." in technical_report
    assert QC_SUMMARY_POLICY not in technical_calls[0][0]["content"]
    # Preview headers are bounded to 40 characters; the comparison column remains present.
    assert "concordance_doubletfinder_vs_" in technical_calls[0][1]["content"]
    assert "scdblfinder_score" in technical_calls[0][1]["content"]
    assert all(p.read_bytes() == content for p, content in original.items())


@pytest.mark.parametrize("failure", ["empty", "exception"])
def test_report_fallback_keeps_concise_synthesis_and_detail_downloads(tmp_path, failure):
    from aiscientist.gateway.app import _build_report, _review_report

    (tmp_path / "tables").mkdir()
    (tmp_path / "tables/cellqc_metrics.csv").write_text("doublet_kappa\n0.1\n")

    def unavailable(messages, **_kwargs):
        assert "Default QC presentation:" in messages[0]["content"]
        if failure == "exception":
            raise RuntimeError("offline writer unavailable")
        return ""

    synthesis = "855 cells kept; 45 doublets removed. Doublet filtering needs review."
    fallback = _build_report(synthesis, tmp_path, unavailable, "Analyze these cells")
    reviewed = _review_report(fallback, tmp_path, unavailable, "Analyze these cells")
    assert synthesis in reviewed
    assert "tables/cellqc_metrics.csv" in reviewed
    assert "kappa" not in reviewed


@pytest.mark.parametrize("planner", ["linear", "dag", "campaign"])
def test_stopped_run_summarizes_qc_without_replaying_caller_comparisons(monkeypatch, planner):
    from aiscientist.agents.dag import LabPlan, TaskNode
    from aiscientist.agents.research_harness import HarnessResult
    from aiscientist.agents.research_lab import DEFAULT_SPECIALISTS

    diagnostic = "S1: the two doublet callers agree poorly (kappa 0.10)."
    cellqc = {"status": "ok", "cells_before": 1000, "cells_after": 855,
              "libraries": [{"sample": "S1", "cells_after_filter": 900,
                             "doublets_removed": 45, "doublet_kappa": 0.1}],
              "warnings": [diagnostic, "S1: step ambient failed — unavailable input."]}
    raw_answer = "DoubletFinder removed 45; scDblFinder scored 65; kappa 0.10."
    other_answer = "ISG score: 1.5; annotation agreement kappa 0.8."
    qc_result = HarnessResult("ok", "finished", raw_answer, [
        {"tool": "run_cellqc", "ok": True, "result": cellqc}], [])
    other_result = HarnessResult("ok", "finished", other_answer, [
        {"tool": "run_clustering", "ok": True, "result": {"status": "ok", "n_clusters": 3}}], [])
    original = deepcopy(qc_result.to_dict())
    completed = []
    model_calls = []

    def no_model(messages, **_kwargs):
        model_calls.append(messages)
        raise AssertionError("Stopping must not call the model")

    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="offline-test"),
                      LabConfig(planner="dag" if planner == "dag" else "linear",
                                context_management=False, step_meetings=False,
                                hypothesis_driven=False, max_cycles=2), complete_fn=no_model)

    def scientist(*_args, **_kwargs):
        answer = [qc_result, other_result][len(completed)]
        completed.append(answer)
        return answer

    monkeypatch.setattr(lab, "_scientist", scientist)
    monkeypatch.setattr(lab, "_critic", lambda *_args: CriticVerdict("accept", 1.0, ""))
    agenda = ["Run CellQC", "Cluster cells", "Identify markers"]
    events = []
    kwargs = {"exec_roster": DEFAULT_SPECIALISTS, "exec_multi": False}
    def stop():
        return len(completed) == 2

    if planner == "linear":
        result = lab._run_loop("Analyze cells", agenda, events.append, stop, None, **kwargs)
    elif planner == "campaign":
        def execute(cycle_agenda, **cycle_kwargs):
            return lab._run_loop("Analyze cells", cycle_agenda, events.append, stop, None,
                                 **kwargs, **cycle_kwargs)

        result = lab._run_campaign("Analyze cells", agenda, execute, events.append, stop)
    else:
        plan = LabPlan(tuple(TaskNode(id=f"s{i}", goal=goal,
                                      depends_on=(f"s{i-1}",) if i > 1 else ())
                             for i, goal in enumerate(agenda, 1)))
        result = lab._run_dag("Analyze cells", plan, events.append, stop, None, **kwargs)
    assert not result.converged and result.accepted_steps == 2
    assert "stopped by the user" in result.final_answer
    assert "855 of 1,000 cells kept" in result.final_answer
    assert "Doublets removed: 45" in result.final_answer
    assert "Doublet filtering needs review" in result.final_answer
    assert "step ambient failed" in result.final_answer
    assert other_answer in result.final_answer
    for detail in ("DoubletFinder", "scDblFinder", "kappa 0.10", "callers agree"):
        assert detail not in result.final_answer
    assert result.rounds[0].scientist_result == original
    assert qc_result.to_dict() == original
    assert model_calls == []
