"""The report may not call a gene a marker of a lineage the run's curated reference gives to another.

Run c57071e7dc94's manuscript (2026-10-04, human retina, annotation with ``reference: "retina"``) said
"Notably, RLBP1 is a canonical rod photoreceptor marker, but in this dataset it discriminates the
Müller glia clusters…". RLBP1 (CRALBP) marks Müller glia and RPE; the labels were right, the writer's
explanation was not, and no layer checked what a gene marks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiscientist.agents.marker_claims import check_marker_claims
from aiscientist.agents.research_lab import CriticVerdict, LabRound, _marker_reference, verify_report_facts
from aiscientist.tools.api import available_references, load_marker_reference

# The paragraph as the prod report printed it (report.md line 100).
_PROD = (
    "Müller glia is the second major population (1,623 nuclei, 13.5%; clusters 3 and 8), anchored by "
    "RLBP1 and GLUL. Notably, RLBP1 is a canonical rod photoreceptor marker, but in this dataset it "
    "discriminates the Müller glia clusters rather than identifying a rod population, consistent with "
    "the near-absence of rods. Microglia is a small but coherent population (244 nuclei, 2.0%; cluster "
    "13), supported by C1QC, TYROBP, HLA-DRB5, TREM2, and CYBA.\n"
)
_FALSE = ("Notably, RLBP1 is a canonical rod photoreceptor marker, but in this dataset it discriminates "
          "the Müller glia clusters rather than identifying a rod population, consistent with the "
          "near-absence of rods.")


@pytest.fixture(scope="module")
def retina():
    assert "retina" in available_references()
    return load_marker_reference("retina")


def test_the_prod_sentence_is_removed_and_the_paragraph_kept(retina):
    out, issues = check_marker_claims(_PROD, retina)

    assert _FALSE not in out
    assert out == _PROD.replace(_FALSE + " ", "")
    assert out.startswith("Müller glia is the second major population")
    assert "anchored by RLBP1 and GLUL. Microglia is a small" in out
    assert len(issues) == 1
    assert "RLBP1" in issues[0] and "Rod photoreceptor" in issues[0] and "Muller glia" in issues[0]
    assert _FALSE in issues[0]                       # the Diagnostics quote what was removed


def test_verify_report_facts_removes_it_with_the_runs_reference(retina):
    out, issues = verify_report_facts(_PROD, {}, marker_reference=retina)
    assert _FALSE not in out
    assert any(i.startswith("marker claim:") for i in issues)

    # Without a reference (no curated annotation in the run) nothing is judged.
    assert verify_report_facts(_PROD, {}) == (_PROD, [])


def _round(result, verdict="accept"):
    return LabRound(1, 1, "Annotate clusters", "Generalist",
                    {"steps": [{"tool": "run_marker_annotation", "ok": True, "result": result}]},
                    CriticVerdict(verdict, 0.9, ""))


_USED = {"status": "ok", "reference": {"name": "retina", "species": "human", "auto_applied": False}}


def test_the_reference_comes_from_the_accepted_marker_annotation_step():
    assert _marker_reference([_round(_USED)])["tissue"] == "retina"
    assert _marker_reference([_round(_USED, "revise")]) is None              # a rejected step is not the run's
    assert _marker_reference([_round({"status": "ok", "reference": "WT"})]) is None   # run_de's reference arm
    assert _marker_reference([_round({"status": "ok", "reference": {}})]) is None     # a caller-written panel


def test_the_gateway_checks_the_final_manuscript_before_rendering():
    """The prod sentence was in the gateway writer's manuscript, not the lab's synthesis."""
    from aiscientist.gateway import app as gw

    result = SimpleNamespace(rounds=[_round(_USED)], claim_audit={})
    out = gw._correct_manuscript_markers(_PROD, result)
    assert _FALSE not in out and "anchored by RLBP1 and GLUL. Microglia" in out
    assert len(result.claim_audit["report_corrections"]) == 1

    no_reference = SimpleNamespace(rounds=[_round({"status": "ok"})], claim_audit={})
    assert gw._correct_manuscript_markers(_PROD, no_reference) == _PROD


@pytest.mark.parametrize("sentence", [
    "Notably, RLBP1 is a canonical rod photoreceptor marker.",
    "RLBP1, a canonical rod marker, was detected in clusters 3 and 8.",
    "RLBP1 is a marker of rod photoreceptors.",
    "RLBP1 marks rod photoreceptors.",
    "RHO and RLBP1 are canonical rod markers.",
    "*Rlbp1* marks rods in the mouse retina.",                      # mouse symbol, italicised
    "GFAP is a canonical Müller glia marker.",                        # an astrocyte discriminator
    # The run's claim audit, where the phrase first appeared (run_state.json claim_audit.claims[4]).
    "Müller glia are anchored by RLBP1 and GLUL expression, with RLBP1—a canonical rod photoreceptor "
    "marker—serving as the Müller glia discriminator rather than identifying a rod population.",
])
def test_wrong_attributions_are_removed(retina, sentence):
    md = f"Clusters 3 and 8 are glia. {sentence} Microglia form cluster 13.\n"
    out, issues = check_marker_claims(md, retina)
    assert out == "Clusters 3 and 8 are glia. Microglia form cluster 13.\n"
    assert len(issues) == 1


@pytest.mark.parametrize("sentence", [
    "RLBP1 is a canonical Müller glia marker.",
    "RLBP1 is a canonical Müller glia and RPE marker.",
    "RLBP1 is an RPE marker.",                                       # also_marks: CRALBP is in RPE too
    "RLBP1 is not a rod marker.",                                    # negated
    "RLBP1 is absent from the rod marker panel.",                    # not an attribution
    "RLBP1 is a marker of glia rather than rods.",                   # the claimed lineage is "glia"
    "RLBP1 is a glial marker.",                                      # a lineage the reference does not name
    "GFAP marks reactive Müller glia.",                              # a state (gliosis), not an identity
    "GRIK1 is an OFF cone bipolar marker.",                          # "cone" in a bipolar subtype
    "RHO marks rod photoreceptors.",
    "APOE is a microglia marker.",                                   # a panel gene, not a discriminator
])
def test_correct_or_unjudgeable_statements_are_kept(retina, sentence):
    md = f"Clusters 3 and 8 are glia. {sentence} Microglia form cluster 13.\n"
    assert check_marker_claims(md, retina) == (md, [])


def test_a_bullet_that_was_only_the_false_claim_goes_with_its_marker(retina):
    md = "Markers:\n- RLBP1 is a rod marker.\n- GLUL is a Müller glia marker.\n"
    out, issues = check_marker_claims(md, retina)
    assert out == "Markers:\n- GLUL is a Müller glia marker.\n"
    assert len(issues) == 1


def test_a_sentence_after_an_abbreviation_is_cut_whole(retina):
    md = "Glia (e.g. clusters 3 and 8) express RLBP1. RLBP1 is a canonical rod marker, e.g. in macula. Done.\n"
    out, _ = check_marker_claims(md, retina)
    assert out == "Glia (e.g. clusters 3 and 8) express RLBP1. Done.\n"
