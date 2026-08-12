"""Offline tests for the OFFLINE VCF annotation line (tools/vcf_offline.py).

The whole point of this line is that bcftools + VEP do the heavy lifting as external processes while
Python only stream-parses VEP's JSONL — so here the subprocess runner is INJECTED (a fake that writes
a canned JSONL) and no bcftools/VEP/network is needed. Exercises: the pure command builders, the
ClinVar-from-``--custom`` augmentation (the one thing that differs from the REST path), the streamed
JSONL parse (skip blank/malformed, honour ``max_variants``), and the end-to-end orchestration.
"""

from __future__ import annotations

import json
import types

from bioagent.tools import vcf_offline as vo

# A VEP --json element whose ClinVar significance comes from the colocated variants (REST-style).
_LINE_COLOCATED = {
    "input": "17 7676154 . G A . . .",
    "seq_region_name": "17", "start": 7676154, "allele_string": "G/A",
    "most_severe_consequence": "missense_variant",
    "transcript_consequences": [
        {"gene_symbol": "TP53", "gene_id": "ENSG00000141510", "impact": "MODERATE",
         "consequence_terms": ["missense_variant"], "amino_acids": "R/H",
         "sift_prediction": "deleterious", "polyphen_prediction": "probably_damaging"}],
    "colocated_variants": [
        {"id": "rs28934578", "clin_sig": ["pathogenic"], "frequencies": {"A": {"gnomade": 1e-6}}}],
}

# A VEP --json element with NO colocated clin_sig — ClinVar arrives only via the --custom annotation
# (the usual OFFLINE case, since the cache's colocated variants carry dbSNP, not ClinVar).
_LINE_CUSTOM = {
    "input": "13 32316508 . C T . . .",
    "seq_region_name": "13", "start": 32316508, "allele_string": "C/T",
    "most_severe_consequence": "stop_gained",
    "transcript_consequences": [
        {"gene_symbol": "BRCA2", "impact": "HIGH", "consequence_terms": ["stop_gained"]}],
    "colocated_variants": [{"id": "rs80359550"}],
    "custom_annotations": {"ClinVar": [{"fields": {"CLNSIG": "Pathogenic", "CLNDN": "Breast_cancer"}}]},
}


# --- command builders (pure) -------------------------------------------------


def test_detect_assembly_from_chr1_length():
    # chr1 contig length is unambiguous: 249250621 = GRCh37/hg19, 248956422 = GRCh38. The tag on the
    # line ("assembly=hg19.fasta") is IGNORED in favour of the length — this is the real-VCF header shape.
    grch37 = "##contig=<ID=chr1,length=249250621,assembly=hg19.fasta>\n"
    grch38 = "##contig=<ID=chr1,length=248956422,assembly=GRCh38>\n"
    assert vo.detect_assembly(grch37) == "GRCh37"
    assert vo.detect_assembly(grch38) == "GRCh38"
    # non-chr-prefixed contig id works too
    assert vo.detect_assembly("##contig=<ID=1,length=249250621>\n") == "GRCh37"


def test_detect_assembly_falls_back_to_tags_then_unknown():
    assert vo.detect_assembly("##reference=file:///refs/hg19.fasta\n") == "GRCh37"
    assert vo.detect_assembly("##reference=GRCh38_full_analysis_set.fa\n") == "GRCh38"
    # no contig length, no build tag → unknown ('' keeps the caller's configured default)
    assert vo.detect_assembly("##fileformat=VCFv4.2\n##source=GATK\n") == ""


def test_build_filter_cmd_keeps_pass_and_dot_by_default():
    cmd = vo.build_filter_cmd("in.vcf.gz", "out.vcf.gz")
    assert cmd[:4] == ["bcftools", "view", "-f", "PASS,."]   # '.' kept, else single-sample VCFs vanish
    assert cmd[-4:] == ["-Oz", "-o", "out.vcf.gz", "in.vcf.gz"]


def test_build_filter_cmd_region_and_sample():
    cmd = vo.build_filter_cmd("in.vcf.gz", "out.vcf.gz", pass_only=False,
                              regions_bed="panel.bed", sample="TUMOR")
    assert "-f" not in cmd                       # pass_only off → no filter flag
    assert cmd[cmd.index("-s") + 1] == "TUMOR"
    assert cmd[cmd.index("-R") + 1] == "panel.bed"


def test_build_vep_cmd_offline_flags_fork_and_custom_clinvar():
    cmd = vo.build_vep_cmd("f.vcf.gz", "o.jsonl", "/cache", assembly="GRCh37", fork=12,
                           clinvar_vcf="/c/clinvar.vcf.gz")
    assert "--offline" in cmd and "--cache" in cmd and "--json" in cmd
    assert cmd[cmd.index("--dir_cache") + 1] == "/cache"
    assert cmd[cmd.index("--assembly") + 1] == "GRCh37"
    assert cmd[cmd.index("--fork") + 1] == "12"
    custom = cmd[cmd.index("--custom") + 1]
    assert custom.startswith("file=/c/clinvar.vcf.gz,")
    assert "fields=CLNSIG%CLNDN%CLNREVSTAT" in custom       # incl. the review-status star rating


def test_build_vep_cmd_omits_custom_when_no_clinvar():
    assert "--custom" not in vo.build_vep_cmd("f.vcf.gz", "o.jsonl", "/cache")


# --- predictor plugins / HGVS / norm (all gated, additive) -------------------


def test_vep_plugin_flags_off_by_default(monkeypatch):
    monkeypatch.delenv("BIOAGENT_VEP_PLUGINS", raising=False)
    assert vo.vep_plugin_flags({}) == ()                    # nothing unless explicitly enabled


def test_vep_plugin_flags_adds_only_staged_predictors(tmp_path):
    cadd = tmp_path / "cadd.tsv.gz"; cadd.write_text("x")
    am = tmp_path / "am.tsv.gz"; am.write_text("x")
    env = {"BIOAGENT_VEP_PLUGINS": "1", "BIOAGENT_VEP_CADD_SNV": str(cadd),
           "BIOAGENT_VEP_ALPHAMISSENSE": str(am),
           "BIOAGENT_VEP_REVEL": str(tmp_path / "missing_revel.tsv.gz")}   # REVEL file absent → skipped
    flags = vo.vep_plugin_flags(env)
    joined = " ".join(flags)
    assert f"CADD,snv={cadd}" in joined and f"AlphaMissense,file={am}" in joined
    assert "REVEL" not in joined                            # its data file does not exist → omitted
    assert "--mane_select" in flags


def test_build_vep_cmd_adds_plugins_and_hgvs():
    cmd = vo.build_vep_cmd("f.vcf.gz", "o.jsonl", "/cache",
                           plugins=("--plugin", "CADD,snv=/c/cadd.tsv.gz", "--mane_select"),
                           ref_fasta="/ref/GRCh38.fa", plugins_dir="/c/vep_plugins")
    assert cmd[cmd.index("--fasta") + 1] == "/ref/GRCh38.fa" and "--hgvs" in cmd
    assert cmd[cmd.index("--dir_plugins") + 1] == "/c/vep_plugins"   # load .pm from a bind-mounted dir
    assert "CADD,snv=/c/cadd.tsv.gz" in cmd and "--mane_select" in cmd


def test_vep_plugin_flags_explicit_paths_win_over_env(tmp_path, monkeypatch):
    # The gateway injects paths (they resolve inside vep.sif) — explicit args must work with NO env set.
    monkeypatch.delenv("BIOAGENT_VEP_PLUGINS", raising=False)
    cadd = tmp_path / "cadd.tsv.gz"; cadd.write_text("x")
    flags = vo.vep_plugin_flags(enabled=True, cadd_snv=str(cadd))
    assert f"CADD,snv={cadd}" in " ".join(flags) and "--mane_select" in flags
    assert vo.vep_plugin_flags(enabled=False, cadd_snv=str(cadd)) == ()   # master switch off → nothing


def test_build_norm_cmd_splits_and_left_aligns():
    cmd = vo.build_norm_cmd("in.vcf.gz", "out.vcf.gz", "/ref/GRCh38.fa")
    assert cmd[:3] == ["bcftools", "norm", "-m-any"]
    assert cmd[cmd.index("-f") + 1] == "/ref/GRCh38.fa"
    assert cmd[-4:] == ["-Oz", "-o", "out.vcf.gz", "in.vcf.gz"]


_LINE_PREDICTORS = {
    "input": "1 200 . C T . . .", "seq_region_name": "1", "start": 200, "allele_string": "C/T",
    "most_severe_consequence": "missense_variant",
    "transcript_consequences": [
        {"gene_symbol": "GENEX", "impact": "MODERATE", "consequence_terms": ["missense_variant"],
         "hgvsc": "ENST0:c.200C>T", "hgvsp": "ENSP0:p.Pro67Ser", "mane_select": "NM_1.2",
         "cadd_phred": 28.4, "revel": 0.72, "am_pathogenicity": 0.91}],
    "colocated_variants": [{"id": "rs1"}],
    "custom_annotations": {"ClinVar": [{"fields": {
        "CLNSIG": "Uncertain_significance",
        "CLNREVSTAT": "criteria_provided,_multiple_submitters,_no_conflicts"}}]},
}


def test_parse_offline_line_reads_predictors_and_review():
    row = vo.parse_offline_vep_line(_LINE_PREDICTORS)
    assert row["cadd_phred"] == 28.4 and row["revel"] == 0.72 and row["alphamissense"] == 0.91
    assert row["hgvsc"].endswith("c.200C>T") and row["mane_select"] == "NM_1.2"
    assert "multiple_submitters" in row["clinvar_review_status"]        # ClinVar star rating surfaced


def test_run_offline_normalizes_when_ref_fasta_staged(tmp_path, monkeypatch):
    ds = tmp_path / "s.vcf.gz"; ds.write_bytes(b"\x1f\x8b")
    cache = tmp_path / "cache"; cache.mkdir()
    ref = tmp_path / "ref.fa"; ref.write_text(">1\nACGT\n")
    monkeypatch.setenv("BIOAGENT_REF_FASTA", str(ref))
    monkeypatch.delenv("BIOAGENT_VEP_PLUGINS", raising=False)
    seen: list[list[str]] = []

    def run(argv):
        seen.append(argv)
        if argv[0] == "bcftools":
            open(argv[argv.index("-o") + 1], "w").close()
        elif argv[0] == "vep":
            open(argv[argv.index("--output_file") + 1], "w").write(json.dumps(_LINE_PREDICTORS) + "\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    out = vo.run_offline_annotation(ds, tmp_path / "run", cache_dir=str(cache), run=run)
    assert out["status"] == "ok" and out["normalized"] is True
    assert any(a[0] == "bcftools" and "norm" in a for a in seen)        # the norm stage ran


# --- ClinVar-from-custom + line parse ----------------------------------------


def test_clinvar_from_custom_splits_compound_clnsig():
    item = {"custom_annotations": {"ClinVar": [{"fields": {"CLNSIG": "Pathogenic/Likely_pathogenic|drug_response"}}]}}
    assert vo._clinvar_from_custom(item) == ["Pathogenic", "Likely_pathogenic", "drug_response"]


def test_parse_offline_line_uses_colocated_clinvar_when_present():
    row = vo.parse_offline_vep_line(_LINE_COLOCATED)
    assert row["gene_symbol"] == "TP53"
    assert row["consequence"] == "missense_variant"
    assert row["clinical_significance"] == "pathogenic"   # from colocated clin_sig
    assert row["max_af"] == 1e-6


def test_parse_offline_line_falls_back_to_custom_clinvar():
    row = vo.parse_offline_vep_line(_LINE_CUSTOM)
    assert row["gene_symbol"] == "BRCA2" and row["impact"] == "HIGH"
    # colocated carried no clin_sig, so significance MUST come from the --custom ClinVar annotation
    assert row["clinical_significance"] == "pathogenic"


# --- streamed JSONL parse ----------------------------------------------------


def test_annotate_from_vep_json_skips_blank_and_malformed(tmp_path):
    p = tmp_path / "vep.jsonl"
    p.write_text("\n".join([json.dumps(_LINE_COLOCATED), "", "{not json}",
                            json.dumps(_LINE_CUSTOM)]) + "\n", encoding="utf-8")
    rows = vo.annotate_from_vep_json(p)
    assert [r["gene_symbol"] for r in rows] == ["TP53", "BRCA2"]


def test_annotate_from_vep_json_honours_max_variants(tmp_path):
    p = tmp_path / "vep.jsonl"
    p.write_text("\n".join([json.dumps(_LINE_COLOCATED), json.dumps(_LINE_CUSTOM)]) + "\n", encoding="utf-8")
    assert len(vo.annotate_from_vep_json(p, max_variants=1)) == 1


# --- end-to-end orchestration with an injected runner ------------------------


def _fake_runner(jsonl_lines):
    """A fake subprocess runner: bcftools 'creates' its -o output; vep writes the canned JSONL to
    its --output_file. Returns a returncode-0 CompletedProcess-like object."""
    def run(argv):
        if argv[0] == "bcftools":
            open(argv[argv.index("-o") + 1], "w").close()
        elif argv[0] == "vep":
            out = argv[argv.index("--output_file") + 1]
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(x) for x in jsonl_lines) + "\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_run_offline_annotation_end_to_end(tmp_path):
    ds = tmp_path / "sample.vcf.gz"
    ds.write_bytes(b"\x1f\x8b")                 # just needs to exist; the fake bcftools/vep read it
    cache = tmp_path / "cache"
    cache.mkdir()
    ws = tmp_path / "run"

    out = vo.run_offline_annotation(
        ds, ws, cache_dir=str(cache), assembly="GRCh38", fork=4,
        clinvar_vcf=str(tmp_path / "clinvar.vcf.gz"),
        run=_fake_runner([_LINE_COLOCATED, _LINE_CUSTOM]))

    assert out["status"] == "ok"
    assert out["execution_mode"] == "offline_vep"
    assert out["assembly"] == "GRCh38"
    assert out["n_input_variants"] == 2
    assert out["n_pathogenic"] == 2             # both lines resolve to pathogenic (colocated + custom)
    assert out["raw_data_to_llm"] is False
    # the derived table was written under artifacts/tables/
    table = ws / "artifacts" / "tables" / "variant_annotation.tsv"
    assert table.exists()
    header = table.read_text(encoding="utf-8").splitlines()[0]
    assert "gene_symbol" in header and "clinical_significance" in header


def test_run_offline_annotation_errors_on_missing_cache(tmp_path):
    ds = tmp_path / "s.vcf"
    ds.write_text("##fileformat=VCFv4.2\n", encoding="utf-8")
    out = vo.run_offline_annotation(ds, tmp_path / "run", cache_dir=str(tmp_path / "nope"),
                                    run=_fake_runner([]))
    assert out["status"] == "error" and "cache" in out["error"].lower()


# --- SpliceAI (OpenSpliceAI) splice-disruption scoring -----------------------


def test_spliceai_is_enabled_gating(monkeypatch):
    monkeypatch.delenv("BIOAGENT_SPLICEAI", raising=False)
    assert vo.spliceai_is_enabled({}) is False               # off by default
    assert vo.spliceai_is_enabled({"BIOAGENT_SPLICEAI": "1"}) is True
    assert vo.spliceai_is_enabled(enabled=True) is True       # explicit wins over env
    assert vo.spliceai_is_enabled({"BIOAGENT_SPLICEAI": "1"}, enabled=False) is False


def test_build_spliceai_cmd_shape():
    cmd = vo.build_spliceai_cmd("in.vcf", "out.vcf", bin_path="/env/bin/openspliceai",
                                ref_fasta="/ref/GRCh38.fa", models_dir="/models",
                                annotation="grch38", flank=10000, threads=6)
    assert cmd[0] == "env" and "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1" in cmd and "OMP_NUM_THREADS=6" in cmd
    assert cmd[cmd.index("variant") - 1] == "/env/bin/openspliceai"   # env vars precede the binary
    assert cmd[cmd.index("-R") + 1] == "/ref/GRCh38.fa"
    assert cmd[cmd.index("-m") + 1] == "/models" and cmd[cmd.index("-t") + 1] == "pytorch"
    assert cmd[cmd.index("-A") + 1] == "grch38" and cmd[cmd.index("-f") + 1] == "10000"
    assert cmd[cmd.index("-I") + 1] == "in.vcf" and cmd[cmd.index("-O") + 1] == "out.vcf"
    assert "HOME=" not in " ".join(cmd)                       # no writable-HOME vars unless asked
    cmd_home = vo.build_spliceai_cmd("in.vcf", "out.vcf", bin_path="/b", ref_fasta="/r",
                                     models_dir="/m", home_dir="/run/sahome")
    assert "HOME=/run/sahome" in cmd_home and "XDG_CACHE_HOME=/run/sahome" in cmd_home


def test_parse_spliceai_vcf_keeps_max_delta_and_site(tmp_path):
    out = tmp_path / "sa.vcf"
    out.write_text(
        "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        # donor-loss dominates (0.917); two gene entries → the max is kept
        "1\t930337\t.\tG\tC\t.\t.\tOpenSpliceAI=C|SAMD11|0.000|0.010|0.001|0.917|-46|-1|-17|-1,"
        "C|OTHER|0.200|0.000|0.000|0.100|-1|-1|-1|-1\n"
        "1\t500\t.\tA\tT\t.\t.\t.\n",                          # no SpliceAI field → skipped
        encoding="utf-8")
    scores = vo.parse_spliceai_vcf(out)
    assert scores[("1", "930337", "G", "C")] == {"spliceai_max_ds": 0.917, "spliceai_site": "donor_loss"}
    assert ("1", "500", "A", "T") not in scores


def test_write_and_merge_spliceai_roundtrip(tmp_path):
    rows = [{"input": "1 930337 . G C . . .", "gene_symbol": "SAMD11"},
            {"input": "1 926014 . G C . . .", "gene_symbol": "SAMD11"}]
    sa_in = tmp_path / "in.vcf"
    assert vo.write_spliceai_vcf(rows, sa_in) == 2
    body = sa_in.read_text(encoding="utf-8")
    assert "1\t930337\t.\tG\tC" in body and "1\t926014\t.\tG\tC" in body
    # a ##contig header per chromosome is REQUIRED — OpenSpliceAI's pysam writer rejects records whose
    # contig is undeclared (regression guard for the contig-less-VCF SpliceAI failure).
    assert "##contig=<ID=1>" in body
    scores = {("1", "930337", "G", "C"): {"spliceai_max_ds": 0.917, "spliceai_site": "donor_loss"}}
    assert vo.merge_spliceai(rows, scores) == 1               # only the scored locus is filled
    assert rows[0]["spliceai_max_ds"] == 0.917 and rows[0]["spliceai_site"] == "donor_loss"
    assert "spliceai_max_ds" not in rows[1]                   # unscored row untouched


def _fake_runner_with_spliceai(jsonl_lines, ds_by_pos):
    """Like ``_fake_runner`` but also fakes the ``openspliceai variant`` step: it reads the loci from
    the ``-I`` VCF and writes an ``OpenSpliceAI=`` INFO with the canned donor-loss score for each."""
    def run(argv):
        if argv[0] == "bcftools":
            open(argv[argv.index("-o") + 1], "w").close()
        elif argv[0] == "vep":
            with open(argv[argv.index("--output_file") + 1], "w", encoding="utf-8") as fh:
                fh.write("\n".join(json.dumps(x) for x in jsonl_lines) + "\n")
        elif argv[0] == "env" and "variant" in argv:          # openspliceai
            inp, out = argv[argv.index("-I") + 1], argv[argv.index("-O") + 1]
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
                for line in open(inp, encoding="utf-8"):
                    if line.startswith("#") or not line.strip():
                        continue
                    c = line.rstrip("\n").split("\t")
                    ds = ds_by_pos.get(c[1], 0.0)
                    fh.write(f"{c[0]}\t{c[1]}\t.\t{c[3]}\t{c[4]}\t.\t.\t"
                             f"OpenSpliceAI={c[4]}|G|0.000|0.000|0.000|{ds:.3f}|-1|-1|-1|-1\n")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_run_offline_spliceai_stage_scores_rows(tmp_path):
    ds = tmp_path / "sample.vcf.gz"; ds.write_bytes(b"\x1f\x8b")
    cache = tmp_path / "cache"; cache.mkdir()
    ref = tmp_path / "GRCh38.fa"; ref.write_text(">1\nACGT\n", encoding="utf-8")
    sa_bin = tmp_path / "openspliceai"; sa_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    models = tmp_path / "models"; models.mkdir()

    out = vo.run_offline_annotation(
        ds, tmp_path / "run", cache_dir=str(cache), assembly="GRCh38", fork=4,
        ref_fasta=str(ref), spliceai_enabled=True, spliceai_bin=str(sa_bin),
        spliceai_models=str(models),
        run=_fake_runner_with_spliceai([_LINE_COLOCATED, _LINE_CUSTOM], {"7676154": 0.88}))

    assert out["status"] == "ok"
    assert out["spliceai"] == {"ran": True, "n_input": 2, "n_scored": 2}
    table = (tmp_path / "run" / "artifacts" / "tables" / "variant_annotation.tsv").read_text()
    assert "spliceai_max_ds" in table.splitlines()[0] and "0.88" in table


def test_run_offline_spliceai_skips_over_cap(tmp_path):
    ds = tmp_path / "sample.vcf.gz"; ds.write_bytes(b"\x1f\x8b")
    cache = tmp_path / "cache"; cache.mkdir()
    ref = tmp_path / "GRCh38.fa"; ref.write_text(">1\nACGT\n", encoding="utf-8")
    sa_bin = tmp_path / "openspliceai"; sa_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    models = tmp_path / "models"; models.mkdir()

    out = vo.run_offline_annotation(
        ds, tmp_path / "run", cache_dir=str(cache), ref_fasta=str(ref),
        spliceai_enabled=True, spliceai_bin=str(sa_bin), spliceai_models=str(models),
        spliceai_max_variants=1,                              # 2 variants > cap ⇒ skipped, not run
        run=_fake_runner_with_spliceai([_LINE_COLOCATED, _LINE_CUSTOM], {}))

    assert out["spliceai"]["ran"] is False and "cap" in out["spliceai"]["note"]
