"""Offline tests for the per-run optional-GPU-capability record (gateway/app.py).

``_write_capability_log`` must ALWAYS record whether scGPT / the VL render review ran this run —
invoked or not, and why not — so a bundle can never again show zero trace of them (the failure mode
where a report claimed scGPT "did not execute" while nothing about scGPT was anywhere in the logs).
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("fastapi")

from bioagent.gateway.app import _scan_tool_invocation, _write_capability_log  # noqa: E402


def _conn(*, live=True, vl=False):
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(vlreview_enabled=vl, scgpt_image="/dfs3b/x/scgpt.sif"),
        executor=(object() if live else None), mock=False)


def _result(steps):
    return types.SimpleNamespace(rounds=[types.SimpleNamespace(scientist_result={"steps": steps})])


def test_scan_tool_invocation_finds_last_and_missing():
    r = _result([{"tool": "run_de", "status": "ok"},
                 {"tool": "scgpt_annotate", "result": {"status": "ok", "n_cells": 5}}])
    assert _scan_tool_invocation(r, "scgpt_annotate")["n_cells"] == 5
    assert _scan_tool_invocation(r, "run_enrichment") is None
    assert _scan_tool_invocation(None, "scgpt_annotate") is None


def test_capability_log_records_not_invoked_and_vl_disabled(tmp_path):
    events = []
    _write_capability_log(tmp_path, _result([{"tool": "run_de", "status": "ok"}]),
                          _conn(live=True, vl=False), lambda *a: events.append(a))
    txt = (tmp_path / "process" / "capabilities.log").read_text()
    assert "scGPT annotation: NOT INVOKED" in txt
    assert "VL render review: DISABLED" in txt
    assert any("Capability log" in a[2] for a in events)   # one-line summary reaches the event log


def test_capability_log_records_invoked_ok_and_vl_ran(tmp_path):
    (tmp_path / "process").mkdir(parents=True)
    (tmp_path / "process" / "visual_review.md").write_text("defects", encoding="utf-8")
    r = _result([{"tool": "scgpt_annotate",
                  "result": {"status": "ok", "n_cells": 12000, "predictions_csv": "data/scgpt_predictions.csv"}}])
    _write_capability_log(tmp_path, r, _conn(live=True, vl=True), lambda *a: None)
    txt = (tmp_path / "process" / "capabilities.log").read_text()
    assert "scGPT annotation: INVOKED, status=ok" in txt and "n_cells=12000" in txt
    assert "VL render review: ENABLED and ran" in txt and "visual_review.md" in txt


def test_capability_log_flags_vl_degraded_when_vision_model_did_not_load(tmp_path):
    # visual_review_pass1.json model="bbox-only" => the vision model never loaded; the capability
    # log must say DEGRADED, not "ENABLED and ran; pages read clean" (the false positive we fixed).
    import json
    (tmp_path / "process").mkdir(parents=True)
    (tmp_path / "process" / "visual_review_pass1.json").write_text(
        json.dumps({"clean": True, "model": "bbox-only",
                    "defects": [{"note": "VL review unavailable: ImportError: ...PyTorch..."}]}),
        encoding="utf-8")
    _write_capability_log(tmp_path, _result([{"tool": "run_de", "status": "ok"}]),
                          _conn(live=True, vl=True), lambda *a: None)
    txt = (tmp_path / "process" / "capabilities.log").read_text()
    assert "VL render review: ENABLED but DEGRADED" in txt
    assert "did NOT" in txt and "read clean" not in txt


def test_capability_log_records_scgpt_error_note(tmp_path):
    r = _result([{"tool": "scgpt_annotate", "result": {"status": "error", "error": "gres unsatisfiable"}}])
    _write_capability_log(tmp_path, r, _conn(live=True, vl=False), lambda *a: None)
    txt = (tmp_path / "process" / "capabilities.log").read_text()
    assert "INVOKED, status=error" in txt and "gres unsatisfiable" in txt


def test_capability_log_never_raises_on_bad_input(tmp_path):
    # A malformed conn (no .settings) must be swallowed — logging can never break a run.
    _write_capability_log(tmp_path, None, types.SimpleNamespace(), lambda *a: None)
