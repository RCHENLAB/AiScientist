"""Run the research lab's ``run_code`` (CodeAct) snippets as **HPC3 Slurm batch jobs**.

This is the long-term home for CodeAct execution (see the team's "analysis-as-HPC3-Slurm-job"
direction). The local :class:`~bioagent.agents.sandbox.CodeSandbox` runs snippets as an *uncapped*
subprocess on the eyeserver, so a snippet that loads the full AnnData and ``.copy()``-s large
subsets in a loop gets OOM-killed by the host (the ``returncode == -9`` we saw in production). On
HPC3, CPU and RAM are effectively unlimited *and* Slurm enforces a real per-job memory cap via
``#SBATCH --mem`` (cgroups) — so an over-budget snippet fails cleanly as ``OUT_OF_MEMORY`` instead
of taking down the shared server.

``SlurmCodeExecutor`` implements the same injectable ``CodeExecutor`` contract as ``CodeSandbox``
(``executor(code) -> {"status","stdout","stderr","returncode",...}``) and reuses the proven Slurm
lifecycle in :mod:`bioagent.gateway.slurm_job` (submit → wait for a node with startup retry → wait
for completion → collect output). Everything goes through the ``RemoteExecutor`` protocol, so the
whole flow runs offline against a scripted fake in tests — no real Slurm, no SSH.

Isolation & data safety come from the same Singularity contract the scGPT job already uses: the
dataset is bind-mounted **read-only**, only work/artifacts are writable, and ``--containall`` hides
every other host path. Network access follows :func:`~bioagent.agents.sandbox.sandbox_network_enabled`
— ON by default (Jin Li 2026-07-08: prioritize effectiveness), toggled off with
``BIOAGENT_SANDBOX_NETWORK=0`` (which restores ``--net --network none``). If no live remote is wired,
the executor falls back to a local ``CodeSandbox`` so a run never hard-fails just because HPC is
unavailable.

Example sbatch request this produces (CPU analysis job, real memory cap; network on by default) —
also documented in the ``run`` skill::

    #!/bin/bash
    #SBATCH --job-name=bioagent_runcode_3
    #SBATCH --partition=standard
    #SBATCH --account=<lab_account>
    #SBATCH --cpus-per-task=8
    #SBATCH --mem=64G            # <-- the real, cgroup-enforced memory cap
    #SBATCH --time=01:00:00
    #SBATCH --output=<scratch>/bioagent_runcode_3-%j.log

    singularity exec --containall --writable-tmpfs \\
      -B <dataset>:<dataset>:ro -B <work>:<work> -B <artifacts>:<artifacts> \\
      <image> bash -lc 'python <scratch>/snippet_3.py > out 2> err; echo $? > rc'
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..agents.sandbox import sandbox_network_enabled
from .executor import RemoteExecutor
from .job_store import JobRecord, JobStore
from .slurm_job import (
    AcquireConfig, JobCancelled, RunConfig, SlurmJobError, SlurmJobSpec, build_analysis_script,
    run_batch_job, singularity_exec)

# A heredoc sentinel that will never collide with real Python source.
_SNIPPET_EOF = "BIOAGENT_SNIPPET_EOF_c0ffee"


def _tail(text: str, n: int) -> str:
    return text if len(text) <= n else "…(truncated)…\n" + text[-n:]


@dataclass
class SlurmCodeExecutor:
    """A ``CodeExecutor`` that runs each snippet as a contained CPU batch job on HPC3.

    ``remote`` is the connected ``RemoteExecutor`` (SSH to HPC3). ``container_image`` is the same
    Singularity image already used for the analysis stack. The dataset/work/artifacts paths are the
    **remote** (HPC3 DFS) locations the snippet reads/writes; they are exposed to the snippet as the
    usual ``BIOAGENT_DATASET`` / ``BIOAGENT_WORK`` / ``BIOAGENT_ARTIFACTS`` env vars.
    """

    remote: RemoteExecutor | None
    container_image: str
    # These three are BIND-MOUNTED into the container on the COMPUTE NODE, so they must be paths the
    # HPC3 node can see — i.e. dfs3b, NOT the eyeserver-local /data/BioAgent run dir (binding a local
    # path there fails container creation with exit 127; see app.py's run_code wiring). Mirror the
    # analysis/VEP lines, which pass dfs3b paths via _storage_base(conn) / decisions['hpc_primary'].
    dataset_path: str | None = None
    work_dir: str | None = None
    artifacts_dir: str | None = None
    # Eyeserver-local dir to mirror the job's artifacts back into (the report bundler is still on the
    # host). NOT bind-mounted — only a get_file target — so a local path here is correct. None → no sync.
    local_artifacts: str | None = None
    scratch_dir: str = "$HOME/.bioagent/runcode"
    mem_gb: int = 64
    cpus: int = 8
    partition: str = "standard"
    account: str = ""
    time_limit: str = "01:00:00"
    container_module: str = ""
    container_bin: str = "singularity"
    max_output: int = 20_000
    startup_timeout_s: int = 600     # HPC queues can be long; keep the whole run patient
    run_timeout_s: int = 1800
    # Fallback used when no live remote is wired (offline/mock, or HPC not enabled). Kept as a
    # plain callable so this module has no hard dependency on CodeSandbox's shape.
    local_fallback: object | None = None
    fallback_on_error: bool = True
    # Optional durable registry: when set, each submitted job is recorded the instant its
    # job_id is known and marked terminal when it finishes, so a gateway restart mid-run
    # leaves a reattachable record instead of an orphaned job (see slurm_job.reattach_job).
    job_store: JobStore | None = None
    owner: str = ""
    # Stop button: when this fires mid-job the in-flight run_code Slurm job is scancelled and the
    # call ends at once (no local fallback — that would re-run the snippet and defeat Stop). Set to
    # conn.chat_stop.is_set. Without it, a Stop can't cancel a run_code job (the old bug).
    should_cancel: Callable[[], bool] | None = None
    _counter: int = field(default=0, repr=False)
    _scratch: str | None = field(default=None, repr=False)

    @property
    def mem_mb(self) -> int:
        """The per-snippet memory cap in MB — read by ``build_run_code_context`` so the injected
        run_code guidance quotes the REAL HPC cap (not the local sandbox default)."""
        return int(self.mem_gb) * 1024

    def __call__(self, code: str) -> dict[str, object]:
        return self.run(code)

    def run(self, code: str) -> dict[str, object]:
        code = (code or "").strip()
        if not code:
            return {"status": "error", "error": "no code provided"}
        if self.remote is None:
            return self._fallback(code, "no live HPC connection")
        try:
            return self._run_on_slurm(code)
        except JobCancelled:
            return {"status": "cancelled", "error": "Run cancelled by the user.",
                    "returncode": None, "stdout": "", "stderr": ""}
        except SlurmJobError as exc:
            if self.fallback_on_error:
                return self._fallback(code, f"Slurm job failed: {exc}")
            return {"status": "error", "error": f"Slurm job failed: {exc}",
                    "returncode": None, "stdout": "", "stderr": str(getattr(exc, "detail", "") or "")}

    # -- internals ------------------------------------------------------------

    def _fallback(self, code: str, reason: str) -> dict[str, object]:
        if callable(self.local_fallback):
            out = self.local_fallback(code)
            if isinstance(out, dict):
                out.setdefault("execution_mode", "local_fallback")
                out["fallback_reason"] = reason
                return out
        return {"status": "not_enabled",
                "note": f"CodeAct HPC execution unavailable ({reason}) and no local fallback is set."}

    def _resolved_scratch(self) -> str:
        """Expand shell vars (e.g. ``$HOME``) in ``scratch_dir`` to a concrete remote path ONCE.

        ``scratch`` is used **unquoted** in the SSH shell (``mkdir``/heredoc — ``$HOME`` expands) but
        also lands in the ``singularity -B`` binds and the ``#SBATCH --output`` directive, and **Slurm
        does NOT expand ``$HOME`` in ``#SBATCH`` lines** — so an unresolved ``$HOME`` makes the job's
        log path a literal ``$HOME/.bioagent/runcode/...``. Resolve up front (mirrors
        ``SlurmAnalysisExecutor._resolved_scratch``). If the remote can't expand it (offline/mock), keep
        the literal so behaviour is unchanged."""
        if self._scratch is not None:
            return self._scratch
        scratch = self.scratch_dir
        if "$" in scratch and self.remote is not None:
            got = self.remote.exec(f"echo {scratch}")
            lines = (got.stdout or "").strip().splitlines()
            if got.ok and lines and "$" not in lines[0]:
                scratch = lines[0].strip()
        self._scratch = scratch
        return scratch

    def _run_on_slurm(self, code: str) -> dict[str, object]:
        self._counter += 1
        name = f"bioagent_runcode_{self._counter}"
        scratch = self._resolved_scratch()
        snippet = f"{scratch}/snippet_{self._counter}.py"
        out_f, err_f, rc_f = (f"{scratch}/{name}.{ext}" for ext in ("out", "err", "rc"))

        # Stage the snippet on the cluster (quoted heredoc → no shell/`$` expansion of the code).
        write = self.remote.exec(
            f"mkdir -p {scratch} && cat > {snippet} <<'{_SNIPPET_EOF}'\n{code}\n{_SNIPPET_EOF}"
        )
        if not write.ok:
            raise SlurmJobError("failed to stage the snippet on the cluster", detail=write.stderr)

        # Expose the run's data to the snippet exactly like CodeSandbox does, then run it contained,
        # capturing stdout/stderr/exit-code to files (so a non-zero exit does not abort the sbatch
        # script — we want the traceback + returncode back, not a bare job failure).
        exports = "; ".join(
            f'export {var}={shlex_quote(val)}'
            for var, val in (
                ("BIOAGENT_DATASET", self.dataset_path),
                ("BIOAGENT_WORK", self.work_dir),
                ("BIOAGENT_ARTIFACTS", self.artifacts_dir),
                ("MPLBACKEND", "Agg"),
            )
            if val
        )
        inner_payload = f"{exports}; python {snippet} > {out_f} 2> {err_f}; echo $? > {rc_f}"
        binds_ro = tuple(p for p in (self.dataset_path,) if p)
        binds_rw = tuple(p for p in (self.work_dir, self.artifacts_dir, scratch) if p)
        inner = singularity_exec(
            self.container_image, inner_payload,
            binds_ro=binds_ro, binds_rw=binds_rw, nv=False, network=sandbox_network_enabled(),
            container_bin=self.container_bin,
        )
        script = build_analysis_script(
            name, inner, partition=self.partition, cpus=self.cpus, mem_gb=self.mem_gb,
            time_limit=self.time_limit, account=self.account, gres="",  # CPU-only analysis job
            container_module=self.container_module, log_dir=scratch,
        )
        spec = SlurmJobSpec(script=script, job_name=name, submit_dir=scratch)

        def _record(job_id: str) -> None:
            if self.job_store is not None:
                self.job_store.record(JobRecord(
                    job_id=job_id, job_name=name, kind="runcode",
                    owner=self.owner or getattr(self.remote, "username", ""),
                    submit_dir=scratch, state="SUBMITTED",
                ))

        result = run_batch_job(
            self.remote, spec,
            acquire=AcquireConfig(startup_timeout_s=self.startup_timeout_s),
            run=RunConfig(run_timeout_s=self.run_timeout_s),
            on_submit=_record if self.job_store is not None else None,
            should_cancel=self.should_cancel,   # Stop scancels the in-flight run_code job
        )
        if self.job_store is not None:
            self.job_store.mark(result.job_id, state=result.state, node=result.node,
                                completed=result.completed)
        out = self._collect(result, out_f, err_f, rc_f)
        self._sync_artifacts_back()
        return out

    def _sync_artifacts_back(self) -> None:
        """Mirror any files the snippet wrote under the (dfs3b) artifacts dir back to the eyeserver run
        dir, so the still-local report bundler picks them up — the analysis line does the same. Work
        checkpoints stay on dfs3b for the next step. Best-effort; no-op without a local target."""
        if not self.local_artifacts or not self.artifacts_dir or self.remote is None:
            return
        remote_art = str(self.artifacts_dir)
        listing = self.remote.exec(f"find {shlex_quote(remote_art)} -type f 2>/dev/null").stdout or ""
        for remote_file in filter(None, (ln.strip() for ln in listing.splitlines())):
            rel = remote_file[len(remote_art):].lstrip("/")
            local_file = Path(self.local_artifacts) / rel
            try:
                local_file.parent.mkdir(parents=True, exist_ok=True)
                self.remote.get_file(remote_file, str(local_file))
            except OSError:
                continue

    def _collect(self, result, out_f: str, err_f: str, rc_f: str) -> dict[str, object]:
        # Read over SFTP, not `cat` on a login node — job output is file content, i.e. a transfer.
        def _text(path: str) -> str:
            return self.remote.read_bytes(path).decode("utf-8", errors="replace")

        stdout = _text(out_f)
        stderr = _text(err_f)
        rc_raw = _text(rc_f).strip()
        try:
            returncode = int(rc_raw)
        except (TypeError, ValueError):
            # No rc file: the job died before the snippet finished — most often OUT_OF_MEMORY, the
            # very case this executor exists to surface cleanly.
            returncode = -9 if result.state.upper().startswith("OUT_OF_MEMORY") else None
        status = "ok" if returncode == 0 else "error"
        out: dict[str, object] = {
            "status": status,
            "returncode": returncode,
            "stdout": _tail(stdout, self.max_output),
            "stderr": _tail(stderr, self.max_output),
            "execution_mode": "hpc_slurm",
            "slurm_state": result.state,
        }
        if status == "error":
            hint = ""
            if returncode is None or (result.state or "").upper().startswith("OUT_OF_MEMORY"):
                hint = (f" (Slurm state {result.state}; if OUT_OF_MEMORY, raise --mem above "
                        f"{self.mem_gb}G or reduce the snippet's peak memory)")
            out["error"] = (_tail(stderr, 2000).strip() or f"exited with code {returncode}{hint}")
        return out


def shlex_quote(value: str) -> str:
    """Local re-export of ``shlex.quote`` (kept importable for tests without pulling in shlex)."""
    import shlex
    return shlex.quote(str(value))
