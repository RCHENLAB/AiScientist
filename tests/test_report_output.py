"""Offline tests for the report-output fixes — no cluster, no SSH, no network.

Covers two pure pieces:
- ``_promote_doc_title`` (gateway): the rendered PDF/DOCX must be titled by the report's
  own first H1 (content-derived), with that heading removed from the body so pandoc's
  title metadata doesn't duplicate it; missing/empty H1 falls back unchanged.
- ``CodeSandbox._env``: matplotlib's config/cache dir must be pinned to a WRITABLE,
  run-owned location (``MPLCONFIGDIR``) so plotting snippets don't die on the read-only
  container HOME ("Permission denied creating matplotlib cache directories").
"""

from __future__ import annotations

import pytest


# ---- _promote_doc_title ---------------------------------------------------

def _title():
    pytest.importorskip("fastapi")
    from bioagent.gateway.app import _promote_doc_title
    return _promote_doc_title


def test_promotes_first_h1_and_strips_it():
    promote = _title()
    md = "# Retinal Müller glia drive the stress response\n\nIntro paragraph.\n"
    title, body = promote(md, "FALLBACK")
    assert title == "Retinal Müller glia drive the stress response"
    assert "# Retinal Müller glia" not in body
    assert body.startswith("Intro paragraph.")


def test_fallback_when_no_h1():
    promote = _title()
    md = "## Subsection only\n\nNo top-level title here.\n"
    title, body = promote(md, "AiScientist Research Report")
    assert title == "AiScientist Research Report"
    assert body == md  # nothing lost


def test_does_not_match_h2():
    promote = _title()
    md = "Some preamble.\n\n## Methods\n\nText.\n"
    title, body = promote(md, "FB")
    assert title == "FB" and body == md


def test_empty_h1_falls_back():
    promote = _title()
    md = "# \n\nBody.\n"
    title, _ = promote(md, "FB")
    assert title == "FB"


def test_first_h1_wins_with_leading_content():
    promote = _title()
    md = "intro line\n# The Real Title\n\nbody\n"
    title, body = promote(md, "FB")
    assert title == "The Real Title"
    assert "# The Real Title" not in body
    assert "intro line" in body


# ---- CodeSandbox matplotlib cache dir -------------------------------------

def test_sandbox_pins_writable_mplconfigdir(tmp_path):
    from bioagent.agents.sandbox import CodeSandbox

    work = tmp_path / "work"
    sb = CodeSandbox(work_dir=str(work), artifacts_dir=str(tmp_path / "art"))
    env = sb._env()
    assert env["MPLBACKEND"] == "Agg"
    mpl = env["MPLCONFIGDIR"]
    # under the run-owned work dir, and actually created (writable)
    assert mpl.startswith(str(work))
    import os
    assert os.path.isdir(mpl)
    assert "XDG_CACHE_HOME" in env


def test_sandbox_mplconfigdir_falls_back_to_home_when_no_dirs(tmp_path, monkeypatch):
    from bioagent.agents.sandbox import CodeSandbox

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    sb = CodeSandbox()  # no work/artifacts dirs
    env = sb._env()
    # still set, and points somewhere (HOME-based) rather than crashing
    assert env.get("MPLCONFIGDIR")


# --- Degradation channel: analysis-step failures -> technical report ONLY --------------

def _fake_result(rounds, agenda=None):
    class _R:
        def __init__(self, rd): self._rd = rd
        def to_dict(self): return self._rd
    return type("Res", (), {"rounds": [_R(rd) for rd in rounds], "agenda": agenda or []})()


def test_summarize_degradations_flags_maxsteps_and_oom():
    pytest.importorskip("fastapi")   # gateway extra; offline CI subset doesn't install it
    from bioagent.gateway.app import _summarize_pipeline_degradations
    rounds = [
        {"step_index": 4, "step": "DE DDX41 vs WT",
         "verdict": {"verdict": "accept", "score": 0.95},
         "scientist_result": {"status": "incomplete", "stop_reason": "max_steps", "steps": [
             {"tool": "run_code", "result": {"status": "error", "returncode": -9, "error": "exited with code -9"}},
             {"tool": "run_de", "result": {"status": "ok"}},
         ]}},
    ]
    note = _summarize_pipeline_degradations(_fake_result(rounds))
    assert "Step 4" in note and "max_steps" in note
    assert "OUT_OF_MEMORY" in note and "manuscript renders the clean" in note


def test_summarize_degradations_empty_when_all_clean():
    pytest.importorskip("fastapi")   # gateway extra; offline CI subset doesn't install it
    from bioagent.gateway.app import _summarize_pipeline_degradations
    rounds = [
        {"step_index": 1, "step": "QC",
         "verdict": {"verdict": "accept", "score": 1.0},
         "scientist_result": {"status": "ok", "stop_reason": "model_final_text",
                              "steps": [{"tool": "run_scanpy_qc", "result": {"status": "ok"}}]}},
    ]
    assert _summarize_pipeline_degradations(_fake_result(rounds)) == ""


def test_step_failures_scans_steps_not_just_errors_list():
    pytest.importorskip("fastapi")   # gateway extra; offline CI subset doesn't install it
    from bioagent.gateway.app import _step_failures
    sr = {"errors": [], "steps": [
        {"tool": "run_code", "result": {"status": "error", "returncode": 1,
                                         "error": "Traceback\nValueError: bad"}},
        {"tool": "literature_search", "result": {"status": "ok"}},
    ]}
    fails = _step_failures(sr)
    assert fails == [("run_code", "ValueError: bad")]
