"""CellQC over several libraries of two species: the defects the website test of RCHENLAB #147 found
on v0.4.25 (RCHENLAB/AiScientist issue 154, item 1). The test data is two mouse libraries
(Sample1_WT, Sample2_Ddx41) and one human library (Sample3_GSM5676874).

* B2, run 6e5a33230a46 (the parent folder bound): the plan card said species = human, and that one
  value was forced onto every per-library call the model sent as mouse. Every call also named a
  library folder inside the bound folder and was refused with a bare "input path must identify
  exactly one attached dataset or input".
* B2, continued: "Abort the run" on the decision card did not end the run; only Stop did.
* B1, run 1692dc47adc7 (three folders bound): every number was right, and the chat reply was one total
  line, because the three-library result reached the step as a bounded preview without its rows.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("yaml")

from fastapi.testclient import TestClient  # noqa: E402

from aiscientist.agents.mcp_analysis import AnalysisAdapter  # noqa: E402
from aiscientist.agents.mcp_bridge import load_servers  # noqa: E402
from aiscientist.agents.plan_doc import PlanDoc, PlanStep, check_adherence  # noqa: E402
from aiscientist.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from aiscientist.agents.research_lab import ABORT_RUN_OPTION, LabConfig, ResearchLab, per_library_settings  # noqa: E402
from aiscientist.gateway import app as gw_app  # noqa: E402
from aiscientist.gateway.settings import HPCSettings  # noqa: E402
from test_mcp_analysis import analysis_bridge, config, fixture_result  # noqa: E402,F401  (fixture)
from test_research_lab import _dag_always_revise_complete  # noqa: E402

MOUSE = ("Sample1_WT", "Sample2_Ddx41")
HUMAN = ("Sample3_GSM5676874",)
LIBS = MOUSE + HUMAN


@pytest.fixture(autouse=True)
def _clean_conns(monkeypatch):
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)
    yield
    gw_app.CONNECTIONS.clear()


# --- B2: one plan value is not forced onto every library -------------------------------------------

QC_STEP = ("**CellQC quality control** — Run `mcp_run_cellqc` with species human over the bound "
           "libraries, removing cells whose reads are more than 10% mitochondrial.")
# A plan that chooses which libraries to QC: a choice for the whole call, which the plan keeps.
TWO_STEP = ("**CellQC on the mouse libraries** — Run `mcp_run_cellqc` with species mouse on "
            "Sample1_WT and Sample2_Ddx41, removing cells whose reads are more than 10% mitochondrial.")


def _cellqc_catalog(calls: list) -> "list[HarnessTool]":
    def run(args, _ctx):
        calls.append(dict(args))
        return {"status": "ok", "cells_before": 48112, "cells_after": 41526}
    return [HarnessTool("mcp_run_cellqc", "CellQC test double", {"type": "object", "properties": {
        "species": {"type": "string", "default": "auto"}, "max_pct_mt": {"type": "number", "default": 10},
        "samples": {"type": "array", "items": {"type": "string"}}, "dataset": {"type": "string"}}}, run)]


def _complete(step: str, settings: dict):
    def complete(messages):
        system = messages[0]["content"]
        if "Principal Investigator of a bioinformatics lab" in system:
            return json.dumps({"agenda": [step]})
        if "filling in a form" in system:
            return json.dumps([{"step": 1, "settings": settings}])
        if "rigorous scientific Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "FINAL REPORT"
    return complete


def _qc_lab(calls: list, briefs: list, model_args: dict, libraries, *, step: str = QC_STEP,
            settings: "dict | None" = None, review: bool = False) -> ResearchLab:
    def chat(messages, _tools):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"answer": "done"})}}]}
        briefs.append(next(m["content"] for m in messages if m.get("role") == "user"))
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "mcp_run_cellqc", "arguments": json.dumps(model_args)}}]}

    decisions = {"cellranger_layout": {"n_libraries": len(libraries),
                                       "libraries": [{"sample": s} for s in libraries]}}
    return ResearchLab(HarnessContext(decisions=decisions, tunnel_port=1, model="m"),
                       LabConfig(plan_yaml=True, plan_review=review, auto_select_skill=False,
                                 mode="single", planner="linear"),
                       complete_fn=_complete(step, settings if settings is not None
                                             else {"species": "human", "max_pct_mt": 10}),
                       scientist=ResearchHarness(catalog=_cellqc_catalog(calls), chat_fn=chat))


def _of(events, kind):
    return [e for e in events if e["type"] == kind]


def test_cellqc_names_its_per_library_settings():
    assert per_library_settings("mcp_run_cellqc") == ("species", "chemistry")
    assert per_library_settings("run_cellqc") == per_library_settings("mcp_run_cellqc")
    assert per_library_settings("mcp_run_clustering") == ()


@pytest.mark.parametrize("model_args, ran", [
    # One call over every library, species read from each: CellQC QCs each species on its own.
    ({}, {}),
    ({"species": "auto"}, {"species": "auto"}),
    # The mouse libraries, as the model sent them in run 6e5a33230a46.
    ({"species": "mouse", "samples": list(MOUSE)}, {"species": "mouse", "samples": list(MOUSE)}),
])
def test_over_mixed_species_libraries_the_planners_species_is_not_on_the_card_or_the_call(model_args, ran):
    calls: list = []
    events: list = []
    _qc_lab(calls, [], model_args, LIBS).run("Do cellqc quality control", events.append)
    # The card shows what runs: no single species for three libraries of two species.
    steps = _of(events, "plan_doc")[0]["steps"]
    assert steps[0].get("settings") == {"max_pct_mt": 10.0}
    assert {"param": "species", "value": "human", "why": "per_library"}.items() <= \
        _of(events, "plan_bind_dropped")[0]["dropped"][0].items()
    # The plan's species = human ran on none of them; the cohort-wide threshold still did.
    assert calls == [{**ran, "max_pct_mt": 10.0}]
    assert not _of(events, "plan_setting_enforced")
    check = _of(events, "plan_adherence")[0]
    assert check["ok"] is True and "per_call" not in check["steps"][0]


def test_a_species_put_on_the_card_by_hand_is_not_forced_onto_every_library():
    """A hand edit can still set one species over several libraries: it is said, not forced."""
    calls: list = []
    briefs: list = []
    events: list = []
    seen: list = []

    def review(_kind, _payload):
        seen.append(1)
        if len(seen) == 1:
            doc = _of(events, "plan_doc")[-1]["yaml"]
            assert "      max_pct_mt: 10.0\n" in doc
            return {"action": "edit", "plan_yaml": doc.replace("      max_pct_mt: 10.0\n",
                                                               "      max_pct_mt: 10.0\n      species: human\n")}
        return {"action": "approve"}

    lab = _qc_lab(calls, briefs, {"species": "mouse", "samples": list(MOUSE)}, LIBS, review=True)
    lab.run("Do cellqc quality control", events.append, review)
    assert calls == [{"species": "mouse", "samples": list(MOUSE), "max_pct_mt": 10.0}]
    assert not _of(events, "plan_setting_enforced")
    said = _of(events, "plan_setting_per_library")
    assert len(said) == 1 and said[0]["settings"] == {"species": "human"}
    assert said[0]["libraries"] == list(LIBS)
    assert "NOT applied to every call" in briefs[0] and "Sample3_GSM5676874" in briefs[0]
    # The read-back does not call a per-library value a different setting than approved.
    check = _of(events, "plan_adherence")[0]
    assert check["ok"] is True and check["steps"][0]["per_call"] == {"species": "human"}


def test_the_libraries_a_plan_chooses_stay_pinned_over_several_libraries():
    """`samples` chooses which libraries the call QCs: three bound, the plan QCs two, and a call the
    model sends over all three runs over the two (and the read-back compares it)."""
    calls: list = []
    events: list = []
    lab = _qc_lab(calls, [], {"species": "mouse", "samples": list(LIBS)}, LIBS, step=TWO_STEP,
                  settings={"species": "mouse", "samples": list(MOUSE), "max_pct_mt": 10})
    lab.run("Do cellqc quality control on the mouse libraries", events.append)
    assert _of(events, "plan_doc")[0]["steps"][0]["settings"] == {"samples": list(MOUSE), "max_pct_mt": 10.0}
    assert calls == [{"species": "mouse", "samples": list(MOUSE), "max_pct_mt": 10.0}]
    enforced = _of(events, "plan_setting_enforced")
    assert enforced and enforced[0]["overrode"] == [{"param": "samples", "approved": list(MOUSE),
                                                     "model": list(LIBS)}]
    check = _of(events, "plan_adherence")[0]
    assert check["ok"] is True and check["steps"][0]["status"] == "as_planned"


def test_one_library_keeps_the_approved_species():
    calls: list = []
    events: list = []
    _qc_lab(calls, [], {"species": "mouse"}, HUMAN).run("Do cellqc quality control", events.append)
    assert _of(events, "plan_doc")[0]["steps"][0]["settings"] == {"species": "human", "max_pct_mt": 10.0}
    assert calls == [{"species": "human", "max_pct_mt": 10.0}]
    enforced = _of(events, "plan_setting_enforced")
    assert enforced and enforced[0]["overrode"] == [{"param": "species", "approved": "human", "model": "mouse"}]
    assert not _of(events, "plan_setting_per_library")


def test_the_read_back_holds_back_only_the_step_it_is_told_about():
    one = PlanStep(id="s1", text=QC_STEP, tool="mcp_run_cellqc", settings={"species": "human"})
    two = PlanStep(id="s2", text=TWO_STEP, tool="mcp_run_cellqc", settings={"species": "human"})
    doc = PlanDoc(request="qc", steps=(one, two))
    executed = [{"step": s.text, "accepted": True,
                 "calls": [{"tool": "mcp_run_cellqc", "args": {"species": "mouse"}, "ok": True}]}
                for s in (one, two)]
    plain = check_adherence(doc, executed)
    assert [r["status"] for r in plain["steps"]] == ["settings_differ", "settings_differ"]
    held = check_adherence(doc, executed, per_call={"s1": ("species",)})
    assert [r["status"] for r in held["steps"]] == ["as_planned", "settings_differ"]
    assert held["steps"][0]["per_call"] == {"species": "human"} and "per_call" not in held["steps"][1]


def test_a_failed_call_names_the_approved_value_that_ran():
    fed: list = []

    def broken(_args, _ctx):
        raise ValueError("input path must identify exactly one attached dataset or input")

    def chat(messages, _tools):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs:
            fed.append(tool_msgs[-1]["content"])
            return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"answer": "failed"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "mcp_run_cellqc", "arguments": json.dumps({"species": "mouse"})}}]}

    tool = HarnessTool("mcp_run_cellqc", "CellQC", {"type": "object", "properties": {
        "species": {"type": "string"}}}, broken)
    ResearchHarness(catalog=[tool], chat_fn=chat).run(
        "QC.", HarnessContext(decisions={}, tunnel_port=1, model="m"),
        pinned_args={"mcp_run_cellqc": {"species": "human"}})
    assert '"approved_settings_applied": {"species": "human"}' in fed[0]
    assert "exactly one attached dataset" in fed[0]


def test_a_library_inside_the_bound_folder_is_refused_with_what_to_send_instead(tmp_path):
    server, = load_servers(config(tmp_path, "cellqc"))
    adapter = AnalysisAdapter(server)
    root = "/hpc/sessions/s1/inputs/CellQC_testdata"
    executor = SimpleNamespace(dataset_path=f"{root}/Sample1_WT/outs/filtered_feature_bc_matrix.h5",
                               dataset_root=root, inputs=[])
    for args in ({"dataset": f"{root}/Sample3_GSM5676874"},
                 {"dataset_paths": [f"{root}/Sample1_WT", f"{root}/Sample2_Ddx41/"]},
                 {"dataset": "$AISCIENTIST_DATASET_ROOT/Sample3_GSM5676874"}):
        with pytest.raises(ValueError) as err:
            adapter.prepare(args, executor, tool="run_cellqc")
        text = str(err.value)
        assert text.startswith("input path must identify exactly one attached dataset or input")
        assert "inside the bound folder CellQC_testdata" in text
        assert "leave `dataset` and `dataset_paths` out" in text and "`samples` chooses libraries" in text
        assert "/hpc/" not in text, "the refusal names folders, never a server path"
    # A path outside the bound data gets the plain refusal; the bound folder itself still binds.
    with pytest.raises(ValueError) as err:
        adapter.prepare({"dataset": "/elsewhere/Sample1_WT"}, executor, tool="run_cellqc")
    assert str(err.value) == "input path must identify exactly one attached dataset or input"
    assert "dataset" not in adapter.prepare({"dataset": root}, executor, tool="run_cellqc").arguments


# --- B2, continued: "Abort the run" ends the run as Stop does ---------------------------------------

def _conn():
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=__import__("asyncio").new_event_loop(),
                             username="tester")
    conn.status = "ready"
    conn.executor = object()
    conn.alloc = object()
    conn.push = lambda *_a, **_k: None
    gw_app.CONNECTIONS[conn.id] = conn
    return conn


def _start(conn, conversation_id, run_id):
    ctx = contextvars.copy_context()

    def begin():
        run = conn.begin_run(conversation_id)
        conn.bind_run_id(run, run_id)
        run.running = True
        return run
    return ctx.run(begin), ctx


def _abort(conn, conversation_id):
    return TestClient(gw_app.app).post("/api/lab/plan", json={
        "connection_id": conn.id, "conversation_id": conversation_id, "action": "revise",
        "feedback": ABORT_RUN_OPTION}).json()


def test_abort_at_a_waiting_failure_fork_ends_the_run():
    """The decision card of a failed step is up and the run waits for it: Abort answers it, and the
    run stops as Stop would stop it, with what it finished."""
    conn = _conn()
    run, ctx = _start(conn, "conv-B2", "6e5a33230a46")
    other, _ = _start(conn, "conv-other", "other-run")
    refused = "input path must identify exactly one attached dataset or input"

    def chat(messages, _tools):
        # B2's step: each call is refused, then the step gives up and the Critic sends it back.
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
                "name": "finish", "arguments": json.dumps({"answer": "every call was refused"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function", "function": {
            "name": "mcp_run_cellqc", "arguments": json.dumps({"dataset": "Sample1_WT"})}}]}

    cellqc = HarnessTool("mcp_run_cellqc", "CellQC", {"type": "object", "properties": {
        "dataset": {"type": "string"}}}, lambda a, c: {"status": "error", "error": refused})
    lab = ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model="m"),
                      LabConfig(planner="dag", auto_select_skill=False, max_revisions=0),
                      complete_fn=_dag_always_revise_complete(
                          ["**CellQC quality control** — Run `mcp_run_cellqc` on the bound libraries."]),
                      scientist=ResearchHarness(catalog=[cellqc], chat_fn=chat))
    events: list = []
    out: dict = {}

    def go():
        out["result"] = lab.run("Do cellqc quality control", on_event=events.append,
                                should_cancel=run.chat_stop.is_set,
                                decision_review=lambda fork: gw_app._decision_review(
                                    conn, lambda *_a, **_k: None, fork, timeout=30))

    worker = threading.Thread(target=ctx.run, args=(go,))
    worker.start()
    for _ in range(2000):
        if run.pending_plan is not None:
            break
        time.sleep(0.005)
    assert run.pending_plan["kind"] == "decision"
    assert ABORT_RUN_OPTION in run.pending_plan["payload"]["options"]
    assert _abort(conn, "conv-B2") == {"status": "stopping", "action": "abort"}
    worker.join(timeout=30)
    assert not worker.is_alive(), "the run ended instead of waiting out the decision timeout"
    assert run.chat_stop.is_set() and not run.hard_stop.is_set()
    assert any(e["type"] == "run_cancelled" for e in events)
    assert out["result"].converged is False
    assert not other.chat_stop.is_set(), "another chat's run is not touched"


def test_abort_from_a_card_the_run_no_longer_waits_for_still_ends_the_run():
    """Run 6e5a33230a46: the first decision had timed out and the run was running tool calls when
    "Abort the run" was clicked. The answer used to be nobody's; now it stops the run."""
    conn = _conn()
    run, _ctx = _start(conn, "conv-B2", "6e5a33230a46")
    assert run.pending_plan is None
    assert _abort(conn, "conv-B2") == {"status": "stopping", "action": "abort"}
    assert run.chat_stop.is_set() and not run.hard_stop.is_set()
    # A second Abort on a run already stopping is not a second Stop: the write-up still runs.
    assert _abort(conn, "conv-B2")["status"] == "stopping"
    assert not run.hard_stop.is_set()


def test_a_timed_out_decision_closes_only_its_own_chats_card():
    """Two chats on one connection: B's plan card waits while A's decision times out. The close
    goes to A's chat, and B's plan is still waiting."""
    conn = _conn()
    pushed: list = []
    conn.push = lambda payload, **_k: pushed.append(payload)
    a, ctx_a = _start(conn, "conv-A", "runA")
    b, _ = _start(conn, "conv-B", "runB")
    b.pending_plan = {"kind": "agenda", "payload": ["CellQC quality control"]}
    node = SimpleNamespace(id="s1-failfork", goal="Step failed after 1 attempts",
                           options=("Skip this step", ABORT_RUN_OPTION))
    out = ctx_a.run(gw_app._decision_review, conn, lambda *_a, **_k: None, node, timeout=0.05)
    assert out == {"action": "proceed", "timed_out": True}
    assert [(p["type"], p.get("conversation_id")) for p in pushed] == [
        ("decision_prompt", "conv-A"), ("plan_done", "conv-A")]
    assert a.pending_plan is None and b.pending_plan == {"kind": "agenda", "payload": ["CellQC quality control"]}


def test_abort_is_not_read_into_a_plan_card():
    conn = _conn()
    run, _ctx = _start(conn, "conv-B2", "6e5a33230a46")
    run.pending_plan = {"kind": "agenda", "payload": ["CellQC quality control"]}
    reply = _abort(conn, "conv-B2")
    assert reply == {"status": "ok", "action": "revise"}
    assert not run.chat_stop.is_set()
    assert run.plan_value["feedback"] == ABORT_RUN_OPTION


# --- B1: the reply to a multi-sample run has a row and the warnings of each sample -----------------

def _library(sample, species, cr, filt, kept, *, mito, ambient, kappa, removed):
    """One library of CellQC's result, with the many metrics the real one carries."""
    row = {"sample": sample, "species": species, "cells_cellranger": cr, "cells_after_filter": filt,
           "cells_after_doublets": kept, "frac_retained": round(kept / cr, 4),
           "doublets_removed": filt - kept, "doublet_kappa": kappa, "median_pct_mt": mito,
           "median_pct_mt_kept": round(mito * 0.9, 4), "ambient_soupx_contamination_mean": ambient,
           "ambient_decontx_contamination_mean": ambient * 2, "removed_only_by": removed,
           "removed_by_mito_total": removed["max_pct_mt"] + removed["several_thresholds"],
           "n_mt_genes": 13, "mt_matched_by": "prefix"}
    row.update({f"metric_{i}": i * 1.5 for i in range(40)})
    return row


_WARNINGS = [
    "Sample1_WT: the two doublet callers agree poorly (kappa 0.07), so the choice of decider drives "
    "which cells are removed.",
    "Sample2_Ddx41: the two doublet callers agree poorly (kappa 0.14), so the choice of decider drives "
    "which cells are removed.",
    "Sample2_Ddx41: barcode-rank knee (117 UMIs) is below the inflection (683 UMIs); the knee diagnostic "
    "is inconsistent. Cell Ranger's called cells were used.",
    "Sample3_GSM5676874: max_pct_mt=10.0% alone removed 2,277 of 13,559 cells (16.8%); median before "
    "filtering: 6.09%. Check that threshold against this tissue before using the result.",
]


def _b1_result():
    libs = [
        _library("Sample1_WT", "mouse", 18681, 18011, 16750, mito=0.75, ambient=0.025, kappa=0.07,
                 removed={"min_counts": 4, "min_features": 27, "max_pct_mt": 541, "several_thresholds": 98}),
        _library("Sample2_Ddx41", "mouse", 15872, 15482, 14553, mito=0.77, ambient=0.016, kappa=0.14,
                 removed={"min_counts": 2, "min_features": 28, "max_pct_mt": 317, "several_thresholds": 43}),
        _library("Sample3_GSM5676874", "human", 13559, 11234, 10223, mito=6.09, ambient=0.010, kappa=0.71,
                 removed={"min_counts": 0, "min_features": 16, "max_pct_mt": 2277, "several_thresholds": 32}),
    ]
    return {"step": "qc", "method": "CellQC 0.3.6", "libraries": libs, "n_libraries": 3,
            "excluded_libraries": [], "cells_before": 48112, "cells_after": 41526, "genes_after": 33696,
            "pct_cells_removed": 13.69, "thresholds": {"min_counts": 500, "min_features": 300, "max_pct_mt": 10.0},
            "threshold_defaults": {"min_counts": 500, "min_features": 300, "max_pct_mt": 10.0},
            "ambient": {"applied": "soupx", "compared": "decontx"},
            "doublets": {"decider": "doubletfinder", "callers": ["doubletfinder", "scdblfinder"]},
            "warnings": list(_WARNINGS),
            "notes": ["The libraries are from different species (mouse: Sample1_WT, Sample2_Ddx41; human: "
                      "Sample3_GSM5676874). Each species was QC'd separately with its own gene sets and "
                      "merged into its own result dataset."],
            "checkpoints": {"mouse": {"checkpoint": "adata_qc.h5ad", "cells": 31303},
                            "human": {"checkpoint": "adata_qc.human.h5ad", "cells": 10223}},
            "assumptions": {"species": ["mouse+human", "mouse for Sample1_WT, Sample2_Ddx41 (read from "
                                        "Cell Ranger output); human for Sample3_GSM5676874"],
                            "chemistry": ["next_gem+next_gem", "next_gem for both species' libraries"]}}


def _qc_run(response):
    return SimpleNamespace(rounds=[{"step": "**CellQC on all three libraries** — `mcp_run_cellqc`.",
                                    "specialist": "QC", "verdict": {"verdict": "accept"},
                                    "scientist_result": {"steps": [{"tool": "mcp_run_cellqc", "ok": True,
                                                                    "result": response}]}}],
                           skipped_steps=[])


def test_a_three_library_reply_has_a_row_and_the_warnings_of_each_sample(analysis_bridge):  # noqa: F811
    runner, ctx, make = analysis_bridge
    value, output = fixture_result(runner)
    (output / "work" / "adata_qc.human.h5ad").write_bytes(b"human checkpoint bytes")
    value.update(_b1_result())
    runner.fixture.write_text(json.dumps(value))
    response = make().executor({}, ctx)
    # What the step holds is the bounded preview: totals, and not every library's row (run 1692dc47adc7).
    assert response["status"] == "ok", {k: response.get(k) for k in ("error", "stderr", "returncode")}
    assert response["cells_before"] == 48112
    assert len(response.get("libraries") or []) < 3, "the precondition of the defect"
    art = runner.artifacts

    reply = gw_app._qc_chat_reply(art, _qc_run(response))
    assert "`run_cellqc` on Sample1_WT, Sample2_Ddx41, Sample3_GSM5676874." in reply
    assert "**41,526 of 48,112 cells kept (86.3%)**" in reply
    assert "| Library | Species | Cell Ranger cells |" in reply and "| ambient RNA |" in reply
    assert "| Sample1_WT | mouse | 18,681 | 18,011 | 16,750 | 1,261 | 89.7% |" in reply
    assert "| Sample2_Ddx41 | mouse | 15,872 | 15,482 | 14,553 | 929 | 91.7% |" in reply
    assert "| Sample3_GSM5676874 | human | 13,559 | 11,234 | 10,223 | 1,011 | 75.4% |" in reply
    assert "| 2.5% |" in reply and "| 1.0% |" in reply
    assert ("- Sample3_GSM5676874 — filters removed 2,325: 2,277 only for mitochondrial RNA above 10%, "
            "16 only for fewer than 300 genes, 32 for more than one.") in reply
    # Each sample's warnings, in the order of the table.
    for needle in ("Sample1_WT: Doublet filtering needs review", "Sample2_Ddx41: barcode-rank knee (117 UMIs)",
                   "Sample3_GSM5676874: max_pct_mt=10.0% alone removed 2,277 of 13,559 cells (16.8%)"):
        assert needle in reply
    assert reply.index("Sample1_WT: Doublet") < reply.index("Sample2_Ddx41: barcode") < reply.index(
        "Sample3_GSM5676874: max_pct_mt")
    assert "Each species was QC'd separately" in reply

    shown = gw_app._default_qc_display(reply, art, _qc_run(response))
    for needle in ("| Sample3_GSM5676874 | human | 13,559 |", "Sample3_GSM5676874: max_pct_mt=10.0% alone removed",
                   "Sample2_Ddx41: barcode-rank knee"):
        assert needle in shown


def test_a_full_result_of_another_call_is_not_read(tmp_path):
    full = _b1_result()
    rel = "mcp/cellqc/default/tasks/task-one/run_cellqc.json"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(json.dumps({**full, "cells_before": 99999}))
    preview = {"status": "ok", "cells_before": 48112, "cells_after": 41526, "n_libraries": 3,
               "full_result": rel, "response_compacted": True}
    assert gw_app._qc_complete_result(tmp_path, preview) is preview
    (tmp_path / rel).write_text(json.dumps(full))
    assert len(gw_app._qc_complete_result(tmp_path, preview)["libraries"]) == 3
    # A path out of the run's folder is never read.
    assert gw_app._qc_complete_result(tmp_path, {**preview, "full_result": "mcp/../../x.json"})["full_result"]
    assert "libraries" not in gw_app._qc_complete_result(tmp_path, {**preview, "full_result": "mcp/../../x.json"})


def test_the_chat_says_the_plans_species_was_not_applied_to_every_library():
    said = gw_app._lab_event_to_chat({"type": "plan_setting_per_library", "tool": "mcp_run_cellqc",
                                      "settings": {"species": "human"}, "libraries": list(LIBS)})
    text = " ".join(str(m) for m in said)
    assert "species = 'human' from the plan is one value for 3 libraries" in text
    assert "not applied to every call" in text
