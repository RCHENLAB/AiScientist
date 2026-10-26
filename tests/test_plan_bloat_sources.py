"""What made a DDX41 plan grow to 18 steps, pinned so it does not come back.

A plan for the mouse DDX41 retina (n = 1 library per arm, DDX41 1.6x deeper) carried:
  * a run_code step re-deriving the per-direction depth verdict run_depth_matched_de already
    returns — and applying it backwards (every UP gene demoted, DOWN genes filtered on a rho the
    tool says is not evidence). The planning prompt had told the PI that DOWN genes were "the most
    credible set", and the tool's own description still sold one rho rule for both directions;
  * a plan review that could not see what any tool returns, so it could not call that step (or a
    figures step re-drawing the tools' figures) redundant;
  * "species unknown" and every gene symbol in human upper case on a mouse matrix — the profile
    never said which species the symbols belong to.
"""

from __future__ import annotations

import json

import pytest

from aiscientist.agents.research_harness import HarnessContext, ResearchHarness, default_catalog
from aiscientist.agents.research_lab import (
    LabConfig, ResearchLab, _PLAN_REVIEW_CRITIC_SYSTEM, plan_tool_contracts,
)


def _profile(**extra):
    dr = {"cells": 15307, "genes": 33696, "dataset_kind": "h5ad_single_cell",
          "obs_categoricals": {"sampleid": {"n": 2, "values": ["DDX41", "WT"]},
                               "majorclass": {"n": 2, "values": ["Rod", "MG"]},
                               "orig.ident": {"n": 1, "values": ["0"]}},
          "design_by_arm": {"condition_column": "sampleid",
                            "cells_by_arm": {"DDX41": 7000, "WT": 8307},
                            "label_column": "majorclass",
                            "cells_by_label_and_arm": {"Rod": {"DDX41": 4084, "WT": 5973},
                                                       "MG": {"DDX41": 787, "WT": 535}},
                            "qc_median_by_arm": {"nCount_RNA": {"DDX41": 3078.0, "WT": 1916.0}},
                            "depth_imbalance": "median nCount_RNA differs 1.6x between arms"}}
    dr.update(extra)
    return dr


def _lab(dr, complete=None):
    ctx = HarnessContext(decisions={"dataset_result": dr}, tunnel_port=1, model="m")
    return ResearchLab(ctx, LabConfig(), complete_fn=complete or (lambda _m: "{}"),
                       scientist=ResearchHarness(catalog=default_catalog(),
                                                 chat_fn=lambda *_a: {"content": ""}))


# --- the depth paragraph and the tool description say the same thing ---------------------------

def test_planning_context_defers_direction_bookkeeping_to_the_tool():
    text = _lab(_profile())._dataset_context()
    assert "DEPTH IMBALANCE" in text
    assert "most credible set" not in text                # what the PI turned into a backwards rule
    assert "`depth_robust`" in text and "against_depth_untestable" in text
    assert "do NOT demote every up gene wholesale" in text
    assert "Do not plan a separate step to re-derive" in text


def test_depth_tool_description_states_the_per_direction_verdicts():
    from aiscientist.tools.run_depth_matched_de.tool import make_tool
    desc = make_tool().description
    assert "`depth_robust`" in desc and "`against_depth_untestable`" in desc
    assert "must not be used as a filter" in desc
    assert "no later step needs to re-derive it" in desc


# --- the plan review sees the contracts of the tools the plan names -----------------------------

def test_plan_tool_contracts_lists_only_the_named_tools():
    catalog = default_catalog()
    agenda = ["QC with `run_qc`", "Then something in prose only, naming run_de without backticks"]
    contracts = plan_tool_contracts(agenda, catalog)
    assert list(contracts) == ["run_qc"]
    assert contracts["run_qc"] == next(t.description for t in catalog if t.name == "run_qc")
    assert plan_tool_contracts(["no tools here"], catalog) == {}


def test_plan_review_hands_the_critic_the_named_tools_contracts():
    seen: list[dict] = []

    def complete(messages):
        if "Scientific Critic reviewing a DRAFT" in messages[0]["content"]:
            seen.append(json.loads(messages[1]["content"]))
        return json.dumps({"issues": [], "revised_agenda": []})

    lab = _lab(_profile(), complete)
    draft = ["QC with `run_qc`", "Markers with `run_de_markers`", "Draw the volcano plots again"]
    assert lab._plan_review("q", draft, lambda _e: None) == draft
    assert seen and set(seen[0]["tools_named_in_plan"]) == {"run_qc", "run_de_markers"}
    assert "tools_named_in_plan" in _PLAN_REVIEW_CRITIC_SYSTEM
    assert "Re-DERIVING is the same waste" in _PLAN_REVIEW_CRITIC_SYSTEM


def test_the_review_does_not_trade_a_dropped_step_for_a_reconciliation_step():
    """Measured on the 18-step plan: the PI dropped the redundant steps and, 9 times in 10, put back
    a 'reconciliation' step — its prompt said to — duplicating the claim audit run before writing."""
    from aiscientist.agents.research_lab import _PLAN_REVIEW_PI_SYSTEM
    assert "DO add a reconciliation step" not in _PLAN_REVIEW_PI_SYSTEM
    assert "claim audit" in _PLAN_REVIEW_PI_SYSTEM
    assert "missing analysis or reconciliation" not in _PLAN_REVIEW_CRITIC_SYSTEM


def test_a_review_reply_missing_its_closing_brace_is_still_read():
    """Measured: the Critic's ~15k-char reply ended `…"]` without the final `}`; the parse failed
    and five correct issues were dropped as "no issues"."""
    from aiscientist.agents.research_lab import _parse_verdict
    reply = json.dumps({"issues": ["step 8 re-derives the tool's verdict"],
                        "revised_agenda": ["a", "b"]})[:-1]
    assert _parse_verdict(reply) == {"issues": ["step 8 re-derives the tool's verdict"],
                                     "revised_agenda": ["a", "b"]}
    # cut INSIDE a string: closing it would invent text, so it stays unparsed
    assert _parse_verdict('{"issues": ["step 8 re-deri') is None
    assert _parse_verdict("no json here") is None


def test_an_unreadable_review_is_reported_not_passed_as_clean():
    events: list[dict] = []

    def complete(messages):
        if "Scientific Critic reviewing a DRAFT" in messages[0]["content"]:
            return '{"issues": ["step 2 duplicates step 1'          # cut mid-string
        raise AssertionError("no PI round after an unreadable review")

    draft = ["QC with `run_qc`", "Markers with `run_de_markers`"]
    assert _lab(_profile(), complete)._plan_review("q", draft, events.append) == draft
    assert [e["type"] for e in events] == ["plan_review_unparsed"]


# --- species from the gene identifiers ----------------------------------------------------------

h5py = pytest.importorskip("h5py")


def _var_h5(tmp_path, names):
    p = tmp_path / "v.h5"
    with h5py.File(p, "w") as h:
        g = h.create_group("var")
        g.attrs["_index"] = "_index"
        g.create_dataset("_index", data=[n.encode() for n in names])
    return p


@pytest.mark.parametrize("names,species,case", [
    (["Gfap", "Rho", "Actb", "Gapdh", "mt-Co1", "Rpl13a", "Ddx41", "Glul", "AY036118"], "mouse",
     "Title-case (e.g. Gfap)"),
    (["GFAP", "RHO", "ACTB", "GAPDH", "MT-CO1", "RPL13A", "DDX41", "GLUL", "C1orf112"], "human",
     "UPPER-case (e.g. GFAP)"),
    (["Gfap", "Rho", "Actb", "Mt-co1", "Mt-nd1", "Glul", "Rpl13a"], "rat", "Title-case (e.g. Gfap)"),
])
def test_species_is_read_off_the_symbol_case(tmp_path, names, species, case):
    from aiscientist.tools.datasets import _gene_symbol_species
    with h5py.File(_var_h5(tmp_path, names), "r") as h:
        out = _gene_symbol_species(h["var"])
    assert out["species_hint"] == species and out["symbol_case"] == case


def test_ensembl_ids_give_the_species_from_their_prefix(tmp_path):
    from aiscientist.tools.datasets import _gene_symbol_species
    with h5py.File(_var_h5(tmp_path, [f"ENSMUSG{i:011d}" for i in range(20)]), "r") as h:
        out = _gene_symbol_species(h["var"])
    assert out["identifiers"] == "ensembl" and out["species_hint"] == "mouse"


def test_the_planner_is_told_the_species_and_the_symbol_form():
    gs = {"identifiers": "symbols", "symbol_case": "Title-case (e.g. Gfap)", "species_hint": "mouse",
          "evidence": "30740 Title-case vs 466 upper-case symbols", "examples": ["Actb", "Gapdh"]}
    text = _lab(_profile(gene_symbols=gs))._dataset_context()
    assert "the species is mouse" in text
    assert "Write every gene symbol in a step in this form (Actb, Gapdh)" in text
    # no hint, no line — and never a guessed species
    assert "species is" not in _lab(_profile())._dataset_context()
