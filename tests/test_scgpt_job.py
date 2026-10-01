"""Offline tests for the scGPT GPU batch-inference engine (gateway/scgpt_job.py).

A scripted fake ``RemoteExecutor`` drives the Slurm queue + the post-run predictions
check deterministically — no real Slurm, no GPU, no scGPT image. Asserts the GPU job is
shaped correctly (gpu:1 + --nv + read-only model/dataset binds) and that the lifecycle
returns the predictions path on success and fails loudly otherwise.
"""

from __future__ import annotations

from bioagent.gateway.executor import ExecResult
from bioagent.gateway.settings import HPCSettings
from bioagent.gateway.scgpt_job import (
    build_scgpt_script,
    run_scgpt_inference,
    scgpt_job_name,
)
from bioagent.gateway.slurm_job import AcquireConfig, RunConfig, SlurmJobError
from bioagent.gateway.settings import LAB_STORAGE, REFERENCE_ROOT, SHARED_ROOT  # noqa: F401

IN = f"{LAB_STORAGE}/runs/u1/query_aligned.h5ad"
MODEL = f"{LAB_STORAGE}/software/bioagent/scgpt/reference_model"
OUT = f"{LAB_STORAGE}/runs/u1/scgpt_out"


class FakeScgptHost:
    """Fake HPC3 host: ``plans[i]`` is the i-th submitted job's per-poll squeue script
    (``"PD"`` | ``"R:node"`` | ``""``). ``predictions_present`` controls the post-run
    ``test -f predictions.csv`` probe."""

    def __init__(self, plans, finals=None, predictions_present=True):
        self.host = "hpc3-mock"
        self.username = "u1"
        self.plans = plans
        self.finals = finals or {}
        self.predictions_present = predictions_present
        self.submits: list[str] = []
        self.cancels: list[str] = []
        self.checks: list[str] = []
        self._poll: dict[str, int] = {}
        self._next = 2000

    def _ok(self, out=""):
        return ExecResult(command="", exit_status=0, stdout=out, stderr="")

    @staticmethod
    def _arg_after(flag, cmd):
        toks = cmd.split()
        return toks[toks.index(flag) + 1] if flag in toks else ""

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        if "BIOAGENT_EOF" in cmd or ("cat >" in cmd and "<<" in cmd):
            return self._ok()
        if cmd.startswith("mkdir"):
            return self._ok()
        if cmd.startswith("sbatch"):
            jid = str(self._next)
            self._next += 1
            self.submits.append(jid)
            return self._ok(f"Submitted batch job {jid}")
        if cmd.startswith("scancel"):
            self.cancels.append(cmd.split()[-1])
            return self._ok()
        if cmd.startswith("squeue"):
            jid = self._arg_after("-j", cmd)
            idx = self.submits.index(jid)
            plan = self.plans[idx] if idx < len(self.plans) else [""]
            n = self._poll.get(jid, 0)
            self._poll[jid] = n + 1
            val = plan[n] if n < len(plan) else plan[-1]
            if val == "":
                return self._ok("")
            if ":" in val:
                state, node = val.split(":", 1)
                return self._ok(f"{state}|{node}")
            return self._ok(f"{val}|")
        if cmd.startswith("sacct"):
            jid = self._arg_after("-j", cmd)
            return self._ok(self.finals.get(jid, "COMPLETED"))
        if cmd.startswith("test -f"):
            self.checks.append(cmd)
            return self._ok("OK" if self.predictions_present else "")
        return self._ok()

    def open_tunnel(self, remote_host, remote_port, local_port=0):
        return local_port or 1234

    def close(self):
        pass


# --- script shape ------------------------------------------------------------


def test_build_scgpt_script_is_a_contained_gpu_job():
    s = HPCSettings()
    script = build_scgpt_script(s, job_name="bioagent-scgpt-u1", input_h5ad=IN, model_dir=MODEL, out_dir=OUT)
    # GPU job, not CPU: requests a GPU and passes it into the container.
    assert "#SBATCH --gres=gpu:1" in script
    assert "--nv" in script
    # Contained: model + the dataset's directory are read-only; only out_dir is writable.
    assert f"-B {MODEL}:{MODEL}:ro" in script
    assert f"{LAB_STORAGE}/runs/u1:/dfs3b/ruic20_lab/runs/u1:ro" in script
    assert f"-B {OUT}:{OUT}" in script and f"{OUT}:{OUT}:ro" not in script
    assert "--containall" in script and "--network none" in script
    # Entry command got --input/--model/--out and runs the scGPT image.
    assert "--input" in script and "--model" in script and "--out" in script
    assert s.scgpt_image in script
    assert s.scgpt_entrypoint.split()[0] in script


def test_scgpt_job_name_is_per_user():
    assert scgpt_job_name("alice") == "bioagent-scgpt-alice"
    assert scgpt_job_name("") == "bioagent-scgpt-user"   # safe fallback


# --- lifecycle ---------------------------------------------------------------


def _cfgs():
    return (AcquireConfig(startup_timeout_s=1, poll_interval_s=0.01),
            RunConfig(run_timeout_s=5, poll_interval_s=0.01))


def test_run_scgpt_inference_returns_predictions_on_success():
    acquire, run = _cfgs()
    # acquire poll#0 = R, run poll#1 = "" (left queue) -> sacct COMPLETED -> predictions OK.
    fake = FakeScgptHost(plans=[["R:gpu-3-1", ""]], finals={"2000": "COMPLETED"})
    res = run_scgpt_inference(fake, HPCSettings(), input_h5ad=IN, model_dir=MODEL, out_dir=OUT,
                              acquire=acquire, run=run)
    assert res.job.completed is True and res.job.node == "gpu-3-1"
    assert res.predictions_csv == f"{OUT}/predictions.csv"
    assert fake.checks                                  # it verified the output exists
    assert fake.cancels == []                           # a clean run is never cancelled


def test_run_scgpt_inference_raises_on_failed_job():
    acquire, run = _cfgs()
    fake = FakeScgptHost(plans=[["R:gpu-3-1", "F"]], finals={"2000": "FAILED"})
    try:
        run_scgpt_inference(fake, HPCSettings(), input_h5ad=IN, model_dir=MODEL, out_dir=OUT,
                            acquire=acquire, run=run)
        assert False, "expected SlurmJobError"
    except SlurmJobError as exc:
        assert "did not complete" in str(exc) and exc.job_id == "2000"


def test_run_scgpt_inference_raises_when_no_predictions_written():
    acquire, run = _cfgs()
    # Job COMPLETES but the image wrote no predictions.csv -> loud failure, not silent ok.
    fake = FakeScgptHost(plans=[["R:gpu-3-1", ""]], finals={"2000": "COMPLETED"},
                         predictions_present=False)
    try:
        run_scgpt_inference(fake, HPCSettings(), input_h5ad=IN, model_dir=MODEL, out_dir=OUT,
                            acquire=acquire, run=run)
        assert False, "expected SlurmJobError"
    except SlurmJobError as exc:
        assert "no predictions.csv" in str(exc)


def test_scgpt_runner_captures_job_log_on_failure(tmp_path, monkeypatch):
    """On a failed scGPT job the runner must still pull the Slurm log into the bundle
    (process/scgpt_job.log) — the real error can no longer be stranded on HPC3."""
    import types
    from pathlib import Path

    from bioagent.gateway import scgpt_runner as sr

    ds = tmp_path / "q.h5ad"
    ds.write_text("x")
    gets: list[str] = []

    class FakeExec:
        username = "u1"

        def put_file(self, a, b):  # noqa: ANN001
            pass

        def get_file(self, remote, local):  # noqa: ANN001
            gets.append(remote)
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_text("boom", encoding="utf-8")

    def _boom(*a, **k):
        raise SlurmJobError("scGPT job failed", job_id="777")

    monkeypatch.setattr(sr, "run_scgpt_inference", _boom)
    runner = sr.build_scgpt_runner(FakeExec(), HPCSettings(), cluster_user_dir="/dfs3b/u1")
    ctx = types.SimpleNamespace(decisions={"dataset_path": str(ds)}, workspace=tmp_path)

    import pytest
    with pytest.raises(SlurmJobError):
        runner({}, ctx)

    assert any(g.endswith("-777.log") for g in gets)                       # fetched the job log
    assert (tmp_path / "artifacts" / "process" / "scgpt_job.log").exists()  # into the bundle


# --- the scGPT job must stay off known-dead nodes -------------------------------------------
#
# Prod sets BIOAGENT_SLURM_EXCLUDE=hpc3-gpu-n54-01 (GPU1 dead; Slurm still offers it). Only the
# vLLM serve job read it. build_analysis_script had no exclude parameter at all, so the scGPT and
# VL-review GPU jobs could be placed on that node — and fail for a reason that has nothing to do
# with the model, while reading exactly like a model failure.

def test_scgpt_job_honours_the_node_exclude():
    from bioagent.gateway.scgpt_job import build_scgpt_script
    from bioagent.gateway.settings import HPCSettings

    st = HPCSettings(exclude="hpc3-gpu-n54-01")
    script = build_scgpt_script(st, job_name="j", input_h5ad="/dfs3b/x/q.h5ad",
                                model_dir="/dfs3b/m", out_dir="/dfs3b/x/out")
    assert "#SBATCH --exclude=hpc3-gpu-n54-01" in script


def test_no_exclude_line_when_nothing_is_excluded():
    """An empty --exclude= is a Slurm error, not a no-op."""
    from bioagent.gateway.scgpt_job import build_scgpt_script
    from bioagent.gateway.settings import HPCSettings

    script = build_scgpt_script(HPCSettings(exclude=None), job_name="j", input_h5ad="/dfs3b/x/q.h5ad",
                                model_dir="/dfs3b/m", out_dir="/dfs3b/x/out")
    assert "--exclude" not in script


def test_the_vl_review_gpu_job_honours_it_too():
    """The same gap, the other GPU job that goes through build_analysis_script."""
    from bioagent.gateway.settings import HPCSettings
    from bioagent.gateway.vlreview_job import build_vlreview_script

    script = build_vlreview_script(HPCSettings(exclude="hpc3-gpu-n54-01"), job_name="j",
                                   pdf="/dfs3b/x/report.pdf", model_dir="/dfs3b/m",
                                   out_dir="/dfs3b/x/out")
    assert "#SBATCH --exclude=hpc3-gpu-n54-01" in script


# --- species harmonization -----------------------------------------------------------------
#
# The shipped scGPT model's vocabulary is HUMAN symbols (TFRC, APOE, RHO) and it predicts 123 human
# RETINA types. The DDX41 object is MOUSE (Tfrc, Apoe, Rho): exact matching kept 17 of 33,696 genes,
# normalising 15,307 cells over 17 genes divided by zero, and the GPU job died in log1p with
# "Input contains NaN" — measured on job 57187683. Case-folding recovers 16,599 genes and all 18
# canonical retinal markers.

import json
import subprocess
import sys

import pytest


def _run_harmonizer(tmp_path, var_names, vocab):
    ad = pytest.importorskip("anndata")
    np = pytest.importorskip("numpy")
    from bioagent.gateway.scgpt_job import _HARMONIZE_PY

    inp = tmp_path / "q.h5ad"
    a = ad.AnnData(X=np.ones((4, len(var_names)), dtype="float32"))
    a.var_names = var_names
    a.write_h5ad(inp)
    (tmp_path / "vocab.json").write_text(json.dumps({g: i for i, g in enumerate(vocab)}))
    prog = tmp_path / "h.py"
    prog.write_text(_HARMONIZE_PY)
    out, rep = tmp_path / "harm.h5ad", tmp_path / "rep.json"
    subprocess.run([sys.executable, str(prog), str(inp), str(tmp_path / "vocab.json"), str(out), str(rep)],
                   check=True, capture_output=True)
    return json.loads(rep.read_text()), out


def test_a_mouse_query_is_case_folded_onto_a_human_vocabulary(tmp_path):
    human = [f"GENE{i}" for i in range(1200)] + ["TFRC", "APOE", "RHO"]
    mouse = [f"Gene{i}" for i in range(1200)] + ["Tfrc", "Apoe", "Rho"]
    report, out = _run_harmonizer(tmp_path, mouse, human)

    assert report["exact_match"] == 0 and report["uppercase_match"] == 1203
    assert report["rule"] == "uppercase"
    assert report["assumption"] and "across species" in report["assumption"]
    import anndata as ad
    assert "TFRC" in set(ad.read_h5ad(out).var_names)


def test_a_human_query_passes_through_untouched(tmp_path):
    """A query already in the vocabulary's case must not be rewritten — or the report would
    claim a cross-species transfer that never happened."""
    human = [f"GENE{i}" for i in range(1200)]
    report, out = _run_harmonizer(tmp_path, human, human)
    assert report["rule"] == "none" and report["assumption"] is None
    assert not out.exists()                      # the original is used, unchanged


def test_the_command_fails_fast_on_a_harmonizer_error_and_falls_back_cleanly():
    from bioagent.gateway.scgpt_job import HARMONIZATION_NAME, build_scgpt_command
    from bioagent.gateway.settings import HPCSettings

    c = build_scgpt_command(HPCSettings(), input_h5ad="/dfs3b/x/q.h5ad", model_dir="/dfs3b/m",
                            out_dir="/dfs3b/x/out")
    assert "|| exit 1" in c                               # its own error, not a later NaN
    assert "if [ -f /dfs3b/x/out/query_harmonized.h5ad ]" in c
    assert 'INPUT=/dfs3b/x/q.h5ad;' in c                  # original unless a copy was written
    assert HARMONIZATION_NAME in c and "/dfs3b/m/vocab.json" in c
    import subprocess as _sp
    assert _sp.run(["bash", "-n"], input=c, text=True).returncode == 0
