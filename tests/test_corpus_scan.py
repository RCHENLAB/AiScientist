"""The corpus scan's own GPU job and the scans kept on the Eye Server (gateway/corpus_scan.py).

The job's shell is run for real, with the cluster's commands stubbed, so the two outcomes that
matter are seen happen: the model answers and the scan goes on, or it never comes up and the job
ends with a ``failed`` result naming the model's log. No Slurm, GPU, vLLM or HPC3."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from aiscientist.gateway import corpus_scan
from aiscientist.gateway.executor import ExecResult
















# --- which corpus a scan read ------------------------------------------------------------------

class _Remote:
    def __init__(self, stdout: str):
        self.stdout, self.cmds = stdout, []

    def exec(self, command, timeout=60.0):
        self.cmds.append(command)
        return ExecResult(command=command, exit_status=0, stdout=self.stdout, stderr="")


_SHA = "a" * 64
_STAT = ("mechanism_cache/sentences.jsonl 900 1760000000\nmechanism_cache/chunks.jsonl 800 1760000001\n"
         "mechanism_cache/chunk_vecs.npy 700 1760000002\nhgnc_complete_set.txt 600 1760000003\n")


def test_the_fingerprint_is_the_manifest_hash_and_the_corpus_files():
    remote = _Remote(_SHA + "\n" + _STAT)
    fp = corpus_scan.corpus_fingerprint(remote, "/dfs/retigene/papers", "/dfs/retigene/paperqa_manifest.csv")
    assert fp == {"root": "/dfs/retigene", "manifest_sha256": _SHA,
                  "mechanism_cache/sentences.jsonl": "900 1760000000",
                  "mechanism_cache/chunks.jsonl": "800 1760000001",
                  "mechanism_cache/chunk_vecs.npy": "700 1760000002",
                  "hgnc_complete_set.txt": "600 1760000003"}
    assert remote.cmds[0].startswith("cd /dfs/retigene && sha256sum /dfs/retigene/paperqa_manifest.csv")
    key = corpus_scan.scan_key(fp, "rules1", "m")
    assert key == corpus_scan.scan_key(dict(fp), "rules1", "m") and len(key) == 16
    # a paper added (the manifest changes), new rules, or another model: another scan
    assert corpus_scan.scan_key({**fp, "manifest_sha256": "b" * 64}, "rules1", "m") != key
    assert corpus_scan.scan_key(fp, "rules2", "m") != key
    assert corpus_scan.scan_key(fp, "rules1", "m2") != key


def test_no_fingerprint_without_a_text_cache_or_a_cluster():
    assert corpus_scan.corpus_fingerprint(_Remote(_SHA + "\nhgnc_complete_set.txt 6 7\n"),
                                          "/dfs/retigene/papers", "/m.csv") is None
    assert corpus_scan.corpus_fingerprint(None, "/dfs/retigene/papers") is None
    assert corpus_scan.corpus_fingerprint(_Remote(_STAT), "") is None


# --- scans kept on the Eye Server -------------------------------------------------------------

def _tables(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    with (d / "genes.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["gene", "phenotype", "n_papers", "flag_garbled", "flag_gene_not_in_quote",
                    "pmid_1", "quote_1", "pmid_2", "quote_2", "pmid_3", "quote_3"])
        w.writerow(["PRPF31", "adRP", "3", "False", "True", "111", "PRPF31 haploinsufficiency causes adRP.",
                    "222", "q2", "", ""])
    with (d / "hits.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["pmid", "page", "gene", "quote"])
        w.writerow(["111", "4", "PRPF31", "PRPF31 haploinsufficiency causes adRP."])
    return d


def test_a_scan_is_kept_whole_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path / "data"))
    tables = _tables(tmp_path / "run" / "haploinsufficiency")
    result = {"status": "ok", "n_genes": 1, "files": {"genes_csv": "x"}, "slurm_state": "COMPLETED"}
    kept = corpus_scan.store("k1", result, tables, {"model": "m", "run_id": "r1"})
    assert kept.dir == tmp_path / "data" / "corpus_scans" / "k1"
    got = corpus_scan.load("k1")
    assert got is not None and got.result == {"status": "ok", "n_genes": 1}
    assert got.meta["run_id"] == "r1" and got.meta["key"] == "k1" and got.meta["stored_at"]
    assert (got.tables / "genes.csv").is_file() and (got.tables / "hits.csv").is_file()
    assert not [p for p in kept.dir.parent.iterdir() if p.name.startswith(".tmp-")]
    # a second scan of the same key replaces the first
    corpus_scan.store("k1", {"status": "ok", "n_genes": 2}, tables, {"run_id": "r2"})
    assert corpus_scan.load("k1").result["n_genes"] == 2
    # copied into a run, where the run's own scan would have written them
    corpus_scan.copy_into(got, tmp_path / "run2" / "artifacts")
    assert (tmp_path / "run2" / "artifacts" / "haploinsufficiency" / "genes.csv").is_file()


def test_an_incomplete_folder_is_not_a_kept_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path))
    d = tmp_path / "corpus_scans" / "k2"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "result.json").write_text('{"status": "ok"}')
    assert corpus_scan.load("k2") is None                    # no genes.csv
    assert corpus_scan.load("missing") is None


def test_the_chat_rows_carry_the_quote_its_page_the_pmids_and_the_set_flags(tmp_path):
    rows = corpus_scan.gene_rows(_tables(tmp_path / "t"))
    assert rows == [{"gene": "PRPF31", "phenotype": "adRP", "n_papers": "3",
                     "evidence": "PRPF31 haploinsufficiency causes adRP.", "page": "4",
                     "pmids": ["111", "222"], "flags": ["gene_not_in_quote"]}]
    assert corpus_scan.gene_rows(tmp_path / "nothing") == []


@pytest.mark.parametrize("question, expected", [
    ("What are haploinsufficient genes?", True),
    ("Which genes are haploinsufficient in retinitis pigmentosa?", True),
    ("List all haploinsufficient genes in the RetiGene papers", True),
    ("哪些基因是单倍剂量不足的？", True),
    ("Is PRPF31 haploinsufficient?", False),                 # one gene: the corpus search answers it
    ("What genes cause Stargardt disease?", False),
])
def test_which_chat_messages_ask_for_the_gene_list(question, expected):
    assert corpus_scan.asks_for_gene_list(question) is expected




# --- the job itself ----------------------------------------------------------------------------
