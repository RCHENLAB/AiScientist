"""The PI must not assemble a team, or hold a design meeting, blind.

Measured on the 26 production run bundles (2026-08-11). Of the 16 team runs, only 11 had a real
assignment to make (the other 5 were single-step runs, trivially one worker). Of those 11:

  * runs launched with a REAL question averaged 2-3 experts sharing the work 62/38 — reasonable
    specialisation, not a collapse;
  * **4 were launched with a continuation phrase** — "complete the research", "finish the
    research" — which is a normal way to use the console. In every one of those the PI produced
    generic titles ("Biostatistician", "Computational Statistician") and ONE member executed
    everything.

`_form_team` and `_team_meeting` only ever saw the question string, so on those runs they were
reasoning about nothing. Both now see the study: the dataset profile and the selected protocol,
which are resolved before this point because Axis B (skill selection) runs before Axis A (mode).
"""

from __future__ import annotations

from pathlib import Path

from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import LabConfig, ResearchLab

DATASET = {
    "cells": 21006, "genes": 33696,
    "obs_categoricals": {
        "sampleid": {"n": 2, "values": ["DDX41", "WT"]},
        "subclass": {"n": 12, "values": ["AC", "BC_OFF", "Cone", "MG", "RGC", "Rod"]},
    },
    "obs_keys": ["sampleid", "subclass", "percent.mt"],
}


def _lab(tmp_path: Path, capture: list, *, dataset=DATASET, skills=()) -> ResearchLab:
    class _Stub:
        catalog: list = []

        def add_tools(self, *_a, **_k):
            return None

    def fn(messages):
        capture.append(messages)
        return "[]"                      # unparseable team -> falls back, fine for these tests

    lab = ResearchLab(
        HarnessContext(decisions={"dataset_result": dataset} if dataset else {},
                       workspace=tmp_path),
        LabConfig(auto_select_skill=False), complete_fn=fn, scientist=_Stub())
    lab._skills = list(skills)
    return lab


class _Skill:
    def __init__(self, label): self.label = label


# --- the context itself --------------------------------------------------------


def test_the_dataset_reaches_team_formation(tmp_path):
    cap: list = []
    _lab(tmp_path, cap)._form_team("compare DDX41 vs WT per cell type", lambda _e: None)

    prompt = cap[0][1]["content"]
    assert "DDX41" in prompt and "subclass" in prompt, "the team is formed from the DATA too"
    assert "21006" in prompt


def test_the_selected_protocol_reaches_team_formation(tmp_path):
    cap: list = []
    lab = _lab(tmp_path, cap, skills=(_Skill("Differential expression"),))
    lab._form_team("compare KO vs WT", lambda _e: None)

    assert "Differential expression" in cap[0][1]["content"]


def test_a_continuation_phrase_is_flagged_so_the_model_ignores_its_wording(tmp_path):
    """"complete the research" describes no study. The model must be told to read the data
    instead of inventing a team from four words."""
    cap: list = []
    _lab(tmp_path, cap)._form_team("complete the research", lambda _e: None)

    prompt = cap[0][1]["content"]
    assert "continuation phrase" in prompt
    assert "DDX41" in prompt, "with no real question the dataset is ALL the model has"


def test_a_real_question_is_not_flagged(tmp_path):
    cap: list = []
    _lab(tmp_path, cap)._form_team(
        "Compare DDX41 mutant versus wild-type retina within each major cell class and interpret "
        "the changed genes", lambda _e: None)

    assert "continuation phrase" not in cap[0][1]["content"]


def test_no_dataset_still_produces_a_usable_prompt(tmp_path):
    """A literature-only study has no dataset — that must not break team formation."""
    cap: list = []
    _lab(tmp_path, cap, dataset=None)._form_team("What is known about CRB1?", lambda _e: None)

    prompt = cap[0][1]["content"]
    assert "CRB1" in prompt and "Assemble the team" in prompt


# --- the design meeting --------------------------------------------------------


def test_the_design_meeting_sees_the_study_not_just_the_question(tmp_path):
    from aiscientist.agents.research_lab import Specialist

    cap: list = []
    lab = _lab(tmp_path, cap, skills=(_Skill("Differential expression"),))
    lab.config = LabConfig(auto_select_skill=False, meeting_rounds=1)
    lab._team_meeting("complete the research", "How should we approach this?",
                      (Specialist("Retina expert", "retina"),), "design", lambda _e: None)

    expert_prompt = cap[0][1]["content"]
    assert "DDX41" in expert_prompt, "a design meeting over a bare phrase is a meeting about nothing"
    assert "Differential expression" in expert_prompt
    assert "continuation phrase" in expert_prompt


def test_team_formation_still_falls_back_when_the_reply_is_unusable(tmp_path):
    """Unchanged safety property: a garbled roster must never leave the run without experts."""
    cap: list = []
    lab = _lab(tmp_path, cap)
    team = lab._form_team("compare KO vs WT", lambda _e: None)

    assert team == lab.config.specialists


# --- non-single-cell modalities -------------------------------------------------

VCF = {
    "dataset_kind": "vcf_variants", "format": ".vcf",
    "n_samples": 1, "samples": ["NA12878"],
    "n_variants_sampled": 20000, "variant_count_truncated": True,
    "note": "variant calls — annotate with the variant_annotation workflow, NOT the scanpy line",
}


def test_a_vcf_profile_is_more_than_its_kind(tmp_path):
    """The cells/genes/obs block renders nothing for a VCF, so a variant study's ENTIRE profile
    used to be the line "Dataset profile: vcf_variants." — while dataset_result carried the sample
    count, the sample names, the variant scale and the routing note. 3 of the 4 production runs
    launched with a bare "complete the research" were VCFs, planned against exactly that."""
    profile = _lab(tmp_path, [], dataset=VCF)._dataset_context()

    assert "NA12878" in profile
    assert "20000" in profile
    assert "variant_annotation" in profile, "the inspection's routing note must survive"
    assert len(profile.splitlines()) >= 3


def test_the_vcf_profile_reaches_team_formation(tmp_path):
    cap: list = []
    _lab(tmp_path, cap, dataset=VCF)._form_team("complete the research", lambda _e: None)

    prompt = cap[0][1]["content"]
    assert "NA12878" in prompt and "continuation phrase" in prompt


def test_a_single_cell_profile_is_unchanged_by_the_variant_block(tmp_path):
    profile = _lab(tmp_path, [])._dataset_context()
    assert "Variant data" not in profile
    assert "21006 cells" in profile
