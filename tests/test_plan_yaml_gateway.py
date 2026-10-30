"""The plan document on the wire: the edit action and the chat lines.

The lab side is pinned in ``test_plan_yaml_flow.py`` and the plan card's editor in
``test_console_plan_card_js.py``. Here: ``/api/lab/plan`` hands an edited document to the waiting
run without disturbing the other actions, and the events an edit and a finished run produce are
readable in the chat.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from aiscientist.gateway import app as gw_app  # noqa: E402


@pytest.fixture(autouse=True)
def _accounts_off(monkeypatch):
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", False)


def _client():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(gw_app.app)


def _conn():
    import asyncio

    from aiscientist.gateway.settings import HPCSettings

    loop = asyncio.new_event_loop()
    conn = gw_app.Connection(HPCSettings(), mock=True, loop=loop, username="tester")
    conn.pending_plan = {"kind": "agenda", "payload": ["a", "b"]}
    gw_app.CONNECTIONS[conn.id] = conn
    return conn, loop


def test_an_edit_reaches_the_waiting_run_with_its_document():
    conn, loop = _conn()
    try:
        r = _client().post("/api/lab/plan", json={
            "connection_id": conn.id, "action": "edit", "plan_yaml": "steps:\n  - does: a\n"})
        assert r.status_code == 200 and r.json()["action"] == "edit"
        assert conn.plan_event.is_set()
        assert conn.plan_value == {"action": "edit", "feedback": "", "plan_yaml": "steps:\n  - does: a\n"}
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def test_a_reply_in_words_carries_the_edit_that_was_not_applied_and_only_then():
    """Typed in the document but not applied, then a change asked for in words: the lab applies the
    edit first and makes the change on the edited plan, so the edit has to reach it."""
    for sent, expected in (("steps:\n  - does: a\n", {"plan_yaml": "steps:\n  - does: a\n"}), ("", {})):
        conn, loop = _conn()
        try:
            r = _client().post("/api/lab/plan", json={
                "connection_id": conn.id, "action": "revise", "feedback": "drop step 2", "plan_yaml": sent})
            assert r.status_code == 200 and r.json()["action"] == "revise"
            assert conn.plan_value == {"action": "revise", "feedback": "drop step 2", **expected}
        finally:
            gw_app.CONNECTIONS.pop(conn.id, None)
            loop.close()


def test_a_document_far_too_large_to_be_a_plan_is_refused_and_the_review_keeps_waiting():
    conn, loop = _conn()
    try:
        r = _client().post("/api/lab/plan", json={
            "connection_id": conn.id, "action": "edit", "plan_yaml": "x" * 200_001})
        assert r.status_code == 413
        assert not conn.plan_event.is_set() and conn.pending_plan is not None
    finally:
        gw_app.CONNECTIONS.pop(conn.id, None)
        loop.close()


def _text(ev: dict) -> str:
    return "\n".join(str(p.get("text") or p.get("token") or "") for p in gw_app._lab_event_to_chat(ev))


def test_an_applied_edit_is_listed_change_by_change():
    text = _text({"type": "plan_edited", "steps": [2], "changes": [
        {"kind": "setting", "step": 2, "id": "s2", "param": "resolution", "before": 0.5, "after": 1.2},
        {"kind": "removed", "step": 3, "id": "s3", "before": "**Enrichment** — ORA"}],
        "notes": [{"kind": "text_states_old_value", "step": 2, "id": "s2", "param": "resolution",
                   "before": 0.5, "after": 1.2}]})
    assert "step 2: resolution 0.5 → 1.2" in text
    assert "step 3 removed" in text
    assert "The setting is what runs" in text


def test_a_refused_edit_says_the_plan_is_unchanged_and_why():
    text = _text({"type": "plan_edit_rejected",
                  "problems": ["step 2: `run_clustering` has no setting `resolutionn` (it takes: n_pcs)."]})
    assert "the plan is unchanged" in text and "resolutionn" in text


def test_the_plan_check_names_each_step_that_did_not_run_as_approved():
    text = _text({"type": "plan_adherence", "as_planned": 1, "total": 3, "ok": False,
                  "steps": [
                      {"step": 1, "title": "QC", "tool": "run_scanpy_qc", "status": "as_planned", "detail": []},
                      {"step": 2, "title": "Clustering", "tool": "run_clustering", "status": "settings_differ",
                       "detail": [{"param": "resolution", "approved": 1.0, "used": 0.5}]},
                      {"step": 3, "title": "Enrichment", "tool": "run_enrichment", "status": "not_run",
                       "detail": []}],
                  "unplanned": ["**Ambient audit** — an extra look"]})
    assert "1 of 3 approved steps ran as approved" in text
    assert "resolution: approved 1.0, used 0.5" in text
    assert "step 3 (Enrichment): did not run" in text
    assert "not in the approved plan: Ambient audit" in text


def test_a_step_approved_during_the_run_is_listed_but_is_not_a_warning():
    payloads = gw_app._lab_event_to_chat({
        "type": "plan_adherence", "as_planned": 2, "total": 2, "ok": True, "steps": [],
        "added": ["**Doublet check** — approved when asked"], "unplanned": []})
    assert len(payloads) == 1 and payloads[0]["level"] == "info"
    assert "added during the run with your approval: Doublet check" in payloads[0]["text"]


def test_a_clean_plan_check_is_one_line():
    payloads = gw_app._lab_event_to_chat({"type": "plan_adherence", "as_planned": 2, "total": 2, "ok": True,
                                          "steps": [], "unplanned": []})
    assert len(payloads) == 1 and payloads[0]["level"] == "info"


def test_an_enforced_setting_is_shown_with_both_values():
    text = _text({"type": "plan_setting_enforced", "tool": "run_clustering",
                  "overrode": [{"param": "resolution", "approved": 1.2, "model": 0.5}]})
    assert "resolution = 1.2" in text and "0.5" in text


def test_a_dropped_setting_names_why_in_words():
    # A multi-library step's species and samples are stated in its text, so "does not state them"
    # would be wrong for them (#56 re-test, B2).
    text = _text({"type": "plan_bind_dropped", "dropped": [
        {"step": 1, "tool": "run_cellqc", "param": "species", "value": "human",
         "why": "multiple_choices_in_text"},
        {"step": 1, "tool": "run_cellqc", "param": "samples", "value": ["Sample3_GSM5676874"],
         "why": "incomplete_list"},
        {"step": 2, "tool": "run_de", "param": "padj", "value": 0.01, "why": "not_in_text"}]})
    assert "step 1 species='human' (the step names more than one of its choices)" in text
    assert "step 1 samples=['Sample3_GSM5676874'] (the step names items this list leaves out)" in text
    assert "step 2 padj=0.01 (the step's text does not state it)" in text
    assert "does not state them" not in text
