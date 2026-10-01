"""The reference data the tools read sits where the deploy puts it.

Moving code between modules changes what ``Path(__file__)`` means. The split once pointed the gene
set lookup at ``tools/_lib/genesets`` while the deploy keeps the .gmt files in ``tools/genesets``,
which would have left enrichment with no libraries and no error in any test without .gmt files."""
from __future__ import annotations

from pathlib import Path

from aiscientist.tools import api, catalog

TOOLS = Path(catalog.TOOLS_DIR)


def test_gene_sets_are_read_from_tools_genesets(monkeypatch):
    monkeypatch.delenv("AISCIENTIST_GENESETS_DIR", raising=False)
    assert Path(api.genesets_dir()).resolve() == (TOOLS / "genesets").resolve()
    assert (TOOLS / "genesets" / "README.md").is_file()     # the folder the deploy fills with .gmt


def test_gene_sets_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(tmp_path))
    assert Path(api.genesets_dir()) == tmp_path


def test_bundled_reference_files_exist():
    for rel in ("map_phenotype_to_hpo/hpo_lexicon.tsv.gz", "map_phenotype_to_hpo/ird_hpo.tsv",
                "gene_panels/ird_retnet.txt"):
        assert (TOOLS / rel).is_file(), rel
