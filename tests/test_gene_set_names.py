"""Configured gene-set roots remain visible to platform discovery."""
from aiscientist.tools import api

def test_operator_gene_set_root_is_used_without_loading_science(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_GENESETS_DIR", str(tmp_path))
    assert api.genesets_dir() == tmp_path
