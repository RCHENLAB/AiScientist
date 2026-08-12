"""Offline smoke test for the lab → centre-panel streaming translation.

Covers ``_lab_event_to_chat``: the pure function that turns a lab ``on_event`` dict
into chat-stream WebSocket payloads so a run shows live progress in the centre bubble
(Claude-style) instead of sitting at "…" until the report lands. No cluster, no SSH,
no network — just the event→payload mapping.

Two channels are asserted:
- verbose turns (tool_start / tool_result / tool_error / critic) → ``chat_thinking``
  tokens (the collapsible activity log);
- key milestones (plan, step start, acceptance, done) → ``lab_progress`` lines (the
  always-visible key-progress feed).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway.app import _lab_event_to_chat  # noqa: E402


def _kinds(payloads):
    return [p["type"] for p in payloads]


def test_agenda_becomes_key_progress_with_substeps():
    out = _lab_event_to_chat({"type": "pi_agenda", "agenda": ["QC", "Cluster", "DE"]})
    assert all(p["type"] == "lab_progress" for p in out)
    # header line + one line per step
    assert out[0]["text"].startswith("📋 Plan ready — 3 steps")
    assert [p["text"] for p in out[1:]] == ["  1. QC", "  2. Cluster", "  3. DE"]


def test_singular_step_count():
    out = _lab_event_to_chat({"type": "pi_agenda", "agenda": ["only one"]})
    assert out[0]["text"].startswith("📋 Plan ready — 1 step")
    assert not out[0]["text"].startswith("📋 Plan ready — 1 steps")


def test_dag_plan_event_surfaces_task_count():
    out = _lab_event_to_chat({"type": "lab_plan_dag", "nodes": [
        {"id": "s1", "goal": "QC"},
        {"id": "s2", "goal": "Cluster", "depends_on": ["s1"]},
        {"id": "s3", "goal": "Lit", "depends_on": ["s1"]},
    ]})
    assert len(out) == 1 and out[0]["type"] == "lab_progress"
    assert "3 task" in out[0]["text"] and "2 with prerequisites" in out[0]["text"]


def test_coordinator_pick_surfaces_choice():
    out = _lab_event_to_chat({"type": "coordinator_pick", "next": "s3", "ready": ["s2", "s3"]})
    types = [p["type"] for p in out]
    assert "chat_thinking" in types and "lab_progress" in types   # activity log + a feed line
    assert any("s3" in p.get("token", "") for p in out)


def test_node_claim_surfaces_the_expert():
    out = _lab_event_to_chat({"type": "node_claim", "node": "s3", "specialist": "Pathway & enrichment specialist"})
    assert len(out) == 1 and out[0]["type"] == "lab_progress"
    assert "Pathway & enrichment specialist" in out[0]["text"] and "claimed" in out[0]["text"]


def test_concurrency_batch_surfaces_parallel_count():
    out = _lab_event_to_chat({"type": "concurrency_batch", "nodes": ["s4", "s5"]})
    assert len(out) == 1 and out[0]["type"] == "lab_progress"
    assert "2 independent tasks in parallel" in out[0]["text"]


def test_memory_events_surface():
    r = _lab_event_to_chat({"type": "memory_read", "specialist": "QC specialist"})
    assert r and r[0]["type"] == "chat_thinking" and "recalled" in r[0]["token"]
    f = _lab_event_to_chat({"type": "memory_reflect", "specialist": "QC specialist"})
    assert f and f[0]["type"] == "lab_progress" and "updated its lessons" in f[0]["text"]


def test_scientist_start_is_key_progress():
    out = _lab_event_to_chat(
        {"type": "scientist_start", "specialist": "Geneticist", "step": "Run DE"})
    assert len(out) == 1
    assert out[0]["type"] == "lab_progress"
    assert "Geneticist" in out[0]["text"] and "Run DE" in out[0]["text"]


def test_tool_start_activity_plus_live_status():
    # Tool chatter (incl. the noisy run_code loop) stays in the collapsible activity log,
    # but ALSO drives the transient live status so "running a tool" isn't a silent window.
    out = _lab_event_to_chat(
        {"type": "tool_start", "tool": "scanpy_qc", "args": {"min_genes": 200}})
    assert _kinds(out) == ["chat_thinking", "run_status"]
    assert out[0]["token"].endswith("\n")
    assert "scanpy_qc" in out[0]["token"]
    assert "scanpy_qc" in out[1]["text"] and out[1]["type"] == "run_status"


def test_model_call_becomes_live_status_only():
    # The long silent inference window: surfaced as a transient status line (not a
    # permanent feed entry) so the run reads as "reasoning", never frozen.
    out = _lab_event_to_chat({"type": "model_call", "step": 3})
    assert _kinds(out) == ["run_status"]
    assert "reasoning" in out[0]["text"].lower() and "3" in out[0]["text"]


def test_tool_result_goes_to_activity_only():
    out = _lab_event_to_chat(
        {"type": "tool_result", "tool": "scanpy_qc", "summary": "2000 cells"})
    assert _kinds(out) == ["chat_thinking"]
    assert "2000 cells" in out[0]["token"]


def test_run_code_success_emits_formatted_code_block():
    # The FINAL successful run_code snippet surfaces as a step_code block (formatted),
    # plus the activity line — never as raw "run_code: ok" in the visible feed.
    out = _lab_event_to_chat(
        {"type": "tool_result", "tool": "run_code", "summary": "ok",
         "args": {"code": "import scanpy as sc\nprint('hi')"}})
    assert _kinds(out) == ["chat_thinking", "step_code"]
    assert out[1]["code"].startswith("import scanpy")


def test_run_code_without_code_has_no_block():
    out = _lab_event_to_chat(
        {"type": "tool_result", "tool": "run_code", "summary": "ok", "args": {}})
    assert _kinds(out) == ["chat_thinking"]


def test_tool_error_surfaces_as_warning():
    out = _lab_event_to_chat(
        {"type": "tool_error", "tool": "scanpy_qc", "error": "boom"})
    assert _kinds(out) == ["chat_thinking", "lab_progress"]
    assert out[1]["level"] == "warning"
    assert "boom" in out[1]["text"]


def test_finish_surfaces_step_finding():
    out = _lab_event_to_chat({"type": "finish", "answer_preview": "Found 1200 DEGs"})
    assert _kinds(out) == ["lab_progress"]
    assert "Found 1200 DEGs" in out[0]["text"]


def test_finish_without_preview_is_silent():
    assert _lab_event_to_chat({"type": "finish", "answer_preview": ""}) == []


def test_critic_accept_emits_activity_and_step_summary():
    # On accept: a step summary line (result quality via the score) plus, when present, a
    # significance/notes line from the critique.
    out = _lab_event_to_chat(
        {"type": "critic", "verdict": "accept", "score": 0.91, "step": "Run DE",
         "critique": "Solid DE with clear markers."})
    assert _kinds(out) == ["chat_thinking", "lab_progress", "lab_progress"]
    assert "ACCEPT" in out[0]["token"] and "0.91" in out[0]["token"]
    assert out[1]["text"].startswith("✅ Step done") and "0.91" in out[1]["text"]
    assert out[1]["level"] == "success"
    assert "Solid DE" in out[2]["text"]


def test_critic_accept_without_critique_is_just_summary():
    out = _lab_event_to_chat(
        {"type": "critic", "verdict": "accept", "score": 0.88, "step": "Run DE"})
    assert _kinds(out) == ["chat_thinking", "lab_progress"]
    assert out[1]["text"].startswith("✅ Step done")


def test_critic_accept_shows_full_critique_including_the_however_caveat():
    # Regression: the critique used to be guillotined at 220 chars, cutting off the 'However, …'
    # caveat (the most useful half — what's still imperfect). The whole rationale must survive.
    critique = (
        "The step successfully reports the exact proportion of variants passing quality filters "
        "(95.69% PASS vs 4.31% excluded), which is explicitly backed by the stdout from the "
        "'VARIANT LANDSCAPE SUMMARY' run. However, the step did not break the counts down by "
        "chromosome or variant type, so the landscape summary is incomplete and should be extended.")
    assert len(critique) > 220
    out = _lab_event_to_chat(
        {"type": "critic", "verdict": "accept", "score": 0.70,
         "step": "Summarize the overall variant landscape", "critique": critique})
    sub = out[2]["text"]
    assert "However, the step did not break the counts down" in sub   # the caveat is no longer cut
    assert sub.rstrip().endswith("should be extended.")               # tail survives, no truncation


def test_critic_accept_caps_a_runaway_critique():
    long = "x" * 5000
    out = _lab_event_to_chat(
        {"type": "critic", "verdict": "accept", "score": 0.9, "step": "S", "critique": long})
    sub = out[2]["text"]
    assert sub.endswith("[…]") and len(sub) < 1300   # generous ceiling still guards the console


def test_critic_revise_has_no_key_acceptance():
    out = _lab_event_to_chat(
        {"type": "critic", "verdict": "revise", "score": 0.3, "step": "Run DE"})
    assert _kinds(out) == ["chat_thinking"]


def test_critic_missing_score_does_not_crash():
    out = _lab_event_to_chat({"type": "critic", "verdict": "revise", "step": "x"})
    assert _kinds(out) == ["chat_thinking"]
    assert "(?)" in out[0]["token"]


def test_lab_done_converged_is_success():
    out = _lab_event_to_chat(
        {"type": "lab_done", "converged": True, "accepted_steps": 3, "agenda": 3})
    assert out[0]["type"] == "lab_progress"
    assert out[0]["level"] == "success"
    assert "3/3" in out[0]["text"]


def test_lab_done_not_converged_is_warning():
    out = _lab_event_to_chat(
        {"type": "lab_done", "converged": False, "accepted_steps": 1, "agenda": 3})
    assert out[0]["level"] == "warning"


def test_plan_cancelled_is_warning_key_line():
    out = _lab_event_to_chat({"type": "plan_cancelled"})
    assert out[0]["type"] == "lab_progress" and out[0]["level"] == "warning"


def test_unknown_event_is_silent():
    assert _lab_event_to_chat({"type": "something_new"}) == []


def test_skills_loaded_surfaces_active_skill():
    out = _lab_event_to_chat({"type": "skills_loaded", "skills": [
        {"key": "celltype_annotation", "label": "Cell-type annotation",
         "tools": ["run_de", "run_enrichment"]}]})
    text = " ".join(p.get("text", "") for p in out)
    assert all(p["type"] == "lab_progress" for p in out)
    assert "Loaded preset pipeline" in text and "Cell-type annotation" in text
    assert "run_de" in text


def test_skills_loaded_none_says_from_scratch():
    out = _lab_event_to_chat({"type": "skills_loaded", "skills": []})
    assert any("No matching preset pipeline" in p.get("text", "") for p in out)


def test_steps_pruned_no_contrast_is_surfaced():
    out = _lab_event_to_chat({"type": "steps_pruned", "reason": "no_experimental_contrast",
                              "dropped": ["Run pathway enrichment for each class"]})
    text = " ".join(p.get("text", "") for p in out)
    assert "Dropped 1 pathway-enrichment" in text and "circular" in text
