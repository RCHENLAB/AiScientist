"""The case note — the SECOND attachment: the patient's clinical description, alongside the VCF.

Why it is TEXT on the run request and not an upload: a run binds exactly ONE dataset, and for a
rare-disease case that slot must hold the VCF — a note uploaded as a dataset would displace it. The
note's only consumer, ``map_phenotype_to_hpo``, runs IN-PROCESS on the gateway (it is deliberately not
in ``_HPC_PHENOTYPE_TOOLS``), so the note needs neither a dataset row nor a Slurm bind.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from bioagent.agents.research_lab import LabResult, LabRound  # noqa: E402
from bioagent.agents.research_lab import CriticVerdict  # noqa: E402
from bioagent.gateway import app as gw_app  # noqa: E402
from bioagent.tools.hpo_terms.mapper import make_hpo_mapping_tool  # noqa: E402


class _Ctx:
    """A HarnessContext stand-in: no served model (so the mapper takes its lexical path) plus the
    run's decisions, which is where an attached note is carried."""

    tunnel_port = None
    model = ""
    workspace = None

    def __init__(self, **decisions):
        self.decisions = decisions


# --- the note itself --------------------------------------------------------------------------


def test_clean_case_note_trims_and_bounds():
    assert gw_app._clean_case_note("  night blindness  ") == "night blindness"
    assert gw_app._clean_case_note(None) == ""
    assert gw_app._clean_case_note("   ") == ""


def test_an_over_long_note_is_truncated_not_rejected():
    """A long referral letter must not fail the whole run — the clinically dense part is the start."""
    note = gw_app._clean_case_note("x" * (gw_app._MAX_CASE_NOTE_CHARS + 5000))
    assert len(note) == gw_app._MAX_CASE_NOTE_CHARS


def test_the_request_carries_a_note_without_touching_the_dataset_slot():
    """The whole point: the note rides alongside the dataset, it does not compete for the slot."""
    req = gw_app.LabRequest(connection_id="c", question="q",
                            dataset_path="/dfs3b/ruic20_lab/u/uploads/case.vcf.gz",
                            case_note="夜盲，视野缩窄")
    assert req.dataset_path.endswith("case.vcf.gz")      # the VCF still owns the dataset slot
    assert req.case_note == "夜盲，视野缩窄"
    assert gw_app.LabRequest(connection_id="c", question="q").case_note is None   # optional


# --- the mapper reads it ----------------------------------------------------------------------


def test_mapper_falls_back_to_the_attached_note():
    tool = make_hpo_mapping_tool()
    out = tool.executor({}, _Ctx(case_note="Patient with retinitis pigmentosa"))
    assert out["status"] == "ok"
    assert out["hpo_terms"] == ["HP:0000510"]
    assert out["text_source"] == "attached_case_note"       # auditable: which text was mapped


def test_the_attachment_takes_precedence_over_a_text_argument():
    """The attached case note is AUTHORITATIVE — the user attached it for this tool. A `text` argument
    the model improvises must NOT shadow it: in prod the model called the tool with a task-description
    `text` ("map the phenotype for this patient's variants") that extracted to nothing and silently
    bypassed the real note → n_observed:0, so the attachment looked like it "didn't work". So when a
    case note is attached it wins, regardless of what `text` the model passed (fixed 2026-07-17)."""
    tool = make_hpo_mapping_tool()
    out = tool.executor({"text": "achromatopsia"}, _Ctx(case_note="Patient with retinitis pigmentosa"))
    assert out["hpo_terms"] == ["HP:0000510"] and out["text_source"] == "attached_case_note"


def test_no_text_and_no_note_is_an_error_not_an_invented_phenotype():
    tool = make_hpo_mapping_tool()
    out = tool.executor({}, _Ctx())
    assert out["status"] == "error" and out["observed"] == []
    assert "HP:" not in out["error"]                        # it must not suggest a phenotype either
    # and with a dataset but no note, still nothing — a VCF alone never implies a phenotype
    assert tool.executor({}, _Ctx(dataset_path="/x/case.vcf.gz"))["status"] == "error"


def test_a_note_with_no_phenotype_maps_to_nothing():
    """An attached file that isn't a clinical note (a sample sheet, a README) must NOT yield terms —
    otherwise the attachment silently becomes a source of invented phenotypes."""
    tool = make_hpo_mapping_tool()
    out = tool.executor({}, _Ctx(case_note="Sample CASE_A sequenced on NovaSeq at 30x coverage."))
    assert out["status"] == "ok" and out["hpo_terms"] == []


# --- it survives a resume ----------------------------------------------------------------------


def test_run_state_persists_the_note_so_a_resume_keeps_the_phenotype(tmp_path):
    """A resume that lost the note would silently re-run the phenotype step against nothing."""
    rounds = [LabRound(1, 1, "Annotate", "S", {"final_answer": "x"}, CriticVerdict("accept", 0.9, "ok"))]
    res = LabResult("diagnose", ["Annotate"], rounds, True, 1, "final")
    gw_app._write_run_state(tmp_path, res, None,
                            {"dataset_path": "/data/case.vcf.gz", "case_note": "夜盲，无听力障碍"})

    state = json.loads((tmp_path / "process" / "run_state.json").read_text())
    assert state["case_note"] == "夜盲，无听力障碍"


def test_run_state_omits_the_note_when_there_was_none(tmp_path):
    rounds = [LabRound(1, 1, "Annotate", "S", {"final_answer": "x"}, CriticVerdict("accept", 0.9, "ok"))]
    res = LabResult("q", ["Annotate"], rounds, True, 1, "final")
    gw_app._write_run_state(tmp_path, res, None, {"dataset_path": "/data/a.h5ad"})
    assert "case_note" not in json.loads((tmp_path / "process" / "run_state.json").read_text())
