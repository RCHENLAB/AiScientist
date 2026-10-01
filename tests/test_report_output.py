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


# --- the reasoning layer has to reach the exported record ----------------------------------------
# Run c135ae589d96 raised one hypothesis, added a step to test it, and closed it as `supported`.
# technical_report.md mentioned hypotheses ZERO times and event_log.txt held 407 lines without one
# of them; the only trace was a JSON blob nobody reads. You cannot review what the record omits.

def _ns(hypotheses):
    import types
    return types.SimpleNamespace(hypotheses=hypotheses)


_H1 = {
    "id": "h1", "status": "supported",
    "statement": "The shifts are driven by a 1.6x sequencing depth imbalance, not regulation.",
    "prediction": "Pseudobulk aggregation will collapse the top log2FC values.",
    "test": "Run run_pseudobulk_de and see whether the ranking survives aggregation.",
    "origin_step": "**Descriptive differential expression** — rank genes with run_de",
    "evidence": ["Depth-matched contrasts yielded rho < 0.5 for all eight comparisons."],
    "tested_by": ["Run run_pseudobulk_de to aggregate expression by major class."],
}


def test_the_ledger_reaches_the_technical_report_with_its_provenance():
    from bioagent.gateway.app import _hypothesis_ledger_block
    out = _hypothesis_ledger_block(_ns([_H1]))
    assert "[h1] supported" in out
    assert "run_pseudobulk_de" in out                      # the step it ADDED to the plan
    assert "arose from" in out and "rho < 0.5" in out      # provenance + the evidence it closed on


def test_a_supported_hypothesis_with_no_rival_is_flagged_in_the_report_itself():
    # The whole failure in one line: with nothing to weigh against, "supported" means only
    # "not contradicted", and a reader must not have to work that out for themselves.
    from bioagent.gateway.app import _hypothesis_ledger_block
    out = _hypothesis_ledger_block(_ns([_H1]))
    assert "no competing explanation was recorded" in out and "not contradicted" in out


def test_the_flag_does_not_fire_once_a_rival_is_recorded():
    from bioagent.gateway.app import _hypothesis_ledger_block
    out = _hypothesis_ledger_block(_ns([dict(_H1, rival="DDX41 loss disrupts Muller junctions",
                                             discriminator="confined to MG vs uniform")]))
    assert "no competing explanation was recorded" not in out
    assert "competing explanation: DDX41 loss" in out and "tells them apart" in out


def test_an_empty_ledger_says_so_rather_than_rendering_nothing():
    from bioagent.gateway.app import _hypothesis_ledger_block
    assert "none" in _hypothesis_ledger_block(_ns([])).lower()
    assert "none" in _hypothesis_ledger_block(_ns(None)).lower()


def test_the_narrative_mirror_skips_only_what_the_log_chain_already_writes():
    # The mirror puts every lab_progress line into the exported log. These types are written by the
    # run loop's own chain, so mirroring them would double every entry (pi_agenda is the whole
    # agenda); the reasoning events must NOT be in the skip set or the bug comes straight back.
    from bioagent.gateway.app import _LOGGED_BY_EVENT_CHAIN
    assert "pi_agenda" in _LOGGED_BY_EVENT_CHAIN and "tool_result" in _LOGGED_BY_EVENT_CHAIN
    for reasoning in ("team_meeting_start", "expert_contribution", "meeting_critic",
                      "meeting_synthesis", "hypothesis_formed", "hypothesis_resolved",
                      "steps_pruned", "expert_tool"):
        assert reasoning not in _LOGGED_BY_EVENT_CHAIN, reasoning
