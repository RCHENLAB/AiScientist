"""A bare "why" must not mint a fresh study.

Live finding (2026-08-20): a plan review timed out, which clears ``last_run_id`` by design so the
next message opens a clean study. The user's next two messages — a plan revision, then the single
word "why" — therefore each started a FRESH run: staging the dataset from HPC3, loading the preset
pipeline, convening a design meeting whose experts fire Slurm literature jobs. The composer's
research/chat dial stays the user's explicit choice (a classifier would answer a real study with
fluent prose and no analysis behind it); this is only a floor under it.
"""

from __future__ import annotations

import pytest

# The precondition is that gateway.app IMPORTS — it pulls fastapi and paramiko at module scope,
# and guarding on one of them only moves the failure to the other. Without a guard the import
# raises during COLLECTION, which pytest treats as fatal and aborts the whole session: unguarded
# modules took CI from 1,525 passing tests to "33 skipped, 3 errors" and kept main red from
# 2026-08-20 to 2026-09-08. CI installs the gateway extra so these actually RUN; the guard is
# what keeps a leaner environment skipping cleanly instead of taking every other test down.
pytest.importorskip("aiscientist.gateway.app")

from aiscientist.gateway.app import _is_conversational_turn  # noqa: E402


def test_the_word_that_started_a_study():
    assert _is_conversational_turn("why")


def test_punctuation_and_case_do_not_smuggle_it_through():
    for q in ("Why?", "why!!", "  ok. ", "Hmm...", "Thanks!"):
        assert _is_conversational_turn(q), q


def test_chinese_conversational_turns_too():
    for q in ("为什么", "继续", "然后呢", "好的", "嗯嗯"):
        assert _is_conversational_turn(q), q


def test_a_short_request_is_still_a_request():
    """The guard is a closed list, not a length rule — two words can be real work."""
    for q in ("run QC", "DE by cell type", "cluster it", "分析这个数据集"):
        assert not _is_conversational_turn(q), q


def test_a_question_that_merely_starts_with_why_is_a_question():
    for q in ("why is the depth imbalance a problem?",
              "what changes between DDX41 and WT retina?",
              "how many cells survive QC?"):
        assert not _is_conversational_turn(q), q


def test_empty_input_is_not_treated_as_a_study_either():
    assert _is_conversational_turn("")
    assert _is_conversational_turn("   ")


# --- the model decides; the list is only the floor -------------------------------------------

class _Conn:
    """Enough Connection for _starts_no_study: an allocation means a warm model to ask."""
    def __init__(self, alloc=object()):
        self.alloc = alloc


class _Req:
    def __init__(self, question):
        self.question = question


def _with_model(monkeypatch, reply):
    from aiscientist.gateway import app as gw
    monkeypatch.setattr(gw, "_lab_llm", lambda conn: (lambda msgs: reply, None, None))


def test_the_model_recognises_a_request_the_word_list_never_could(monkeypatch):
    """"wait, I don't get it" is not on any list — the model is what makes this work."""
    from aiscientist.gateway.app import _starts_no_study
    _with_model(monkeypatch, '{"intent": "conversational", "confidence": 0.93, "reason": "no request"}')
    assert _starts_no_study(_Conn(), _Req("wait, I don't get it"))


def test_a_study_verdict_is_believed_even_for_a_listed_word(monkeypatch):
    from aiscientist.gateway.app import _starts_no_study
    _with_model(monkeypatch, '{"intent": "study", "confidence": 0.9, "reason": "asks for analysis"}')
    assert not _starts_no_study(_Conn(), _Req("why"))


def test_low_confidence_falls_to_the_floor_not_to_blocking(monkeypatch):
    """Uncertainty must cost compute, never strand the user."""
    from aiscientist.gateway.app import _starts_no_study
    _with_model(monkeypatch, '{"intent": "conversational", "confidence": 0.3, "reason": "unsure"}')
    assert not _starts_no_study(_Conn(), _Req("look at the rod cells again"))
    assert _starts_no_study(_Conn(), _Req("why"))       # floor still catches the bare word


def test_a_broken_model_reply_falls_to_the_floor(monkeypatch):
    from aiscientist.gateway.app import _starts_no_study
    _with_model(monkeypatch, "I think this is a question about cells")
    assert not _starts_no_study(_Conn(), _Req("run the enrichment"))
    assert _starts_no_study(_Conn(), _Req("ok"))


def test_a_raising_model_never_blocks_the_run(monkeypatch):
    from aiscientist.gateway import app as gw
    def _boom(conn):
        raise RuntimeError("no endpoint")
    monkeypatch.setattr(gw, "_lab_llm", _boom)
    assert not gw._starts_no_study(_Conn(), _Req("compare DDX41 with WT per cell type"))


def test_a_cold_session_is_not_woken_just_to_judge_a_sentence(monkeypatch):
    """No allocation → no GPU cold start; the floor answers."""
    from aiscientist.gateway import app as gw
    monkeypatch.setattr(gw, "_lab_llm", lambda conn: (_ for _ in ()).throw(AssertionError("asked")))
    assert gw._starts_no_study(_Conn(alloc=None), _Req("why"))
    assert not gw._starts_no_study(_Conn(alloc=None), _Req("run QC"))
