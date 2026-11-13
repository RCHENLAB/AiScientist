"""Retiring the CLI preserves configured references and filtering semantics."""
from types import SimpleNamespace

import pytest

from aiscientist.agents.mcp_analysis import AnalysisAdapter, setting_paths, setting_values


def server(*names):
    return SimpleNamespace(name="variant-annotation", settings_env=names)


def test_existing_reference_configuration_and_explicit_empty_build_override(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_VEP_CACHE_DIR_GRCH37", "/reference/old37")
    monkeypatch.setenv("VARIANT_VEP_CACHE_DIR_GRCH38", "/reference/new38")
    monkeypatch.setenv("AISCIENTIST_VEP_PLUGINS", "1")
    monkeypatch.setenv("AISCIENTIST_VEP_CADD_SNV_GRCH37", "/reference/cadd37.gz")
    monkeypatch.setenv("VARIANT_VEP_CADD_SNV_GRCH37", "")
    s = server("VARIANT_VEP_CACHE_DIR_GRCH37", "VARIANT_VEP_CACHE_DIR_GRCH38",
               "VARIANT_VEP_CADD_SNV_GRCH37")
    assert setting_values(s) == {"VARIANT_VEP_CACHE_DIR_GRCH37": "/reference/old37",
                                 "VARIANT_VEP_CACHE_DIR_GRCH38": "/reference/new38",
                                 "VARIANT_VEP_CADD_SNV_GRCH37": ""}
    assert setting_paths(s) == ("/reference",)


def test_native_defaults_keep_panel_af_and_exact_administrator_bed(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DEFAULT_GENE_PANEL", "ird")
    monkeypatch.setenv("AISCIENTIST_DEFAULT_MAX_POP_AF", "0.005")
    monkeypatch.setenv("AISCIENTIST_DEFAULT_REGIONS_BED", "/refs/panel.bed")
    s = server("VARIANT_DEFAULT_GENE_PANEL", "VARIANT_DEFAULT_MAX_POP_AF", "VARIANT_DEFAULT_REGIONS_BED")
    adapter = AnalysisAdapter(s)
    ctx = SimpleNamespace(decisions={"mcp_variant_assembly": "GRCh37"})
    out = adapter.authoritative({"assembly": "GRCh38", "max_variants": 500}, ctx,
                                 "annotate_variants", None)
    assert out["assembly"] == "GRCh37" and out["max_variants"] == 0
    assert "PRPF31" in out["genes"] and out["max_pop_af"] == 0.005
    assert out["regions_bed"] == "/refs/panel.bed"
    explicit = adapter.authoritative({"genes": [], "max_pop_af": 0, "regions_bed": ""}, ctx,
                                      "annotate_variants", None)
    assert explicit["genes"] == [] and explicit["max_pop_af"] == 0 and explicit["regions_bed"] == ""


def test_admin_spliceai_mount_contains_environment_but_cannot_be_root(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SPLICEAI", "1")
    monkeypatch.setenv("AISCIENTIST_SPLICEAI_BIN", "/lab/envs/splice/bin/openspliceai")
    s = server("VARIANT_SPLICEAI_BIN")
    assert setting_paths(s) == ("/lab/envs/splice",)
    monkeypatch.setenv("VARIANT_SPLICEAI_BIN", "/bin/python")
    with pytest.raises(ValueError, match="filesystem root"):
        setting_paths(s)


def test_existing_scgpt_model_directory_is_preserved(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_SCGPT_MODEL_DIR", "/lab/models/retina")
    s = SimpleNamespace(name="scgpt-annotation", settings_env=("SCGPT_MODEL_DIR",))
    assert setting_values(s)["SCGPT_MODEL_DIR"] == "/lab/models/retina"
    monkeypatch.setenv("SCGPT_MODEL_DIR", "/new/model")
    assert setting_values(s)["SCGPT_MODEL_DIR"] == "/new/model"


def test_fasta_is_independent_of_the_predictor_plugin_switch(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_VEP_PLUGINS", "0")
    monkeypatch.setenv("AISCIENTIST_REF_FASTA", "/refs/genome38.fa")
    monkeypatch.setenv("AISCIENTIST_REF_FASTA_GRCH37", "")
    values = setting_values(server("VARIANT_REF_FASTA_GRCH38", "VARIANT_REF_FASTA_GRCH37"))
    assert values == {"VARIANT_REF_FASTA_GRCH38": "/refs/genome38.fa", "VARIANT_REF_FASTA_GRCH37": ""}


def test_native_generic_override_wins_over_legacy_build_defaults(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_VEP_CACHE_DIR_GRCH37", "/legacy/cache37")
    monkeypatch.setenv("VARIANT_VEP_CACHE_DIR", "/native/cache")
    monkeypatch.setenv("VARIANT_VEP_CACHE_DIR_GRCH38", "/native/build38")
    values = setting_values(server("VARIANT_VEP_CACHE_DIR", "VARIANT_VEP_CACHE_DIR_GRCH37",
                                    "VARIANT_VEP_CACHE_DIR_GRCH38"))
    assert values["VARIANT_VEP_CACHE_DIR_GRCH37"] == "/native/cache"
    assert values["VARIANT_VEP_CACHE_DIR_GRCH38"] == "/native/build38"
    monkeypatch.setenv("VARIANT_VEP_CACHE_DIR", "")
    assert setting_values(server("VARIANT_VEP_CACHE_DIR_GRCH37"))["VARIANT_VEP_CACHE_DIR_GRCH37"] == ""


def test_unused_invalid_defaults_do_not_override_explicit_call(monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DEFAULT_GENE_PANEL", "missing-panel")
    monkeypatch.setenv("VARIANT_DEFAULT_MAX_POP_AF", "not-a-number")
    adapter = AnalysisAdapter(server("VARIANT_DEFAULT_GENE_PANEL", "VARIANT_DEFAULT_MAX_POP_AF"))
    out = adapter.authoritative({"genes": ["RHO"], "max_pop_af": 0}, None, "annotate_variants", None)
    assert out["genes"] == ["RHO"] and out["max_pop_af"] == 0


def test_native_true_switch_keeps_legacy_predictor_paths(monkeypatch):
    monkeypatch.setenv("VARIANT_VEP_PLUGINS", "true")
    monkeypatch.setenv("AISCIENTIST_VEP_CADD_SNV_GRCH37", "/refs/cadd37.gz")
    values = setting_values(server("VARIANT_VEP_CADD_SNV_GRCH37"))
    assert values["VARIANT_VEP_CADD_SNV_GRCH37"] == "/refs/cadd37.gz"
