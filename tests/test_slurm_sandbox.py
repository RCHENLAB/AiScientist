"""Offline tests for SlurmCodeExecutor — CodeAct snippets run as HPC3 Slurm batch jobs.

A scripted fake ``RemoteExecutor`` drives the whole submit -> run -> collect lifecycle (reusing
the same queue-plan idea as ``test_slurm_job``) and serves the snippet's captured out/err/rc files
from ``cat``. No real Slurm, no SSH.
"""

from __future__ import annotations

from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor
from aiscientist.gateway.executor import ExecResult


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
        if "AISCIENTIST_SNIPPET_EOF" in cmd:
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
    from aiscientist.gateway.slurm_job import JobCancelled
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


# --- a step's evidence is what the snippet WROTE ------------------------------
# Measured on a 12-hour production run: 141 tables and 21 figures reached disk, and every Critic
# verdict on a run_code step read "none of the claimed output CSVs appears in the tool evidence;
# each call lists only the input H5AD". The result carried stdout and a return code and nothing
# else, so `evidence_pointers` had nothing to find — which capped every such step in the 0.6-0.8
# band ("a material claim rests on prose with no backing artifact") and left the report unable to
# cite tables that existed the whole time.

def test_classify_written_uses_the_keys_the_harness_already_collects():
    from aiscientist.gateway.slurm_sandbox import classify_written

    out = classify_written([
        "tables/depth_overlap.csv", "figures/umap_by_arm.png",
        "tables/subtype_support.csv", "data/checkpoint_note.json",
    ])
    assert out["tables"] == ["tables/depth_overlap.csv", "tables/subtype_support.csv"]
    assert out["figures"] == ["figures/umap_by_arm.png"]
    assert out["artifacts"] == ["data/checkpoint_note.json"]


def test_job_logs_are_not_offered_as_scientific_evidence():
    from aiscientist.gateway.slurm_sandbox import classify_written

    # process/ holds the job's own stdout/stderr — scaffolding, not backing for a claim.
    assert classify_written(["process/runcode_7.log"]) == {}


def test_nothing_written_declares_nothing():
    from aiscientist.gateway.slurm_sandbox import classify_written

    # An empty key would read as "this step produced an empty table set", which is a different
    # and false claim from "this step produced nothing".
    assert classify_written([]) == {}


def _executor_with_remote(listings):
    """A SlurmCodeExecutor whose remote returns the queued `find` listings in order."""
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor

    class _Out:
        def __init__(self, text): self.stdout = text

    class _Remote:
        def __init__(self): self.calls = []
        def exec(self, cmd):
            self.calls.append(cmd)
            return _Out(listings.pop(0) if listings else "")

    ex = SlurmCodeExecutor(remote=_Remote(), container_image="x.sif",
                           artifacts_dir="/dfs/run/artifacts")
    return ex


def test_written_since_catches_new_files():
    ex = _executor_with_remote([
        "1700.5 /dfs/run/artifacts/tables/old.csv\n1800.0 /dfs/run/artifacts/tables/fresh.csv\n",
    ])
    before = {"tables/old.csv": "1700.5"}
    assert ex._written_since(before) == ["tables/fresh.csv"]


def test_written_since_catches_a_table_the_step_OVERWROTE():
    # A path-set diff would miss this, and rewriting a table is exactly what a revision does.
    ex = _executor_with_remote(["1900.0 /dfs/run/artifacts/tables/effects.csv\n"])
    assert ex._written_since({"tables/effects.csv": "1700.5"}) == ["tables/effects.csv"]


def test_untouched_files_from_earlier_steps_are_not_claimed():
    # The sync mirrors EVERY file back each time; only this snippet's output is its evidence.
    ex = _executor_with_remote(["1700.5 /dfs/run/artifacts/tables/from_step_two.csv\n"])
    assert ex._written_since({"tables/from_step_two.csv": "1700.5"}) == []


def test_a_broken_find_reports_no_evidence_rather_than_failing_the_snippet():
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor

    class _Remote:
        def exec(self, cmd): raise OSError("ssh died")

    ex = SlurmCodeExecutor(remote=_Remote(), container_image="x.sif",
                           artifacts_dir="/dfs/run/artifacts")
    assert ex._artifact_snapshot() == {}
    assert ex._written_since({}) == []


def test_the_declared_paths_are_what_evidence_pointers_collects():
    """The point of reusing these key names: nothing downstream needs to change."""
    from aiscientist.agents.research_harness import evidence_pointers
    from aiscientist.gateway.slurm_sandbox import classify_written

    result = {"status": "ok", "returncode": 0, "stdout": "…"}
    result.update(classify_written(["tables/depth_overlap.csv", "figures/umap_by_arm.png"]))
    found = evidence_pointers(result)
    assert "tables/depth_overlap.csv" in found
    assert "figures/umap_by_arm.png" in found


# --- the mirror back only moves what changed ---------------------------------
# Every run_code call re-fetched the entire artifacts directory over SFTP. Late in a production
# run that was 168 files per call, re-transferring output untouched for hours. It measured under a
# minute — the run's real cost was preflight — but it scales with both the file count and the file
# size, so one step writing a large checkpoint turns it from wasteful into slow.

def _sync_executor(tmp_path, remote_files, fetch_log):
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor

    class _Out:
        def __init__(self, text): self.stdout = text

    class _Remote:
        def exec(self, cmd):
            return _Out("".join(f"/dfs/run/artifacts/{r}\n" for r in remote_files))
        def get_file(self, remote, local):
            fetch_log.append(remote.rsplit("/artifacts/", 1)[1])
            from pathlib import Path as _P
            _P(local).write_text("x")

    return SlurmCodeExecutor(remote=_Remote(), container_image="x.sif",
                             artifacts_dir="/dfs/run/artifacts",
                             local_artifacts=str(tmp_path))


def test_only_the_files_this_snippet_wrote_are_fetched(tmp_path):
    remote = ["tables/old_a.csv", "tables/old_b.csv", "tables/fresh.csv"]
    fetched: list[str] = []
    ex = _sync_executor(tmp_path, remote, fetched)
    # Pretend the two older tables already came over on an earlier call.
    for rel in ("tables/old_a.csv", "tables/old_b.csv"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")

    ex._sync_artifacts_back(changed=["tables/fresh.csv"])
    assert fetched == ["tables/fresh.csv"]


def test_a_file_missing_locally_is_fetched_even_if_unchanged(tmp_path):
    """An earlier transfer may have failed; incremental copying must not make that permanent."""
    remote = ["tables/present.csv", "tables/lost_last_time.csv"]
    fetched: list[str] = []
    ex = _sync_executor(tmp_path, remote, fetched)
    (tmp_path / "tables").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tables/present.csv").write_text("x")

    ex._sync_artifacts_back(changed=[])          # this snippet wrote nothing
    assert fetched == ["tables/lost_last_time.csv"]


def test_no_changed_list_still_mirrors_everything(tmp_path):
    """A caller that does not know what changed keeps the previous, safe behaviour."""
    remote = ["tables/a.csv", "figures/b.png"]
    fetched: list[str] = []
    ex = _sync_executor(tmp_path, remote, fetched)
    for rel in remote:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")

    ex._sync_artifacts_back()
    assert sorted(fetched) == sorted(remote)


def test_sync_is_a_no_op_without_a_local_target(tmp_path):
    from aiscientist.gateway.slurm_sandbox import SlurmCodeExecutor

    class _Remote:
        def exec(self, cmd): raise AssertionError("must not touch the cluster")

    ex = SlurmCodeExecutor(remote=_Remote(), container_image="x.sif",
                           artifacts_dir="/dfs/run/artifacts", local_artifacts=None)
    ex._sync_artifacts_back(changed=["tables/a.csv"])      # no raise
