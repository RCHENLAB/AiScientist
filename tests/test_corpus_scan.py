"""The corpus scan's own GPU job and the scans kept on the Eye Server (gateway/corpus_scan.py).

The job's shell is run for real, with the cluster's commands stubbed, so the two outcomes that
matter are seen happen: the model answers and the scan goes on, or it never comes up and the job
ends with a ``failed`` result naming the model's log. No Slurm, GPU, vLLM or HPC3."""
from __future__ import annotations

import base64
import csv
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from aiscientist.gateway import corpus_scan
from aiscientist.gateway.executor import ExecResult
from aiscientist.gateway.settings import HPCSettings
from aiscientist.gateway.slurm_analysis import SlurmAnalysisExecutor


def _body(shell: str) -> str:
    m = re.search(r"printf %s '([A-Za-z0-9+/=]+)'", shell)
    assert m, shell
    return base64.b64decode(m.group(1)).decode("utf-8")


def test_the_model_is_served_on_loopback_with_a_short_window():
    st = HPCSettings()
    shell, env = corpus_scan.vllm_prelude(st, name="job_1", scratch="/s", res_f="/s/job_1.result.json")
    body = _body(shell)
    assert f"vllm serve {st.vllm_model} " in body and f"--served-model-name {st.vllm_model}" in body
    assert "--host 127.0.0.1" in body and "0.0.0.0" not in body
    assert '--port "$(cat /s/job_1.port)"' in body
    assert f"--max-model-len {st.corpus_scan_max_model_len}" in body
    assert "--nv" in body and st.vllm_image in body and "HF_HUB_OFFLINE=1" in body
    assert "--enable-auto-tool-choice" not in body          # the judge calls no tools
    # what lets the 27B INT4 fit a 24 GB A30: no image/video encoders, at most the tool's 16 requests
    assert """--limit-mm-per-prompt '{"image":0,"video":0}' --max-num-seqs 16""" in body
    assert (st.corpus_scan_gres, st.corpus_scan_constraint) == ("gpu:1", "nvme")
    # the scan reaches it through the env the tool reads when its args carry no endpoint
    assert env == "export AISCIENTIST_PAPERQA_LLM_BASE_URL=http://127.0.0.1:$(cat /s/job_1.port)/v1; "
    assert subprocess.run(["bash", "-n"], input=shell, text=True).returncode == 0


def test_a_configured_model_and_extra_flags_win():
    st = HPCSettings(corpus_scan_model="org/Other-7B", corpus_scan_vllm_args="--enforce-eager")
    body = _body(corpus_scan.vllm_prelude(st, name="j", scratch="/s", res_f="/s/r")[0])
    assert "vllm serve org/Other-7B " in body and "--enforce-eager" in body
    assert corpus_scan.scan_model(st) == "org/Other-7B"
    assert corpus_scan.scan_model(HPCSettings()) == HPCSettings().vllm_model


def _stub(bin_dir: Path, name: str, script: str) -> None:
    p = bin_dir / name
    p.write_text("#!/bin/bash\n" + script + "\n")
    p.chmod(0o755)


def _run_prelude(tmp_path: Path, curl_exit: int) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the job's shell under ``set -euo pipefail`` (as the sbatch script does), with ``setsid``,
    ``sg`` and ``curl`` stubbed and no apptainer on PATH, then ``echo SCAN`` where the scan's
    container would start."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "setsid", 'exec "$@"')
    _stub(bin_dir, "sg", 'exec bash -c "$3"')                  # sg GROUP -c CMD
    _stub(bin_dir, "curl", f"exit {curl_exit}")
    _stub(bin_dir, "module", "true")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    res = scratch / "job.result.json"
    st = HPCSettings(corpus_scan_ready_timeout_s=60)
    shell, _ = corpus_scan.vllm_prelude(st, name="job", scratch=str(scratch), res_f=str(res))
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    proc = subprocess.run(["bash", "-c", "set -euo pipefail\n" + shell + "\necho SCAN\n"],
                          text=True, capture_output=True, env=env, timeout=60)
    return proc, res


def test_the_scan_starts_once_the_model_answers(tmp_path):
    proc, res = _run_prelude(tmp_path, curl_exit=0)
    assert proc.returncode == 0, proc.stderr
    assert "Model ready" in proc.stdout and "SCAN" in proc.stdout
    assert not res.exists()
    assert (tmp_path / "scratch" / "job.port").read_text().strip().isdigit()


def test_a_model_that_never_comes_up_ends_the_job_with_its_reason(tmp_path):
    proc, res = _run_prelude(tmp_path, curl_exit=7)          # nothing listens; the server exits at once
    assert proc.returncode == 0, proc.stderr
    assert "SCAN" not in proc.stdout                          # the scan's container never starts
    line = res.read_text().strip()
    assert line.startswith("AISCIENTIST_RESULT_JSON ")
    out = json.loads(line[len("AISCIENTIST_RESULT_JSON "):])
    assert out["status"] == "failed" and out["model_unreachable"] is False
    assert "exited before it answered" in out["error"]
    assert out["vllm_log"].endswith("job.vllm.log") and Path(out["vllm_log"]).exists()


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


def test_the_rules_digest_follows_the_rules(monkeypatch):
    from aiscientist.tools import api
    from aiscientist.tools.extract_haploinsufficient_genes import tool as hi

    first = api.corpus_scan_rules_digest()
    assert first == api.corpus_scan_rules_digest() and len(first) == 16
    monkeypatch.setattr(hi, "JUDGE_SYSTEM", hi.JUDGE_SYSTEM + " Be stricter.")
    assert api.corpus_scan_rules_digest() != first
    monkeypatch.undo()
    monkeypatch.setattr(hi, "RULES_REVISION", hi.RULES_REVISION + 1)
    assert api.corpus_scan_rules_digest() != first


# --- the job itself ----------------------------------------------------------------------------

class _Cluster:
    """sbatch accepts, the job is gone at once (sacct COMPLETED), the result file holds the scan's
    marked result line. Records every command."""

    def __init__(self, result: dict):
        self.cmds: list[str] = []
        self.line = "AISCIENTIST_RESULT_JSON " + json.dumps(result) + "\n"
        self.host, self.username = "hpc3", "tester"

    def exec(self, command, timeout=60.0):
        self.cmds.append(command)
        out = ""
        if command.startswith("sbatch"):
            out = "Submitted batch job 901"
        elif command.startswith("sacct"):
            out = "COMPLETED"
        return ExecResult(command=command, exit_status=0, stdout=out, stderr="")

    def read_bytes(self, path, max_bytes=None):
        return self.line.encode() if path.endswith(".result.json") else b""

    def get_file(self, *a, **k):
        pass


def test_a_gpu_job_carries_its_card_its_model_and_the_hosts_network():
    st = HPCSettings()
    cluster = _Cluster({"status": "ok", "n_genes": 3})
    ex = SlurmAnalysisExecutor(
        remote=cluster, container_image="/dfs/paperqa.sif", remote_workspace="/dfs/Temp/t/corpus/r1",
        scratch_dir="/dfs/Temp/t/scratch/paperqa/r1", entrypoint="python3 -m aiscientist.tools.paperqa_cli",
        job_prefix="bioagent_corpusscan", partition="gpu", account="ruic20_lab_gpu", gres="gpu:1",
        constraint="nvme", network=True, startup_timeout_s=5, run_timeout_s=5,
        inject_args={"model": corpus_scan.scan_model(st)},
        job_prelude=lambda name, scratch, res_f: corpus_scan.vllm_prelude(st, name=name, scratch=scratch,
                                                                         res_f=res_f))
    out = ex.run_tool("extract_haploinsufficient_genes", {}, ctx=None)
    assert out["status"] == "ok" and out["n_genes"] == 3
    script = next(c for c in cluster.cmds if "#!/bin/bash" in c)
    for line in ("#SBATCH --partition=gpu", "#SBATCH --account=ruic20_lab_gpu", "#SBATCH --gres=gpu:1",
                 "#SBATCH --constraint=nvme", "#SBATCH --cpus-per-task=8", "#SBATCH --mem=48G",
                 "#SBATCH --time=06:00:00", "#SBATCH --job-name=bioagent_corpusscan_"):
        assert line in script, line
    # the model starts before the scan's container, and the container shares the node's network
    assert script.index("setsid bash -c") < script.index("apptainer exec --containall")
    assert "--network none" not in script
    assert "AISCIENTIST_PAPERQA_LLM_BASE_URL=http://127.0.0.1:$(cat " in script
    staged = next(c for c in cluster.cmds if "AISCIENTIST_ANALYSIS_ARGS_EOF" in c)
    assert f'"model": "{corpus_scan.scan_model(st)}"' in staged and "llm_base_url" not in staged
