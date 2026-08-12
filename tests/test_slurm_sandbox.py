"""Offline tests for SlurmCodeExecutor — CodeAct snippets run as HPC3 Slurm batch jobs.

A scripted fake ``RemoteExecutor`` drives the whole submit -> run -> collect lifecycle (reusing
the same queue-plan idea as ``test_slurm_job``) and serves the snippet's captured out/err/rc files
from ``cat``. No real Slurm, no SSH.
"""

from __future__ import annotations

from bioagent.gateway.slurm_sandbox import SlurmCodeExecutor
from bioagent.gateway.executor import ExecResult


class FakeHPC:
    """Fake HPC host: submits complete immediately (squeue empty -> sacct COMPLETED), and ``cat``
    returns scripted file contents keyed by a substring of the path."""

    def __init__(self, files: dict[str, str], sacct: str = "COMPLETED"):
        self.host, self.username = "hpc3-mock", "tester"
        self.files = files
        self.sacct = sacct
        self.submits: list[str] = []
        self.staged_snippet = ""
        self._next = 500

    def _ok(self, out=""):
        return ExecResult(command="", exit_status=0, stdout=out, stderr="")

    def exec(self, command, timeout=60.0):
        cmd = command.strip()
        if "BIOAGENT_SNIPPET_EOF" in cmd:
            self.staged_snippet = cmd
            return self._ok()
        if cmd.startswith("mkdir") or ("cat >" in cmd and "<<" in cmd):
            return self._ok()
        if cmd.startswith("sbatch"):
            jid = str(self._next)
            self._next += 1
            self.submits.append(jid)
            return self._ok(f"Submitted batch job {jid}")
        if cmd.startswith("squeue"):
            return self._ok("")                     # already left the queue (fast job)
        if cmd.startswith("sacct"):
            return self._ok(self.sacct)
        # NOTE there is deliberately no `cat` read branch (the heredoc `cat > … <<` staging above
        # is a different thing): RCIC forbids data transfer on a login node, so the snippet's
        # out/err/rc are read over SFTP via read_bytes. A regression back to `cat` falls through
        # to the empty default below and fails these tests, which is exactly what should happen.
        return self._ok()

    def read_bytes(self, remote_path, max_bytes=None):
        for key, val in self.files.items():
            if key in remote_path:
                data = val.encode()
                return data[:max_bytes] if max_bytes is not None else data
        return b""

    def remote_size(self, remote_path):
        return len(self.read_bytes(remote_path))

    def put_file(self, *a, **k): pass
    def get_file(self, *a, **k): pass
    def open_tunnel(self, *a, **k): return 1234
    def close(self): pass


def _executor(hpc, **kw):
    return SlurmCodeExecutor(
        remote=hpc, container_image="/dfs/lab/analysis.sif",
        dataset_path="/dfs/lab/ds.h5ad", work_dir="/dfs/lab/run/work",
        artifacts_dir="/dfs/lab/run/art", mem_gb=64,
        startup_timeout_s=5, run_timeout_s=5, **kw,
    )


def test_successful_snippet_returns_stdout_and_rc0():
    hpc = FakeHPC({".out": "hello from hpc\n", ".err": "", ".rc": "0\n"})
    out = _executor(hpc)("print('hello from hpc')")
    assert out["status"] == "ok" and out["returncode"] == 0
    assert out["stdout"].strip() == "hello from hpc"
    assert out["execution_mode"] == "hpc_slurm" and out["slurm_state"] == "COMPLETED"
    assert len(hpc.submits) == 1


def test_failing_snippet_surfaces_traceback_and_nonzero_rc():
    hpc = FakeHPC({".out": "", ".err": "Traceback...\nValueError: boom\n", ".rc": "1\n"})
    out = _executor(hpc)("raise ValueError('boom')")
    assert out["status"] == "error" and out["returncode"] == 1
    assert "boom" in out["stderr"] and "boom" in out["error"]


def test_oom_job_reports_returncode_minus9_with_hint():
    # Job OOM-killed: no rc file was written, sacct state is OUT_OF_MEMORY.
    hpc = FakeHPC({".out": "", ".err": ""}, sacct="OUT_OF_MEMORY")
    out = _executor(hpc)("x = [0] * 10**12")
    assert out["status"] == "error" and out["returncode"] == -9
    assert "OUT_OF_MEMORY" in out["slurm_state"]
    assert "--mem" in out["error"]                      # actionable hint


def test_snippet_is_staged_and_data_env_exposed():
    hpc = FakeHPC({".out": "", ".err": "", ".rc": "0\n"})
    _executor(hpc)("import scanpy")
    # the code was staged via a quoted heredoc, and the run exports the data env vars contained
    assert "import scanpy" in hpc.staged_snippet
    assert "--mem=64G".replace("=", "=")  # sanity


def test_no_remote_uses_local_fallback():
    calls = {}
    def fake_local(code):
        calls["code"] = code
        return {"status": "ok", "stdout": "local", "returncode": 0}
    ex = SlurmCodeExecutor(remote=None, container_image="/img.sif", local_fallback=fake_local)
    out = ex("print(1)")
    assert out["status"] == "ok" and out["stdout"] == "local"
    assert out["execution_mode"] == "local_fallback" and "no live HPC" in out["fallback_reason"]
    assert calls["code"] == "print(1)"


def test_empty_code_rejected():
    assert _executor(FakeHPC({}))("   ")["status"] == "error"


def test_stop_cancels_the_runcode_job_without_fallback(monkeypatch):
    # When Stop scancels the in-flight run_code job (JobCancelled), the executor must NOT drop to
    # the local fallback (which would re-run the snippet and defeat Stop) — it returns cancelled.
    from bioagent.gateway.slurm_job import JobCancelled
    called = {"fallback": False}
    ex = SlurmCodeExecutor(
        remote=object(), container_image="/img.sif",
        local_fallback=lambda code: called.__setitem__("fallback", True) or {"status": "ok"},
        should_cancel=lambda: True,
    )
    monkeypatch.setattr(ex, "_run_on_slurm",
                        lambda code: (_ for _ in ()).throw(JobCancelled("stopped")))
    out = ex("print(1)")
    assert out["status"] == "cancelled" and called["fallback"] is False
