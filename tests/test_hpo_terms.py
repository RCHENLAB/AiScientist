"""Tests for upstream-agent HPO inference (no HITL) — IRD phenotype → HPO terms."""
from __future__ import annotations

import re

from bioagent.tools.hpo_terms import DEFAULT_IRD_HPO, infer_hpo_terms, load_hpo_table


def test_all_ids_are_wellformed_and_unique():
    table = load_hpo_table()
    assert len(table) >= 12
    ids = [t["id"] for t in table]
    assert len(ids) == len(set(ids))                     # no duplicate IDs
    assert all(re.fullmatch(r"HP:\d{7}", t["id"]) for t in table)   # well-formed HPO IDs
    assert all(t["keywords"] for t in table)             # every term has keywords


def test_default_when_no_phenotype_described():
    # A bare/generic prompt ⇒ the root IRD term, so Exomiser always has a phenotype (no HITL).
    assert infer_hpo_terms("annotate this VCF") == [DEFAULT_IRD_HPO]
    assert infer_hpo_terms("") == [DEFAULT_IRD_HPO]


def test_disease_names_map_to_terms():
    ids = dict(_ for _ in ()).fromkeys  # noqa: F841 - readability only
    assert ("HP:0000510", "Rod-cone dystrophy") in infer_hpo_terms("a patient with retinitis pigmentosa")
    assert ("HP:0007754", "Macular dystrophy") in infer_hpo_terms("Stargardt disease case")
    assert ("HP:0011516", "Achromatopsia") in infer_hpo_terms("suspected achromatopsia")


def test_symptoms_map_to_terms():
    got = {i for i, _ in infer_hpo_terms("child with night blindness and tunnel vision")}
    assert "HP:0000662" in got                            # nyctalopia
    assert "HP:0001133" in got                            # constriction of peripheral visual field


def test_no_default_returns_empty_on_miss():
    assert infer_hpo_terms("no phenotype here", default=False) == []


def test_multiple_terms_deduped_and_stable():
    terms = infer_hpo_terms("retinitis pigmentosa with night blindness and photophobia")
    ids = [i for i, _ in terms]
    assert len(ids) == len(set(ids))                      # de-duplicated
    assert "HP:0000510" in ids and "HP:0000662" in ids and "HP:0000613" in ids
