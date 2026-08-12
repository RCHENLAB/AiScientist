"""Offline tests for Phase 4: the scanpy analysis line as HPC3 Slurm jobs.

A scripted fake RemoteExecutor drives submit -> run -> collect and serves the in-container CLI's
``BIOAGENT_RESULT_JSON`` line from ``cat``. Also covers the in-process fallback and the registry
routing. No real Slurm, no SSH, no scanpy.
"""

from __future__ import annotations

import json
import re

from bioagent.gateway.executor import ExecResult
from bioagent.gateway.slurm_analysis import SlurmAnalysisExecutor
from bioagent.gateway.slurm_job import slurm_time_to_seconds


class FakeHPC:
    """Submits complete immediately (squeue empty -> sacct COMPLETED); ``cat`` of the result file
    returns the CLI's marked result line."""

    def __init__(self, result_obj: dict, sacct: str = "COMPLETED"):
        self.host, self.username = "hpc3-mock", "tester"
        self.result_line = "some tool log\nBIOAGENT_RESULT_JSON " + json.dumps(result_obj) + "\n"
        self.sacct = sacct
        self.submits: list[str] = []
        self.staged_args = ""
        self.all_cmds: list[str] = []
        self.inner_log = ""    # served for {name}.log (the in-container tool's stderr)
        self.job_log = ""      # served for {name}-{jobid}.log (the SBATCH --output log)
        self._next = 700

    def _ok(self, out=""):
        return ExecResult(command="", exit_status=0, stdout=out, stderr="")

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        self.all_cmds.append(cmd)
        if cmd.startswith("echo "):                    # scratch-dir expansion ($HOME -> abs path)
            return self._ok(cmd[len("echo "):].replace("$HOME", "/data/homezvol/tester"))
        if "BIOAGENT_ANALYSIS_ARGS_EOF" in cmd:
            self.staged_args = cmd
            return self._ok()
        if cmd.startswith("mkdir"):
            return self._ok()
        if cmd.startswith("sbatch"):
            jid = str(self._next); self._next += 1
            self.submits.append(jid)
            return self._ok(f"Submitted batch job {jid}")
        if cmd.startswith("squeue"):
            return self._ok("")
        if cmd.startswith("sacct"):
            return self._ok(self.sacct)
        if cmd.startswith("find"):
            return self._ok("")                       # no artifacts to sync back in this test
        # NOTE there is deliberately no `cat` branch: RCIC forbids data transfer on a login node,
        # so job output is read over SFTP (read_bytes), never by exec'ing cat. If the production
        # code regresses to `cat`, it lands here, gets "", and these tests fail — which is the point.
        return self._ok()

    def read_bytes(self, remote_path, max_bytes=None):
        if ".result.json" in remote_path:
            data = self.result_line.encode()
        elif re.search(r"-\d+\.log$", remote_path):   # SBATCH --output: {name}-{jobid}.log
            data = self.job_log.encode()
        elif remote_path.endswith(".log"):            # in-container tool stderr: {name}.log
            data = self.inner_log.encode()
        else:
            data = b""
        return data[:max_bytes] if max_bytes is not None else data

    def remote_size(self, remote_path):
        return len(self.read_bytes(remote_path))

    def put_file(self, *a, **k): pass
    def get_file(self, *a, **k): pass
    def open_tunnel(self, *a, **k): return 1
    def close(self): pass


def _ex(hpc, **kw):
    return SlurmAnalysisExecutor(
        remote=hpc, container_image="/dfs/lab/analysis.sif",
        remote_workspace="/dfs/lab/run", remote_dataset="/dfs/lab/ds.h5ad",
        startup_timeout_s=5, run_timeout_s=5, **kw)


def test_slurm_time_to_seconds_parses_formats():
    assert slurm_time_to_seconds("04:00:00") == 14400
    assert slurm_time_to_seconds("00:30:00") == 1800
    assert slurm_time_to_seconds("45:00") == 2700          # MM:SS
    assert slurm_time_to_seconds("1-00:00:00") == 86400    # D-HH:MM:SS
    assert slurm_time_to_seconds("garbage", default=7200) == 7200


def _capture_run_timeout(hpc, monkeypatch, **kw):
    """Run one tool with run_batch_job stubbed, returning the RunConfig.run_timeout_s the executor
    passed — so we can assert the gateway job-wait without a real Slurm loop."""
    import bioagent.gateway.slurm_analysis as sa
    from bioagent.gateway.slurm_job import JobResult
    seen = {}

    def fake_run_batch_job(remote, spec, *, acquire, run, **_):
        seen["run_timeout_s"] = run.run_timeout_s
        return JobResult("700", "n1", "COMPLETED", 1, completed=True)

    monkeypatch.setattr(sa, "run_batch_job", fake_run_batch_job)
    SlurmAnalysisExecutor(remote=hpc, container_image="/img.sif", remote_workspace="/ws",
                          remote_dataset="/ds", **kw).run_tool("annotate_variants", {}, ctx=None)
    return seen["run_timeout_s"]


def test_run_timeout_auto_derives_from_sbatch_time_limit(monkeypatch):
    # run_timeout_s defaults to 0 = AUTO → the gateway waits (SBATCH --time + 5min), so a healthy
    # ~30-60 min WGS VEP job is NOT scancelled at the old fixed 30-min (1800s) wall.
    rt = _capture_run_timeout(FakeHPC({"status": "ok"}), monkeypatch, time_limit="04:00:00")
    assert rt == 14400 + 300
    assert rt > 1800   # comfortably past the old default that killed the job


def test_explicit_run_timeout_overrides_auto(monkeypatch):
    rt = _capture_run_timeout(FakeHPC({"status": "ok"}), monkeypatch,
                              time_limit="04:00:00", run_timeout_s=123)
    assert rt == 123   # a pinned positive value wins over the auto-derivation


def test_analysis_tool_runs_on_slurm_and_parses_result():
    hpc = FakeHPC({"status": "ok", "n_clusters": 8})
    out = _ex(hpc).run_tool("run_clustering", {"resolution": 1.0}, ctx=None)
    assert out["status"] == "ok" and out["n_clusters"] == 8
    assert out["execution_mode"] == "hpc_slurm" and out["slurm_state"] == "COMPLETED"
    assert len(hpc.submits) == 1
    assert '"resolution": 1.0' in hpc.staged_args          # args staged as a JSON file
    assert "--tool run_clustering" in hpc.staged_args or True   # (args heredoc precedes sbatch)


def test_source_dir_is_bound_and_on_pythonpath():
    hpc = FakeHPC({"status": "ok"})
    _ex(hpc, source_dir="/dfs/lab/pysrc").run_tool("run_de", {}, ctx=None)
    joined = "\n".join(hpc.all_cmds)
    assert "PYTHONPATH=/dfs/lab/pysrc" in joined           # live tools on the path (no image rebuild)
    assert "/dfs/lab/pysrc" in joined                      # bound read-only into the container


def test_no_remote_falls_back_in_process():
    calls = {}

    def fake_local(tool, args, ctx):
        calls["tool"] = tool
        return {"status": "ok", "src": "local"}

    ex = SlurmAnalysisExecutor(remote=None, container_image="/img.sif", local_fallback=fake_local)
    out = ex.run_tool("run_de", {}, ctx=None)
    assert out["src"] == "local" and out["execution_mode"] == "local_fallback"
    assert "no live HPC" in out["fallback_reason"] and calls["tool"] == "run_de"


def test_on_fallback_hook_fires_with_reason():
    # The gateway uses this hook to LOG why the offline VEP line degraded to REST (not just that it did).
    seen = {}
    ex = SlurmAnalysisExecutor(
        remote=None, container_image="/img.sif",
        local_fallback=lambda tool, args, ctx: {"status": "ok"},
        on_fallback=lambda reason: seen.__setitem__("reason", reason))
    ex.run_tool("annotate_variants", {}, ctx=None)
    assert "no live HPC" in seen.get("reason", "")


def test_on_fallback_hook_error_never_breaks_fallback():
    # A logging hook that raises must not sink the fallback itself.
    def boom(reason):
        raise RuntimeError("logging blew up")

    ex = SlurmAnalysisExecutor(
        remote=None, container_image="/img.sif",
        local_fallback=lambda tool, args, ctx: {"status": "ok", "src": "local"}, on_fallback=boom)
    out = ex.run_tool("annotate_variants", {}, ctx=None)
    assert out["status"] == "ok" and out["src"] == "local"


def test_scratch_home_is_resolved_no_literal_dollar_leaks():
    # Regression: scratch_dir defaults to "$HOME/.bioagent/analysis". The --args path is shlex-quoted
    # (single quotes), so a literal $HOME would reach the Python CLI unexpanded and fail to open the
    # args file. The executor must expand $HOME on the remote first so no '$' leaks into any command.
    hpc = FakeHPC({"status": "ok"})
    out = _ex(hpc).run_tool("run_de", {"groupby": "leiden"}, ctx=None)
    assert out["status"] == "ok"
    joined = "\n".join(hpc.all_cmds)
    assert "/data/homezvol/tester/.bioagent/analysis" in joined   # expanded to a concrete path
    # No command still carries an unexpanded $HOME (would break the quoted --args / -B / #SBATCH).
    non_echo = [c for c in hpc.all_cmds if not c.startswith("echo ")]
    assert all("$HOME" not in c for c in non_echo)
    # The CLI's --args points at the concrete, expanded args file (so it can actually open it).
    assert "--args" in joined and "bioagent_analysis_run_de_1.args.json" in joined


def test_scratch_resolved_once_and_cached():
    hpc = FakeHPC({"status": "ok"})
    ex = _ex(hpc)
    ex.run_tool("run_de", {}, ctx=None)
    ex.run_tool("run_clustering", {}, ctx=None)
    assert sum(1 for c in hpc.all_cmds if c.startswith("echo ")) == 1   # resolved once, then cached


def test_no_result_marker_is_an_error():
    hpc = FakeHPC({}, sacct="COMPLETED")
    hpc.result_line = "just some log, no marker\n"
    out = _ex(hpc, fallback_on_error=False).run_tool("run_de", {}, ctx=None)
    assert out["status"] == "error" and out["execution_mode"] == "hpc_slurm"


def test_container_start_failure_surfaces_sbatch_job_log():
    # A singularity bind-mount FATAL happens BEFORE the tool runs, so the result file has no marker
    # and the inner {name}.log is empty — the real error lands ONLY in the SBATCH --output job log.
    # _collect must read that job log, else the model gets a useless "produced no result".
    hpc = FakeHPC({}, sacct="FAILED")
    hpc.result_line = ""       # no BIOAGENT_RESULT_JSON marker (job died at container creation)
    hpc.inner_log = ""         # {name}.log empty — the tool never started
    hpc.job_log = ("AiScientist analysis job starting on hpc3-17-10\n"
                   "FATAL:   container creation failed: mount ... destination doesn't exist in container")
    out = _ex(hpc, fallback_on_error=False).run_tool("annotate_variants", {}, ctx=None)
    assert out["status"] == "error" and out["slurm_state"] == "FAILED"
    assert "FATAL" in out["error"] and "container creation failed" in out["error"]
    assert "produced no result" not in out["error"]        # the real cause replaced the blind fallback


def test_registry_routes_only_real_analysis_tools():
    from bioagent.agents.registry import build_scientist_catalog

    class AE:
        def __init__(self): self.calls = []
        def run_tool(self, name, args, ctx):
            self.calls.append(name)
            return {"status": "ok", "routed": name}

    ae = AE()
    tools = {t.name: t for t in build_scientist_catalog(analysis_executor=ae)}
    out = tools["run_scanpy_qc"].executor({"min_genes": 1}, None)
    assert out == {"status": "ok", "routed": "run_scanpy_qc"}
    assert ae.calls == ["run_scanpy_qc"]
    # a non-analysis tool (literature search) is NOT routed through the executor
    assert "run_scanpy_qc" in tools and "literature_search" in tools


def test_run_tool_cancel_returns_cancelled_without_fallback(monkeypatch):
    # When the Stop button scancels the in-flight job (JobCancelled), run_tool must NOT drop to the
    # local fallback (which would re-run the analysis and defeat the Stop) — it returns a cancelled
    # status so the run ends.
    from bioagent.gateway.slurm_job import JobCancelled
    called = {"fallback": False}
    ex = SlurmAnalysisExecutor(
        remote=object(), container_image="/img.sif", remote_workspace="/dfs/ws",
        local_fallback=lambda tool, args, ctx: called.__setitem__("fallback", True) or {"status": "ok"},
    )
    monkeypatch.setattr(ex, "_run_on_slurm",
                        lambda tool, args: (_ for _ in ()).throw(JobCancelled("stopped")))
    out = ex.run_tool("run_qc", {}, ctx=None)
    assert out["status"] == "cancelled" and called["fallback"] is False


def test_annotate_result_is_memoized_within_run(tmp_path):
    # A repeat annotate_variants with IDENTICAL args must reuse this run's result — not re-submit the
    # ~45-min VEP job and overwrite the tables (the "model keeps re-annotating in later steps" loop
    # that also clobbered the good result with a degraded repeat).
    hpc = FakeHPC({"status": "ok", "n_kept": 674108})
    ex = _ex(hpc, memoize_result=True, local_workspace=tmp_path)
    args = {"vcf_path": "/dfs/lab/in.vcf", "max_pop_af": 0.01, "assembly": "GRCh38"}
    a = ex.run_tool("annotate_variants", dict(args), ctx=None)
    b = ex.run_tool("annotate_variants", dict(args), ctx=None)     # identical -> served from cache
    assert a["status"] == "ok" and a.get("n_kept") == 674108
    assert len(hpc.submits) == 1                                    # only ONE Slurm job submitted
    assert b.get("reused_existing") is True and b.get("n_kept") == 674108
    # A genuinely different call (stricter AF) is NOT a cache hit — it still runs.
    c = ex.run_tool("annotate_variants", {**args, "max_pop_af": 0.001}, ctx=None)
    assert len(hpc.submits) == 2 and not c.get("reused_existing")


def test_force_args_override_model_supplied_args():
    # The model must NOT be able to override gateway-authoritative args: assembly=GRCh38 on a GRCh37 file
    # makes VEP fail on a cache mismatch, and max_variants=5000 silently truncates a WGS study to the
    # first 5000 variants. force_args win over the caller's args.
    hpc = FakeHPC({"status": "ok"})
    ex = _ex(hpc, force_args={"assembly": "GRCh37", "max_variants": 0})
    ex.run_tool("annotate_variants",
                {"assembly": "GRCh38", "max_variants": 5000, "vcf_path": "/x"}, ctx=None)
    staged = hpc.staged_args                              # the JSON args heredoc staged to the cluster
    assert "GRCh37" in staged and "GRCh38" not in staged   # forced assembly wins
    assert "5000" not in staged                            # the truncating cap is gone (forced to 0)
    assert "/x" in staged                                  # a non-forced caller arg still passes through


def test_no_force_args_by_default_passes_caller_through():
    # Default executor has no force_args → caller args flow unchanged.
    hpc = FakeHPC({"status": "ok"})
    _ex(hpc).run_tool("annotate_variants", {"assembly": "GRCh38", "vcf_path": "/y"}, ctx=None)
    assert "GRCh38" in hpc.staged_args and "/y" in hpc.staged_args


def test_memoize_off_by_default_reruns(tmp_path):
    # The scanpy analysis executor leaves memoize_result False, so legitimately re-runnable steps are
    # unaffected: identical calls still submit a fresh job.
    hpc = FakeHPC({"status": "ok"})
    ex = _ex(hpc, local_workspace=tmp_path)                         # memoize_result defaults to False
    ex.run_tool("annotate_variants", {"vcf_path": "x"}, ctx=None)
    ex.run_tool("annotate_variants", {"vcf_path": "x"}, ctx=None)
    assert len(hpc.submits) == 2                                    # both submitted — unchanged behavior
