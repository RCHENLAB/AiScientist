"""Which file of a shared dataset a request means (RCHENLAB/AiScientist issue 114): the member is
resolved from the request and the members' facets, never from file size.

The HECA file names below are the real ones: 13 .h5ad covering HRCA scRNA/snRNA, Iris, Lens, ON,
OcularSurface, RPEChoroid and TMCB, where HRCA_snRNA_allcells.h5ad (19.45 GB) is by far the largest.
``_files()`` gives them the facets a publish infers from the names (``HRCA``, ``ON``...);
``_heca_published()`` gives them the tissue facets the maintainers set on the Eye Server's HECA
versions (``retina``, ``optic nerve``, ``RPE/choroid``...).
"""

from __future__ import annotations

import pytest

from aiscientist.gateway import shared_members as sm

GB = 10 ** 9
HECA = {
    "HRCA_snRNA_allcells.h5ad": 19.45 * GB, "HRCA_scRNA_allcells.h5ad": 4.2 * GB,
    "Iris_snRNA_allcells.h5ad": 0.9 * GB, "Iris_scRNA_allcells.h5ad": 0.4 * GB,
    "Lens_snRNA_allcells.h5ad": 0.5 * GB, "Lens_scRNA_allcells.h5ad": 0.3 * GB,
    "ON_snRNA_allcells.h5ad": 4.34 * GB,
    "OcularSurface_snRNA_allcells.h5ad": 1.1 * GB, "OcularSurface_scRNA_allcells.h5ad": 0.8 * GB,
    "RPEChoroid_snRNA_allcells.h5ad": 2.0 * GB, "RPEChoroid_scRNA_allcells.h5ad": 1.0 * GB,
    "TMCB_snRNA_allcells.h5ad": 5.13 * GB, "TMCB_scRNA_allcells.h5ad": 0.7 * GB,
}


def _files(names=HECA):
    out = []
    for name, size in names.items():
        tissue, modality = name.split("_")[:2]
        out.append({"name": name, "size_bytes": int(size), "facets": {"tissue": tissue, "modality": modality}})
    return out


def test_use_hrca_scrna_selects_scrna_even_though_snrna_is_larger():
    got = sm.select("Use HRCA scRNA for the cell-type markers", _files())
    assert (got.status, got.chosen) == ("chosen", ["HRCA_scRNA_allcells.h5ad"])
    assert HECA["HRCA_snRNA_allcells.h5ad"] > HECA["HRCA_scRNA_allcells.h5ad"]


def test_the_equivalent_snrna_request_selects_snrna():
    for text in ("Use HRCA snRNA", "cluster the HRCA single-nucleus RNA data", "HRCA sn-RNA-seq markers"):
        got = sm.select(text, _files())
        assert (got.status, got.chosen) == ("chosen", ["HRCA_snRNA_allcells.h5ad"]), text


def test_this_retina_atlas_offers_both_retina_files_and_nothing_else():
    got = sm.select("Annotate cell types in this retina atlas", _files())
    assert got.status == "ambiguous" and got.chosen == []
    assert sorted(got.candidates) == ["HRCA_scRNA_allcells.h5ad", "HRCA_snRNA_allcells.h5ad"]
    assert not any(n.startswith(("Iris", "Lens", "RPE", "TMCB", "ON_")) for n in got.candidates)


def test_a_two_atlas_request_binds_two_explicit_files_in_the_order_named():
    got = sm.select("Compare the retina snRNA atlas with the retina scRNA atlas", _files())
    assert (got.status, got.chosen) == ("chosen", ["HRCA_snRNA_allcells.h5ad", "HRCA_scRNA_allcells.h5ad"])
    got = sm.select("compare HRCA scRNA against HRCA snRNA", _files())
    assert got.chosen == ["HRCA_scRNA_allcells.h5ad", "HRCA_snRNA_allcells.h5ad"]


def test_a_file_named_in_the_request_is_that_file():
    for text in ("analyse HRCA_scRNA_allcells.h5ad", "analyse HRCA_scRNA_allcells", "use hrca_scrna_allcells.h5ad please"):
        got = sm.select(text, _files())
        assert (got.status, got.chosen, got.how) == ("chosen", ["HRCA_scRNA_allcells.h5ad"], "named in the request")


def test_a_tissue_with_one_file_is_chosen_and_an_assay_alone_lists_that_assay():
    assert sm.select("what is in the optic nerve atlas?", _files()).chosen == ["ON_snRNA_allcells.h5ad"]
    assert sm.select("ON snRNA", _files()).chosen == ["ON_snRNA_allcells.h5ad"]
    got = sm.select("use scRNA", _files())
    assert got.status == "ambiguous" and set(got.candidates) == {n for n in HECA if "_scRNA_" in n}
    got = sm.select("analyse the iris", _files())
    assert got.status == "ambiguous" and sorted(got.candidates) == ["Iris_scRNA_allcells.h5ad", "Iris_snRNA_allcells.h5ad"]


def test_a_tissue_nothing_covers_is_never_replaced_by_another():
    # "liver" is no tissue of the dataset: nothing is chosen, the user is asked (here among the snRNA files)
    got = sm.select("analyse the liver snRNA atlas", _files())
    assert got.status == "ambiguous" and got.chosen == [] and set(got.candidates) == {n for n in HECA if "_snRNA_" in n}
    got = sm.select("show ON scRNA", _files())
    assert got.status == "ambiguous" and "no file" in got.reason and set(got.candidates) == set(HECA)  # ON has snRNA only


def test_a_request_that_says_nothing_about_the_file_asks_rather_than_taking_the_largest():
    for text in ("cluster the data", "what is based on this?", ""):
        got = sm.select(text, _files())
        assert got.status == "ambiguous" and got.chosen == [] and len(got.candidates) == 13, text


def test_a_dataset_with_one_analysable_file_needs_no_question():
    one = _files({"Iris_snRNA_allcells.h5ad": GB})
    assert sm.select("anything", one).chosen == ["Iris_snRNA_allcells.h5ad"]
    assert sm.select("anything", []).status == "ambiguous"


def test_the_word_on_is_not_the_optic_nerve():
    got = sm.select("build a model based on the data and report on markers", _files())
    assert got.status == "ambiguous" and "ON_snRNA_allcells.h5ad" in got.candidates and len(got.candidates) == 13
    assert sm.select("optic nerve", _files()).chosen == ["ON_snRNA_allcells.h5ad"]


def test_members_without_facets_are_not_guessed():
    plain = [{"name": "a.h5ad", "size_bytes": 5 * GB, "facets": {}}, {"name": "b.h5ad", "size_bytes": 1, "facets": {}}]
    got = sm.select("use the retina atlas", plain)
    assert got.status == "ambiguous" and got.candidates == ["a.h5ad", "b.h5ad"]
    assert sm.select("use b.h5ad", plain).chosen == ["b.h5ad"]


def test_describe_names_the_file_tissue_assay_cells_and_size():
    line = sm.describe({"name": "HRCA_scRNA_allcells.h5ad", "size_bytes": int(4.2 * GB),
                        "facets": {"tissue": "HRCA", "modality": "scRNA", "n_obs": 1234567}})
    assert line == "HRCA_scRNA_allcells.h5ad — HRCA, scRNA, 1,234,567 cells, 4.2 GB"


# The tissue facets of the published HECA versions (v2026.11 and v2026.11.1 on the Eye Server), set by
# the maintainers' facets.json: not the file-name prefixes.
HECA_TISSUES = {"HRCA": "retina", "Iris": "iris", "Lens": "lens", "ON": "optic nerve",
                "OcularSurface": "ocular surface", "RPEChoroid": "RPE/choroid",
                "TMCB": "trabecular meshwork / ciliary body"}


def _heca_published():
    out = []
    for name, size in HECA.items():
        prefix, modality = name.split("_")[:2]
        out.append({"name": name, "size_bytes": int(size),
                    "facets": {"tissue": HECA_TISSUES[prefix], "modality": modality, "n_obs": 1000}})
    return out


@pytest.mark.parametrize("text, chosen", [
    ("Use HRCA scRNA for the cell-type markers", ["HRCA_scRNA_allcells.h5ad"]),
    ("Use HRCA snRNA", ["HRCA_snRNA_allcells.h5ad"]),
    ("compare HRCA scRNA against HRCA snRNA", ["HRCA_scRNA_allcells.h5ad", "HRCA_snRNA_allcells.h5ad"]),
    ("Compare the retina snRNA atlas with the retina scRNA atlas", ["HRCA_snRNA_allcells.h5ad", "HRCA_scRNA_allcells.h5ad"]),
    ("retina scRNA", ["HRCA_scRNA_allcells.h5ad"]),
    ("Use the ON snRNA atlas", ["ON_snRNA_allcells.h5ad"]),
    ("optic nerve atlas", ["ON_snRNA_allcells.h5ad"]),
    ("TMCB snRNA markers", ["TMCB_snRNA_allcells.h5ad"]),
    ("trabecular meshwork snRNA", ["TMCB_snRNA_allcells.h5ad"]),
    ("ciliary body scRNA", ["TMCB_scRNA_allcells.h5ad"]),
    ("RPE scRNA", ["RPEChoroid_scRNA_allcells.h5ad"]),
    ("choroid snRNA", ["RPEChoroid_snRNA_allcells.h5ad"]),
    ("retinal pigment epithelium scRNA", ["RPEChoroid_scRNA_allcells.h5ad"]),
    ("cornea scRNA", ["OcularSurface_scRNA_allcells.h5ad"]),
    ("ocular surface snRNA", ["OcularSurface_snRNA_allcells.h5ad"]),
    ("lens scRNA", ["Lens_scRNA_allcells.h5ad"]),
    ("the retina and the lens snRNA", ["HRCA_snRNA_allcells.h5ad", "Lens_snRNA_allcells.h5ad"]),
])
def test_the_published_heca_facets_resolve_like_the_file_names(text, chosen):
    got = sm.select(text, _heca_published())
    assert (got.status, got.chosen) == ("chosen", chosen), (text, got)
    # ...and the same request reads the same way on facets inferred from the names.
    assert sm.select(text, _files()).chosen == chosen, text


def test_the_published_heca_facets_still_ask_when_the_request_is_not_specific():
    got = sm.select("Annotate cell types in this retina atlas", _heca_published())
    assert got.status == "ambiguous" and sorted(got.candidates) == ["HRCA_scRNA_allcells.h5ad", "HRCA_snRNA_allcells.h5ad"]
    assert "retina" in got.reason
    got = sm.select("build a model based on the snRNA data", _heca_published())   # "on" is not the optic nerve
    assert got.status == "ambiguous" and len(got.candidates) == 7
    got = sm.select("show ON scRNA", _heca_published())                          # the optic nerve has snRNA only
    assert got.status == "ambiguous" and "no file" in got.reason and "optic nerve" in got.reason
