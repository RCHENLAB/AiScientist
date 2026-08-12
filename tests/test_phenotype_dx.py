"""Tests for the phenotype-driven differential-diagnosis scaffold (tools/phenotype_dx.py).

Exercises the parts that do NOT depend on a live LIRICAL / PaperQA2: the LIRICAL TSV parser, the
two-track reconciliation (attach vs rescue, currencies never blended), and the PaperQA2 placeholder.
"""
from __future__ import annotations

from pathlib import Path

from bioagent.tools.phenotype_dx import (
    DiseaseCandidate,
    adjudicate,
    apply_entrez_symbols,
    build_lirical_cmd,
    build_phenopacket,
    diagnose,
    entrez_to_symbol_map,
    make_diagnose_disease_tool,
    make_phenotype_differential_tool,
    normalize_assembly,
    paperqa2_evidence,
    parse_lirical_tsv,
    plan_literature_queries,
    reconcile,
    run_lirical,
    strip_chr_prefix,
    tier_at_least,
    vcf_uses_chr_prefix,
)


def _fake_lirical_exec(tsv_body: str, *, captured: "dict | None" = None):
    """A stand-in for the in-container LIRICAL: it writes ``<-o outdir>/<-x prefix>.tsv`` (the file
    run_lirical then reads back) and returns a rc=0 process. Optionally captures the argv it was given."""
    def _exec(argv):
        if captured is not None:
            captured["argv"] = argv
        out_dir = Path(argv[argv.index("-o") + 1])
        prefix = argv[argv.index("-x") + 1]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{prefix}.tsv").write_text(tsv_body, encoding="utf-8")

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""
        return _P()
    return _exec


def test_tier_ordering():
    assert tier_at_least("DEFINITIVE", "STRONG")
    assert tier_at_least("STRONG", "STRONG")
    assert not tier_at_least("MODERATE", "STRONG")     # a positive threshold isn't met by weaker grades
    assert not tier_at_least("DISPUTED", "MODERATE")   # contradictory evidence never passes
    assert not tier_at_least("", "LIMITED")            # unknown -> NONE


def test_parse_lirical_tsv_maps_by_name_and_percent():
    tsv = (
        "! LIRICAL v2.0.0 metadata line ignored\n"
        "rank\tdiseaseName\tdiseaseCurie\tpretestprob\tposttestprob\tcompositeLR\tgene\n"
        "1\tRetinitis pigmentosa 12\tOMIM:600105\t0.001\t96.7%\t1200.5\tCRB1\n"
        "2\tLeber congenital amaurosis 1\tOMIM:204000\t0.001\t0.150\t3.2\tGUCY2D\n"
    )
    cands = parse_lirical_tsv(tsv)
    assert [c.gene for c in cands] == ["CRB1", "GUCY2D"]
    assert abs(cands[0].posttest_prob - 0.967) < 1e-9      # percentage parsed to fraction
    assert cands[1].posttest_prob == 0.15
    assert cands[0].disease_id == "OMIM:600105" and cands[0].sources == {"lirical"}


def test_parse_lirical_tsv_genotype_aware_real_header():
    # The VERIFIED LIRICAL v2.4.1 genotype-aware TSV header (from HPC3): entrezGeneId + variants, and NO
    # gene-symbol column. The parser must capture entrez + the variant string, not silently drop them.
    tsv = (
        "rank\tdiseaseName\tdiseaseCurie\tpretestprob\tposttestprob\tcompositeLR\tentrezGeneId\tvariants\n"
        "1\tRetinitis pigmentosa 19\tOMIM:601718\t1/8621\t96.22%\t5.342\tNCBIGene:24\t"
        "1:94473807C>T NM_000350.2:c.5882G>A:p.(G1961E) pathogenicity:1.0 [0/1]\n"
    )
    c = parse_lirical_tsv(tsv)[0]
    assert c.disease_id == "OMIM:601718" and abs(c.posttest_prob - 0.9622) < 1e-9
    assert c.entrez_gene_id == "NCBIGene:24" and c.gene == ""      # LIRICAL gives entrez, not a symbol
    assert "p.(G1961E)" in c.variants


def test_reconcile_attaches_support_without_moving_probability():
    lirical = [
        DiseaseCandidate("RP12", "OMIM:600105", gene="CRB1", posttest_prob=0.70, sources={"lirical"}),
        DiseaseCandidate("LCA1", "OMIM:204000", gene="GUCY2D", posttest_prob=0.20, sources={"lirical"}),
    ]
    literature = [
        {"status": "ok", "gene": "CRB1", "disease": "RP12", "association": True,
         "clingen_tier": "STRONG", "evidence": [{"pmid": "111"}, {"pmid": "222"}]},
    ]
    res = reconcile(lirical, literature)
    top = res.ranked[0]
    assert top.gene == "CRB1" and top.posttest_prob == 0.70   # probability UNCHANGED by literature
    assert top.evidence_tier == "STRONG" and top.evidence_pmids == ["111", "222"]
    assert top.sources == {"lirical", "literature"}
    assert res.literature_rescued == []


def test_reconcile_rescues_strong_lirical_miss_but_not_weak():
    lirical = [DiseaseCandidate("RP12", "OMIM:600105", gene="CRB1", posttest_prob=0.70, sources={"lirical"})]
    literature = [
        # a gene LIRICAL never surfaced, strongly supported in the literature -> rescued for review
        {"status": "ok", "gene": "NEWGENE1", "disease": "novel RP", "association": True,
         "clingen_tier": "STRONG", "evidence": [{"pmid": "999"}]},
        # only limited evidence -> must NOT be rescued
        {"status": "ok", "gene": "WEAKGENE", "disease": "maybe RP", "association": True,
         "clingen_tier": "LIMITED", "evidence": [{"pmid": "333"}]},
    ]
    res = reconcile(lirical, literature, rescue_threshold="STRONG")
    rescued_genes = {c.gene for c in res.literature_rescued}
    assert rescued_genes == {"NEWGENE1"}
    r = res.literature_rescued[0]
    assert r.posttest_prob is None                       # no fabricated probability
    assert r.flags == ["lirical_missed_literature_supported"] and r.sources == {"literature"}


def test_paperqa2_placeholder_and_injected_runner():
    off = paperqa2_evidence("CRB1", "RP12")
    assert off["status"] == "not_enabled" and off["association"] is False and off["clingen_tier"] == "NONE"

    def fake_runner(*, gene, disease, hpo_terms):
        return {"association": True, "clingen_tier": "moderate",
                "evidence": [{"pmid": "42", "quote": "...", "study_type": "case_report"}]}

    on = paperqa2_evidence("CRB1", "RP12", ["HP:0000512"], runner=fake_runner)
    assert on["status"] == "ok" and on["clingen_tier"] == "MODERATE"   # normalised to upper


def test_run_lirical_is_gated_until_staged():
    # No data_dir / no exec_fn -> LIRICAL is not staged -> the run continues without a differential.
    assert run_lirical(hpo_terms=["HP:0000512"], vcf_path="x.vcf")["status"] == "not_installed"


def test_run_lirical_requires_at_least_one_hpo_term():
    assert run_lirical(hpo_terms=[], data_dir="/d", exec_fn=lambda argv: None)["status"] == "error"


# --- LIRICAL input + CLI (pure builders) ---------------------------------------------------------


def test_normalize_assembly_maps_grch_and_defaults_hg38():
    assert normalize_assembly("GRCh37") == "hg19"
    assert normalize_assembly("hg19") == "hg19"
    assert normalize_assembly("GRCh38") == "hg38"
    assert normalize_assembly("") == "hg38"        # unknown -> LIRICAL's default


def test_build_phenopacket_observed_and_excluded_terms():
    pp = build_phenopacket(["HP:0000512", "HP:0000662"], excluded=["HP:0000556"], sample_id="s1")
    assert pp["subject"]["id"] == "s1"
    assert pp["metaData"]["phenopacketSchemaVersion"] == "2.0"
    observed = [f["type"]["id"] for f in pp["phenotypicFeatures"] if not f.get("excluded")]
    excluded = [f for f in pp["phenotypicFeatures"] if f.get("excluded")]
    assert observed == ["HP:0000512", "HP:0000662"]
    assert excluded and excluded[0]["type"]["id"] == "HP:0000556" and excluded[0]["excluded"] is True


def test_build_lirical_cmd_phenotype_only_omits_vcf():
    cmd = build_lirical_cmd(observed=["HP:0000512", "HP:0000662"], negated=["HP:0000556"],
                            data_dir="/d", output_dir="/o", prefix="x")
    assert "prioritize" in cmd and cmd[cmd.index("-p") + 1] == "HP:0000512,HP:0000662"   # comma-joined
    assert cmd[cmd.index("-n") + 1] == "HP:0000556"                                       # negated terms
    assert cmd[cmd.index("-o") + 1] == "/o" and cmd[cmd.index("-x") + 1] == "x"
    assert cmd[cmd.index("-f") + 1] == "tsv"
    assert "--vcf" not in cmd and "-ed19" not in cmd and "-ed38" not in cmd   # no genotype scoring


def test_build_lirical_cmd_genotype_aware_adds_vcf_and_matching_exomiser():
    cmd = build_lirical_cmd(observed=["HP:0000512"], data_dir="/d", output_dir="/o", assembly="GRCh38",
                            vcf_path="/v.vcf.gz", exomiser_dir="/e/2406_hg38", sample_id="s1")
    assert cmd[cmd.index("--vcf") + 1] == "/v.vcf.gz"
    assert cmd[cmd.index("--assembly") + 1] == "hg38"
    assert cmd[cmd.index("-ed38") + 1] == "/e/2406_hg38" and "-ed19" not in cmd
    assert cmd[cmd.index("--sample-id") + 1] == "s1"


# --- run_lirical orchestration (injected exec) ---------------------------------------------------


def test_run_lirical_phenotype_only_parses_ranked_differential(tmp_path):
    tsv = (
        "! LIRICAL v2 metadata\n"
        "rank\tdiseaseName\tdiseaseCurie\tposttestprob\tgene\n"
        "1\tRetinitis pigmentosa 12\tOMIM:600105\t0.71\tCRB1\n"
        "2\tLeber congenital amaurosis 1\tOMIM:204000\t0.12\tGUCY2D\n"
    )
    res = run_lirical(hpo_terms=["HP:0000512", "HP:0000662"], data_dir=str(tmp_path / "data"),
                      workspace=str(tmp_path), exec_fn=_fake_lirical_exec(tsv))
    assert res["status"] == "ok" and res["mode"] == "phenotype_only"
    assert res["n_candidates"] == 2 and res["candidates"][0]["gene"] == "CRB1"
    assert (tmp_path / "artifacts" / "phenotype" / "phenopacket.json").exists()   # provenance record
    assert res["note"]                                             # phenotype-only advisory present


def test_run_lirical_genotype_aware_when_vcf_and_exomiser_present(tmp_path):
    captured: dict = {}
    exec_fn = _fake_lirical_exec("rank\tdiseaseName\tgene\n1\tRP\tCRB1\n", captured=captured)
    res = run_lirical(hpo_terms=["HP:0000512"], vcf_path="/data/case.vcf.gz",
                      data_dir=str(tmp_path / "data"), workspace=str(tmp_path),
                      assembly="GRCh37", exomiser_hg19="/ref/2406_hg19", exec_fn=exec_fn)
    assert res["status"] == "ok" and res["mode"] == "genotype_aware" and res["assembly"] == "hg19"
    argv = captured["argv"]
    assert "--vcf" in argv and "/data/case.vcf.gz" in argv
    assert argv[argv.index("-ed19") + 1] == "/ref/2406_hg19"
    assert (tmp_path / "artifacts" / "phenotype" / "phenopacket.json").exists()   # provenance record


def test_run_lirical_ignores_vcf_without_exomiser_db(tmp_path):
    # A VCF but no Exomiser DB -> phenotype-only (LIRICAL requires the DB to score variants), no --vcf.
    captured: dict = {}
    exec_fn = _fake_lirical_exec("rank\tdiseaseName\tgene\n1\tRP\tCRB1\n", captured=captured)
    res = run_lirical(hpo_terms=["HP:0000512"], vcf_path="/data/case.vcf.gz",
                      data_dir=str(tmp_path / "data"), workspace=str(tmp_path), exec_fn=exec_fn)
    assert res["mode"] == "phenotype_only" and "--vcf" not in captured["argv"]


# --- chr-prefix detection + stripping (Exomiser wants bare contig names) --------------------------


def test_vcf_uses_chr_prefix_detects_header_records_and_missing(tmp_path):
    chrv = tmp_path / "chr.vcf"
    chrv.write_text("##fileformat=VCFv4.2\n##contig=<ID=chr1,length=249250621>\n"
                    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t100\t.\tA\tG\t.\tPASS\t.\n")
    assert vcf_uses_chr_prefix(str(chrv)) is True
    bare = tmp_path / "bare.vcf"
    bare.write_text("##fileformat=VCFv4.2\n##contig=<ID=1,length=249250621>\n"
                    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t100\t.\tA\tG\t.\tPASS\t.\n")
    assert vcf_uses_chr_prefix(str(bare)) is False
    # no ##contig header -> decide from the first record
    norec = tmp_path / "nohdr.vcf"
    norec.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
                     "chrX\t100\t.\tA\tG\t.\tPASS\t.\n")
    assert vcf_uses_chr_prefix(str(norec)) is True
    assert vcf_uses_chr_prefix("/no/such/file.vcf") is False   # unreadable -> treat as bare


def test_strip_chr_prefix_rewrites_header_and_records_keeps_genotypes(tmp_path):
    src = tmp_path / "chr.vcf"
    src.write_text("##fileformat=VCFv4.2\n##contig=<ID=chr1,length=249250621>\n"
                   "##contig=<ID=chrM,length=16571>\n"
                   "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\ts1\n"
                   "chr1\t100\t.\tA\tG\t.\tPASS\tAC=2\tGT\t0/1\n"
                   "chrM\t150\t.\tT\tC\t.\tPASS\t.\tGT\t1/1\n")
    out = tmp_path / "nochr.vcf"
    n = strip_chr_prefix(str(src), str(out))
    text = out.read_text()
    assert n == 2 and "chr1" not in text and "chrM" not in text
    assert "##contig=<ID=1,length=249250621>" in text and "##contig=<ID=M,length=16571>" in text
    assert "\n1\t100\t.\tA\tG\t.\tPASS\tAC=2\tGT\t0/1\n" in text   # record de-chr'd, genotype untouched


def test_run_lirical_strips_chr_for_genotype_aware(tmp_path):
    (tmp_path / "data").mkdir()
    vcf = tmp_path / "chr.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n##contig=<ID=chr1,length=249250621>\n"
                   "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\ts1\n"
                   "chr1\t94473807\t.\tC\tT\t.\tPASS\t.\tGT\t0/1\n")
    captured: dict = {}
    exec_fn = _fake_lirical_exec("rank\tdiseaseName\n1\tRP19\n", captured=captured)
    res = run_lirical(hpo_terms=["HP:0000512"], vcf_path=str(vcf), data_dir=str(tmp_path / "data"),
                      workspace=str(tmp_path), assembly="hg19", exomiser_hg19="/ref/2406_hg19",
                      exec_fn=exec_fn)
    assert res["chr_stripped"] is True and res["mode"] == "genotype_aware"
    argv = captured["argv"]
    assert argv[argv.index("--vcf") + 1].endswith("nochr.vcf")     # LIRICAL got the de-chr'd copy
    assert (tmp_path / "work" / "nochr.vcf").exists()


def test_run_lirical_leaves_bare_vcf_unchanged(tmp_path):
    (tmp_path / "data").mkdir()
    vcf = tmp_path / "bare.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n##contig=<ID=1,length=249250621>\n"
                   "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\ts1\n"
                   "1\t94473807\t.\tC\tT\t.\tPASS\t.\tGT\t0/1\n")
    captured: dict = {}
    res = run_lirical(hpo_terms=["HP:0000512"], vcf_path=str(vcf), data_dir=str(tmp_path / "data"),
                      workspace=str(tmp_path), assembly="hg19", exomiser_hg19="/ref/2406_hg19",
                      exec_fn=_fake_lirical_exec("rank\tdiseaseName\n1\tRP\n", captured=captured))
    assert res["chr_stripped"] is False
    assert captured["argv"][captured["argv"].index("--vcf") + 1] == str(vcf)   # original used as-is


# --- Entrez → symbol mapping (LIRICAL emits Entrez ids; reconcile keys on symbols) ----------------


def test_entrez_to_symbol_map_and_apply(tmp_path):
    hgnc = tmp_path / "hgnc_complete_set.txt"
    hgnc.write_text("hgnc_id\tsymbol\tname\tentrez_id\n"
                    "HGNC:34\tABCA4\tATP binding cassette 4\t24\n"
                    "HGNC:2551\tCRB1\tcrumbs 1\t23418\n", encoding="utf-8")
    m = entrez_to_symbol_map(str(hgnc))
    assert m["24"] == "ABCA4" and m["23418"] == "CRB1"
    cands = [DiseaseCandidate("RP19", "OMIM:601718", entrez_gene_id="NCBIGene:24"),
             DiseaseCandidate("known", "OMIM:x", gene="ALREADY", entrez_gene_id="NCBIGene:999")]
    n = apply_entrez_symbols(cands, str(hgnc))
    assert n == 1 and cands[0].gene == "ABCA4"        # entrez resolved to a symbol
    assert cands[1].gene == "ALREADY"                 # an existing symbol is left untouched


def test_entrez_map_missing_file_is_noop():
    assert entrez_to_symbol_map("/no/such/hgnc.txt") == {}
    cands = [DiseaseCandidate("x", entrez_gene_id="NCBIGene:24")]
    assert apply_entrez_symbols(cands, "/no/such/hgnc.txt") == 0 and cands[0].gene == ""


def test_run_lirical_fills_gene_symbol_from_hgnc(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "hgnc_complete_set.txt").write_text(
        "hgnc_id\tsymbol\tentrez_id\nHGNC:34\tABCA4\t24\n", encoding="utf-8")
    tsv = ("rank\tdiseaseName\tdiseaseCurie\tposttestprob\tentrezGeneId\n"
           "1\tRP19\tOMIM:601718\t96.22%\tNCBIGene:24\n")
    res = run_lirical(hpo_terms=["HP:0000512"], data_dir=str(tmp_path / "data"),
                      workspace=str(tmp_path), exec_fn=_fake_lirical_exec(tsv))
    assert res["candidates"][0]["gene"] == "ABCA4"    # entrez → symbol filled end-to-end


# --- the Scientist tool factory ------------------------------------------------------------------


def test_make_phenotype_tool_shape_and_gated_default():
    tool = make_phenotype_differential_tool()
    assert tool.name == "run_lirical" and tool.reads_private_data is True
    assert "hpo_terms" in tool.parameters["properties"]
    # no gateway executor / no data_dir → in-process default reports not_installed (LIRICAL runs on HPC3)
    assert tool.executor({"hpo_terms": ["HP:0000510"]}, None)["status"] == "not_installed"
    # missing HPO terms → a clear error, not a crash
    assert tool.executor({}, None)["status"] == "error"


# --- adjudication: ONE ranked differential, literature weighted above LIRICAL ----------------------
#
# reconcile() above keeps the tracks apart for provenance; these cover the DECISION layer, where they
# are deliberately weighed against each other. The invariant that must survive every case below:
# ``posttest_prob`` is never rewritten and no probability is invented — only the RANKING moves.


def _lit(gene, tier, *, disease="", status="graded", association=None, pmids=("1",)):
    return {"status": "ok", "gene": gene, "disease": disease or f"{gene}-disease",
            "association": bool(tier not in ("DISPUTED", "REFUTED", "NONE")
                                if association is None else association),
            "clingen_tier": tier, "evidence_status": status,
            "evidence": [{"pmid": p} for p in pmids]}


def test_literature_refutation_outranks_a_confident_lirical_call():
    lirical = [DiseaseCandidate("RP19", "OMIM:601718", gene="ABCA4", posttest_prob=0.96,
                                sources={"lirical"}),
               DiseaseCandidate("LCA1", "OMIM:204000", gene="GUCY2D", posttest_prob=0.20,
                                sources={"lirical"})]
    diff = adjudicate(reconcile(lirical, [_lit("ABCA4", "REFUTED", status="contradicted")]))
    by_gene = {d["gene"]: d for d in diff}
    # the refuted 96% candidate is pushed BELOW an uncontradicted 20% one — the literature wins
    assert by_gene["ABCA4"]["rank"] > by_gene["GUCY2D"]["rank"]
    assert by_gene["ABCA4"]["agreement"] == "conflict" and by_gene["ABCA4"]["final_score"] < 0
    assert by_gene["ABCA4"]["posttest_prob"] == 0.96          # LIRICAL's number is NOT rewritten


def test_literature_only_candidate_enters_the_same_ranked_list():
    # the gap-fill: LIRICAL never scored NEWGENE, so without this it is simply absent from the answer
    lirical = [DiseaseCandidate("RP12", "OMIM:600105", gene="CRB1", posttest_prob=0.55,
                                sources={"lirical"})]
    diff = adjudicate(reconcile(lirical, [_lit("NEWGENE", "STRONG", disease="novel IRD")]))
    assert diff[0]["gene"] == "NEWGENE" and diff[0]["agreement"] == "literature_only"
    assert diff[0]["posttest_prob"] is None                   # no fabricated probability
    assert diff[1]["gene"] == "CRB1"


def test_concordant_evidence_lifts_without_touching_the_probability():
    lirical = [DiseaseCandidate("RP12", "OMIM:600105", gene="CRB1", posttest_prob=0.70,
                                sources={"lirical"})]
    plain = adjudicate(reconcile(list(lirical), []))[0]
    lifted = adjudicate(reconcile(
        [DiseaseCandidate("RP12", "OMIM:600105", gene="CRB1", posttest_prob=0.70,
                          sources={"lirical"})],
        [_lit("CRB1", "DEFINITIVE", pmids=("1", "2"))]))[0]
    assert plain["agreement"] == "lirical_only" and lifted["agreement"] == "concordant"
    assert lifted["final_score"] > plain["final_score"]
    assert lifted["posttest_prob"] == plain["posttest_prob"] == 0.70
    assert lifted["evidence_pmids"] == ["1", "2"]


def test_silent_corpus_does_not_penalise_but_an_unsupportive_one_does():
    def score(records):
        c = DiseaseCandidate("RP12", "OMIM:600105", gene="CRB1", posttest_prob=0.80,
                             sources={"lirical"})
        return adjudicate(reconcile([c], records))[0]

    # nothing retrieved: absence of DATA. LIRICAL's ranking must stand unpenalised.
    silent = score([])
    assert silent["agreement"] == "lirical_only" and silent["final_score"] == 0.80
    # papers came back and none supported the link: a real, but weak, negative signal
    unsupported = score([_lit("CRB1", "NONE", status="unsupported", association=False)])
    assert unsupported["agreement"] == "unsupported"
    assert 0 < unsupported["final_score"] < 0.80


def test_a_runner_written_to_the_plain_contract_still_behaves_as_before():
    # evidence_status is an ADDITION; a runner that omits it must not be read as a refutation
    legacy = {"status": "ok", "gene": "CRB1", "disease": "RP12", "association": True,
              "clingen_tier": "MODERATE", "evidence": [{"pmid": "7"}]}
    d = adjudicate(reconcile([DiseaseCandidate("RP12", gene="CRB1", posttest_prob=0.60,
                                               sources={"lirical"})], [legacy]))[0]
    assert d["agreement"] == "concordant" and d["evidence_tier"] == "MODERATE"


# --- diagnose(): both tracks, end to end -----------------------------------------------------------


def _fake_lit_tool(answers):
    """A stand-in ``deep_literature`` executor: picks its canned answer by the gene named in the query."""
    def _fn(args, ctx):
        for gene, resp in answers.items():
            if gene in args["question"]:
                return resp
        return {"status": "ok", "answer": "The provided context does not support an association.",
                "contexts": [{"citation": "Other 2020 PMID: 33333333", "summary": "unrelated"}]}
    return _fn


def test_diagnose_answers_from_the_literature_when_lirical_cannot_run():
    """The case this whole line exists for: LIRICAL is not staged, so run_lirical alone returns
    nothing usable. diagnose() must still produce a differential."""
    from bioagent.tools.phenotype_evidence import make_deep_literature_runner

    runner = make_deep_literature_runner(_fake_lit_tool({"CRB1": {
        "status": "ok",
        "answer": "Reported in unrelated families with knockout mouse support. STRONG.",
        "contexts": [{"citation": "A 1999 PMID: 10508521", "summary": "12 unrelated families"},
                     {"citation": "B 2003 PMID: 12915482", "summary": "knockout mouse model"}]}}), None)

    res = diagnose(hpo_terms=["HP:0000510"], literature_runner=runner,
                   candidate_genes=["CRB1", "TTN"])
    assert res["status"] == "ok" and res["mode"] == "literature_only"
    assert res["differential"] and res["top"]["gene"] == "CRB1"
    assert res["top"]["evidence_tier"] == "STRONG"
    assert any("LIRICAL unavailable" in n for n in res["notes"])
    # TTN came back unsupported -> it must NOT be promoted into the differential
    assert "TTN" not in {d["gene"] for d in res["differential"]}


def test_diagnose_without_a_literature_runner_reports_lirical_unchanged():
    lirical = {"status": "ok", "candidates": [
        DiseaseCandidate("RP12", "OMIM:600105", gene="CRB1", posttest_prob=0.70,
                         sources={"lirical"}).as_dict()]}
    res = diagnose(hpo_terms=["HP:0000510"], lirical_result=lirical)
    assert res["mode"] == "lirical_only"
    assert res["differential"][0]["final_score"] == 0.70     # untouched
    assert any("no literature runner" in n for n in res["notes"])


def test_diagnose_keeps_the_untouched_two_track_view_as_provenance():
    lirical = {"status": "ok", "candidates": [
        DiseaseCandidate("RP19", gene="ABCA4", posttest_prob=0.96, sources={"lirical"}).as_dict()]}
    res = diagnose(hpo_terms=["HP:0000510"], lirical_result=lirical,
                   literature_runner=lambda **kw: {"association": False, "clingen_tier": "REFUTED",
                                                   "evidence_status": "contradicted",
                                                   "evidence": [{"pmid": "5"}]})
    assert res["differential"][0]["agreement"] == "conflict"
    # reconcile's view is preserved verbatim next to the decision, so the demotion is auditable
    assert res["reconciled"]["ranked"][0]["posttest_prob"] == 0.96
    assert any("contradicts LIRICAL" in n for n in res["notes"])


def test_plan_literature_queries_covers_lirical_then_the_gap_and_caps():
    cands = [DiseaseCandidate("RP12", gene="CRB1", posttest_prob=0.7),
             DiseaseCandidate("RP19", gene="ABCA4", posttest_prob=0.2)]
    pairs = plan_literature_queries(cands, ["ABCA4", "NEWGENE"], max_queries=6)
    assert pairs == [("CRB1", "RP12"), ("ABCA4", "RP19"), ("NEWGENE", "")]   # no duplicate ABCA4
    assert len(plan_literature_queries(cands, ["NEWGENE"], max_queries=2)) == 2


def test_make_diagnose_tool_shape_and_degraded_default():
    tool = make_diagnose_disease_tool()
    assert tool.name == "diagnose_disease" and tool.reads_private_data is True
    assert "candidate_genes" in tool.parameters["properties"]
    out = tool.executor({"hpo_terms": ["HP:0000510"]}, None)
    # no LIRICAL, no literature executor -> a structured "nothing staged", never a crash and never
    # a bare "error" (nothing failed — the deployment is simply absent)
    assert out["status"] == "not_installed"
    assert out["mode"] == "lirical_only" and out["differential"] == []
