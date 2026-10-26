"""An answer may not give one class label the name of another class of the same dataset.

Prod run 74873590ca7b (2026-10-23, mouse retina, Claude Haiku 5.5): `majorclass` has MG (Müller glia)
and Microglia as two classes, and the answer's headline read "**Microglia-associated (MG) nuclei** show
the clearest DDX41 excess". Every analysis step was `run_code`, so the writer's label list (built from
packaged tools' per-group outputs) was empty. Scripted models throughout; the profile and the answer's
lines are the run's own.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aiscientist.agents.label_claims import (
    check_label_conflation,
    confusable_pairs,
    dataset_labels,
    label_glossary,
    labels_prompt,
)
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import (
    CriticVerdict,
    LabConfig,
    LabRound,
    ResearchLab,
    _class_labels,
    _grounding_vocab,
    _label_reference,
    verify_report_facts,
)
from aiscientist.tools._lib.cellranger import tissue_hint
from aiscientist.tools.api import load_marker_reference

_COUNTS = {"AC": [392, 258], "BC": [801, 1708], "Cone": [112, 499], "Endothelial": [7, 27],
           "HC": [5, 11], "MG": [787, 535], "Microglia": [60, 24], "Pericyte": [4, 2],
           "RGC": [6, 8], "RPE": [2, 2], "Rod": [4084, 5973]}
LABELS = list(_COUNTS)
# The run's dataset profile, as run_state.json holds it (the parts these checks read).
PROFILE = {
    "design_by_arm": {"condition_column": "sampleid", "label_column": "majorclass",
                      "cells_by_label_and_arm": {k: {"DDX41": d, "WT": w} for k, (d, w) in _COUNTS.items()}},
    "obs_categoricals": {"majorclass": {"n": 11, "values": LABELS}, "celltype": {"n": 87, "values": []}},
    "tissue_hint": tissue_hint(["Rho", "Apoe", "Glul", "Sag", "Gnat1", "Pde6b"]),
}

# The answer's lines that name MG or Microglia, verbatim.
HEADLINE = ("- **Microglia-associated (MG) nuclei** show the clearest DDX41 excess: median +0.094 "
            "unadjusted and +0.080 after depth binning, with Cliff's δ +0.38 and +0.32.\n")
UNTOUCHED = (
    "**Major-class counts (post-QC, all nuclei retained):** Rod 10,057; BC 2,509; MG 1,322; AC 650; "
    "Cone 611; Microglia 84; Endothelial 34; HC 16; RGC 14; Pericyte 6; RPE 4.\n"
    "- **Six small classes** (Endothelial, HC, Microglia, Pericyte, RGC, RPE) are reported as counts "
    "and medians only, with no test.\n"
    "| MG | 535 | 0.027 [−0.038, 0.121] | 787 | 0.121 [0.036, 0.225] | Yes |\n"
    "| Microglia | 24 | 0.090 [0.009, 0.198] | 60 | 0.145 [0.069, 0.294] | No (WT n < 30) |\n"
    "Microglia's WT count (24) is a power gap, not a negative result.\n"
    "- In this pair of libraries, MG nuclei from DDX41 have a higher ISG score than WT MG nuclei.\n")


@pytest.fixture(scope="module")
def retina():
    return load_marker_reference("retina")


# --- the labels and what may spell them out -----------------------------------------------------

def test_the_labels_come_from_the_profile_whatever_tool_ran():
    assert dataset_labels(PROFILE) == ("majorclass", LABELS)
    only_categoricals = {"design_by_arm": {"label_column": "majorclass"},
                         "obs_categoricals": PROFILE["obs_categoricals"]}
    assert dataset_labels(only_categoricals) == ("majorclass", LABELS)
    assert dataset_labels(None) == ("", []) and dataset_labels({}) == ("", [])


def test_the_reference_spells_a_label_out_only_where_one_type_fits(retina):
    glossary = label_glossary(LABELS, retina)
    assert glossary["MG"] == "Muller glia"              # by its initials
    assert glossary["Microglia"] == "Microglia"
    assert glossary["RGC"] == "Retinal ganglion cell"
    assert glossary["BC"] == "Bipolar cell" and glossary["AC"] == "Amacrine cell"
    assert glossary["Rod"] == "Rod photoreceptor" and glossary["Endothelial"] == "Vascular endothelium"
    assert label_glossary(LABELS, None) == {}           # no reference: no label has an expansion
    # two labels that would both be "Muller glia": neither is expanded
    assert "MG" not in label_glossary(["MG", "Muller"], retina)


def test_the_look_alike_pair_is_named():
    assert confusable_pairs(LABELS) == [("MG", "Microglia")]


def test_the_reference_is_the_one_the_tissue_hint_names(retina):
    assert "curated retina reference" in PROFILE["tissue_hint"]
    assert _label_reference([], PROFILE)["tissue"] == retina["tissue"]
    assert _label_reference([], {"tissue_hint": ""}) is None
    assert _label_reference([], None) is None


def test_the_writer_gets_the_labels_when_every_step_was_run_code(retina):
    """The run's rounds carry no per-group tool output; before, the vocabulary was empty."""
    rounds = [LabRound(1, 1, "ISG score", "S",
                       {"steps": [{"tool": "run_code", "ok": True, "result": {"stdout": "x"}}],
                        "final_answer": "MG shifts up."}, CriticVerdict("accept", 0.8, ""))]
    assert _grounding_vocab(rounds) == ""
    vocab = _grounding_vocab(rounds, PROFILE, retina)
    assert "MG, Microglia" in vocab and "`majorclass`" in vocab
    assert "MG is not Microglia" in vocab
    assert "MG = Muller glia" in vocab and "Microglia = Microglia" not in vocab
    bare = _grounding_vocab(rounds, PROFILE, None)
    assert "MG is not Microglia" in bare
    assert "No reference spells out AC, BC, HC, MG, RGC, RPE" in bare


def test_labels_prompt_is_empty_without_labels():
    assert labels_prompt("majorclass", [], {}) == []


# --- the check -----------------------------------------------------------------------------------

@pytest.mark.parametrize("reference", ["retina", None])
def test_the_prod_headline_is_corrected_and_nothing_else_moves(reference):
    ref = load_marker_reference(reference) if reference else None
    out, issues = check_label_conflation(HEADLINE + UNTOUCHED, LABELS, ref, "majorclass")
    assert out == HEADLINE.replace("Microglia-associated (MG)", "MG") + UNTOUCHED
    assert out.startswith("- **MG nuclei** show the clearest DDX41 excess")
    assert issues == ["labels: the answer called class 'MG' 'Microglia-associated', but 'Microglia' "
                      "is a separate class of `majorclass` — written as 'MG'."]


@pytest.mark.parametrize("text, fixed", [
    ("MG (microglia) show the shift.", "MG show the shift."),
    ("**MG** (likely microglia) show the shift.", "**MG** show the shift."),
    ("MG (microglial cells) show the shift.", "MG show the shift."),
    ("the activated microglia (MG) shift", "the activated MG shift"),
    ("microglial nuclei (`MG`) shift", "`MG` shift"),
    ("Microglia (MG) and Rod", "MG and Rod"),
    ("immune cells (MG) shift", "MG shift"),                    # the reference's alias of Microglia
])
def test_another_class_name_on_a_label_is_removed(retina, text, fixed):
    out, issues = check_label_conflation(text, LABELS, retina, "majorclass")
    assert out == fixed and len(issues) == 1


@pytest.mark.parametrize("text", [
    "Müller glia (MG) show the shift.",                         # the label's own expansion
    "Muller glia (MG) show the shift.",
    "MG (n = 787) and Microglia (n = 60)",                      # counts
    "MG (vs Microglia) is larger.",                             # a contrast
    "MG (and Microglia) shift up.",
    "MG, not microglia (MG), shift up.",
    "not microglia (MG) shift up.",
    "non-microglial (MG) nuclei",
    "MG (microglia excluded) shift up.",
    "Microglia (WT n < 30)",
    "MG nuclei; Microglia nuclei are too few.",
])
def test_contrasts_counts_and_the_labels_own_name_are_left_alone(retina, text):
    assert check_label_conflation(text, LABELS, retina, "majorclass") == (text, [])


def test_a_class_name_inside_a_longer_name_is_not_the_class():
    """'rod bipolar' names a bipolar type, not Rod."""
    text = "rod bipolar (RBC) cells and Rod"
    assert check_label_conflation(text, ["Rod", "RBC", "Cone"]) == (text, [])


def test_a_name_that_is_no_class_of_this_dataset_is_not_judged(retina):
    """Without a Microglia class, the check has no other class to protect: the prompt governs."""
    text = "Microglia-associated (MG) nuclei"
    assert check_label_conflation(text, ["MG", "Rod", "Cone"], retina) == (text, [])
    assert check_label_conflation(text, ["MG"], retina) == (text, [])


# --- where it runs ---------------------------------------------------------------------------------

def test_verify_report_facts_takes_the_labels(retina):
    out, issues = verify_report_facts(HEADLINE, {}, class_labels=LABELS, label_column="majorclass",
                                      label_reference=retina)
    assert "**MG nuclei**" in out and any(i.startswith("labels:") for i in issues)
    assert verify_report_facts(HEADLINE, {}) == (HEADLINE, [])        # no labels, nothing judged


def _run_code_round():
    return LabRound(1, 2, "ISG score per nucleus", "S",
                    {"steps": [{"tool": "run_code", "ok": True, "result": {"stdout": "x"}},
                               {"tool": "run_enrichment", "ok": True, "result": {}}],
                     "final_answer": "MG shifts up; Microglia too few."},
                    CriticVerdict("accept", 0.8, ""))


def test_the_synthesis_is_given_the_labels_and_corrected(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CLAIM_AUDIT", "0")
    seen: dict[str, str] = {}

    def complete(messages):
        seen["writer"] = messages[1]["content"]
        return "# Report\n\n" + HEADLINE

    lab = ResearchLab(HarnessContext(decisions={"dataset_result": PROFILE}, tunnel_port=1, model="m"),
                      LabConfig(), complete_fn=complete)
    events: list[dict] = []
    out = lab._synthesize("Score the ISG signature by majorclass", [_run_code_round()], events.append)
    assert "MG is not Microglia" in seen["writer"] and "MG = Muller glia" in seen["writer"]
    assert "**MG nuclei** show the clearest DDX41 excess" in out
    assert "Microglia-associated" not in out
    checks = [e for e in events if e.get("type") == "report_fact_check"]
    assert checks and any(i.startswith("labels:") for i in checks[0]["issues"])


def test_the_gateway_checks_the_final_manuscript(retina):
    from aiscientist.gateway import app as gw

    result = SimpleNamespace(rounds=[_run_code_round()], claim_audit={})
    out = gw._correct_manuscript_markers("# Report\n\n" + HEADLINE, result, PROFILE)
    assert "**MG nuclei**" in out
    assert any(i.startswith("labels:") for i in result.claim_audit["report_corrections"])
    # Without the dataset profile there are no labels to protect: the text is left as it was.
    assert gw._correct_manuscript_markers(HEADLINE, SimpleNamespace(rounds=[_run_code_round()],
                                                                    claim_audit={})) == HEADLINE


def test_the_run_state_profile_round_trips_through_json():
    """run_state.json holds the profile as JSON; the labels survive the trip."""
    again = json.loads(json.dumps(PROFILE))
    assert _class_labels([], again) == ("majorclass", LABELS)
