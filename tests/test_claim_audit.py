"""Claim audit: the checks run one short question at a time BEFORE writing, and bind both writers."""

from __future__ import annotations

import json

from aiscientist.agents import claim_audit as ca
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.research_lab import CriticVerdict, LabConfig, LabResult, LabRound, ResearchLab

# The real DDX41 profile's design facts (run 8847): one library per arm, 1.6x depth gap, snRNA-seq.
DDX41 = {
    "dataset_kind": "single_cell",
    "obs_categoricals": {"orig.ident": {"n": 1, "values": ["0"]}, "sampleid": {"n": 2, "values": ["DDX41", "WT"]},
                         "majorclass": {"n": 11, "values": []}},
    "design_by_arm": {
        "condition_column": "sampleid", "label_column": "majorclass",
        "cells_by_arm": {"DDX41": 6260, "WT": 9047},
        "qc_median_by_arm": {"nCount_RNA": {"DDX41": 3078.0, "WT": 1916.0}},
        "snrna_hint": "likely single-NUCLEUS data",
    },
}

RIBO = "DDX41 loss triggers a pan-retinal translational stress response (Rps20, Ubb up in all five classes)"
GFAP = "Muller glia show reactive gliosis (Gfap log2FC +6.0)"
ROD_IN_AC = "Phototransduction genes Rho, Gnat1 are down in amacrine cells"


def scripted_ask(calls):
    """Extraction returns three claims; each check answers like the models did when asked alone."""
    def ask(messages):
        calls.append(messages)
        sys, user = messages[0]["content"], messages[1]["content"]
        if "CANDIDATE HEADLINE FINDINGS" in sys:
            return json.dumps({"claims": [
                {"claim": RIBO, "step": 2, "genes": ["Rps20", "Ubb"], "cell_types": ["Rod", "Cone", "BC", "AC", "MG"]},
                {"claim": GFAP, "step": 2, "genes": ["Gfap"], "cell_types": ["MG"]},
                {"claim": ROD_IN_AC, "step": 2, "genes": ["Rho", "Gnat1"], "cell_types": ["AC"]}]})
        claim = user.split("Candidate finding:\n", 1)[1].split("\n", 1)[0]
        q = user.rsplit("Question: ", 1)[1]
        fail = ((claim == RIBO and ("SAME direction" in q or "causation" in q))
                or (claim == ROD_IN_AC and "NOT normally" in q))
        return json.dumps({"verdict": "fail" if fail else "pass", "reason": f"scripted for {claim[:20]}"})
    return ask


def test_design_facts_from_the_ddx41_profile():
    f = ca.design_facts(DDX41)
    assert f["depth_ratio"] == 1.61 and f["one_sample_per_arm"] and f["single_cell"]
    assert [c.name for c in ca.applicable_checks(f)] == ["global_shift", "cell_type_plausibility", "inference", "causal"]


def test_replicated_design_skips_the_pseudoreplication_check():
    replicated = json.loads(json.dumps(DDX41))
    replicated["obs_categoricals"]["mouse_id"] = {"n": 6, "values": []}
    assert not ca.design_facts(replicated)["one_sample_per_arm"]
    no_gap = json.loads(json.dumps(DDX41))
    no_gap["design_by_arm"]["qc_median_by_arm"]["nCount_RNA"] = {"DDX41": 2000.0, "WT": 1950.0}
    assert "global_shift" not in [c.name for c in ca.applicable_checks(ca.design_facts(no_gap))]


def test_audit_classifies_each_claim_by_the_checks_it_fails():
    calls: list = []
    res = ca.audit(scripted_ask(calls), "- Step 2 (DE): ...", "What changes?", DDX41)
    status = {c["claim"]: c["status"] for c in res["claims"]}
    assert status == {RIBO: "likely_technical", GFAP: "robust", ROD_IN_AC: "likely_technical"}
    assert len(calls) == 1 + 3 * 4                    # one extraction + 4 separate checks per claim
    # every check call carries the design facts from the profile, not from the model's memory
    check_users = [m[1]["content"] for m in calls[1:]]
    assert all("(1.6x)" in u and "NONE, one library per condition" in u for u in check_users)
    block = ca.render_block(res)
    robust, technical = block.split("LIKELY TECHNICAL")[0], block.split("LIKELY TECHNICAL")[1]
    assert GFAP in robust and RIBO in technical and ROD_IN_AC in technical
    assert "does NOT remove a sequencing-depth difference" in block
    assert "never call them robust" in block and "ROBUST:" not in block


def test_unparseable_answers_never_make_a_claim_robust():
    def ask(messages):
        if "CANDIDATE HEADLINE FINDINGS" in messages[0]["content"]:
            return json.dumps({"claims": [{"claim": GFAP}]})
        return "I think it is fine"
    res = ca.audit(ask, "x", "q", DDX41)
    assert res["claims"][0]["status"] == "unclear"


def _round(i, answer):
    return LabRound(1, i, f"step {i}", "sci", {"final_answer": answer, "steps": []}, CriticVerdict("accept", 0.9, ""))


def test_synthesize_runs_the_audit_first_and_binds_the_writer(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CLAIM_AUDIT", "1")
    calls: list = []
    ask = scripted_ask(calls)
    seen = {}

    def complete(messages):
        if "Principal Investigator writing the final research report" in messages[0]["content"]:
            seen["writer"] = messages[1]["content"]
            return "REPORT"
        return ask(messages)

    lab = ResearchLab(HarnessContext(decisions={"dataset_result": DDX41}, tunnel_port=1, model="m"),
                      LabConfig(), complete_fn=complete)
    events = []
    out = lab._synthesize("What changes?", [_round(2, "Rps20 and Ubb up everywhere; Gfap +6 in MG")], events.append)
    assert out.startswith("REPORT")
    assert "CLAIM AUDIT (binding" in seen["writer"] and GFAP in seen["writer"]
    ev = [e for e in events if e.get("type") == "claim_audit"][0]
    assert {c["status"] for c in ev["claims"]} == {"likely_technical", "robust"}
    assert lab._claim_audit["claims"] and LabResult.from_dict(
        LabResult("q", [], [], True, 1, "", claim_audit=lab._claim_audit).to_dict()).claim_audit == lab._claim_audit


def test_audit_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_CLAIM_AUDIT", "0")
    seen = {}

    def complete(messages):
        seen.setdefault("n", 0)
        seen["n"] += 1
        return "REPORT"

    lab = ResearchLab(HarnessContext(decisions={"dataset_result": DDX41}, tunnel_port=1, model="m"),
                      LabConfig(), complete_fn=complete)
    lab._synthesize("q", [_round(1, "x")], lambda e: None)
    assert seen["n"] == 1 and lab._claim_audit == {}


def test_technical_report_section_and_manuscript_block():
    from aiscientist.gateway import app as gw
    res = ca.audit(scripted_ask([]), "- Step 2 (DE): ...", "q", DDX41)
    lab_result = LabResult("q", [], [], True, 1, "", claim_audit=res)
    section = gw._claim_audit_section(lab_result)
    assert section.startswith("## Claim audit") and "likely technical: caveat only" in section
    assert "robust: may be headlined" in section and "global_shift:" in section


def test_block_carries_the_authoritative_design_numbers():
    d = json.loads(json.dumps(DDX41))
    d["design_by_arm"]["cells_by_label_and_arm"] = {"AC": {"DDX41": 392, "WT": 258}, "Rod": {"DDX41": 4084, "WT": 5973}}
    res = ca.audit(scripted_ask([]), "- Step 2 (DE): ...", "q", d)
    block = ca.render_block(res)
    assert "1.61-fold" in block and "AC DDX41 392, WT 258" in block
    # The profile counts the uploaded file: stated as such, and only a count ABOVE it is ruled out.
    assert "IN THE UPLOADED FILE, before QC" in block and "ABOVE the uploaded-file count is wrong" in block


def test_block_puts_the_tested_counts_beside_the_file_counts():
    """f3731e0b7136: told the file's counts were the only authority, the writers overrode run_de's
    post-QC counts and reported Endothelial '7 DDX41 / 27 WT' below the floor (it had tested 5 / 22)."""
    d = json.loads(json.dumps(DDX41))
    d["design_by_arm"]["cells_by_label_and_arm"] = {"Endothelial": {"DDX41": 7, "WT": 27}}
    res = ca.audit(scripted_ask([]), "- Step 2 (DE): ...", "q", d,
                   tested_counts={"Endothelial": {"DDX41": 5, "WT": 22}})
    block = ca.render_block(res)
    assert "Endothelial DDX41 7, WT 27. Use these only to describe the data as uploaded" in block
    assert "AFTER QC" in block and "Endothelial DDX41 5, WT 22" in block
    assert "including which classes fell below a cell floor, MUST use these" in block
    assert "matches neither table is wrong" in block
    assert res["facts"]["cells_tested_by_label_and_arm"] == {"Endothelial": {"DDX41": 5, "WT": 22}}
