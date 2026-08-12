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

IN = "/dfs3b/ruic20_lab/runs/u1/query_aligned.h5ad"
MODEL = "/dfs3b/ruic20_lab/software/bioagent/scgpt/reference_model"
OUT = "/dfs3b/ruic20_lab/runs/u1/scgpt_out"


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
    assert "/dfs3b/ruic20_lab/runs/u1:/dfs3b/ruic20_lab/runs/u1:ro" in script
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
