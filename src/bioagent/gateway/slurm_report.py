"""Render the report (pandoc + XeLaTeX) as an **HPC3 CPU Slurm job** instead of on the eyeserver.

Phase 5 of the HPC3 offload. ``tools/report.py`` normally shells out to ``pandoc``/``xelatex`` on
the gateway host — a texlive render is minutes of CPU that competes with every other session (and
forces a multi-GB texlive install on the eyeserver). This renderer implements the SAME
``(cmd, cwd, out_path, timeout_s) -> (ok, err)`` contract as ``report._run_pandoc``, so
``build_pdf_report(render_fn=...)`` uses it transparently: it tars the render bundle (markdown +
figures/tables + the header/lua helpers) to dfs3b, runs the exact pandoc command inside a texlive
Singularity image as a batch job, and pulls the produced PDF/DOCX back.

No bioagent code runs in the image — pandoc is a self-contained binary — so this needs only a
deps-only texlive/pandoc image (deploy/report/report.def). Reuses the slurm_job lifecycle and the
RemoteExecutor protocol, so it runs offline against a scripted fake in tests. If no live remote is
wired, it falls back to the local ``_run_pandoc``.
"""

from __future__ import annotations

import os
import shlex
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .executor import RemoteExecutor
from .slurm_job import (
    AcquireConfig, RunConfig, SlurmJobError, SlurmJobSpec, build_analysis_script, run_batch_job)


@dataclass
class SlurmReportRenderer:
    """A ``report._run_pandoc``-compatible callable that renders one output on HPC3.

    ``remote_base`` is a dfs3b dir the render bundle is staged under. ``local_fallback`` is the
    local ``_run_pandoc`` used when there is no live remote / on Slurm failure.
    """

    remote: RemoteExecutor | None
    container_image: str
    remote_base: str
    scratch_dir: str = "$HOME/.bioagent/report"
    mem_gb: int = 8
    cpus: int = 2
    partition: str = "standard"
    account: str = ""
    time_limit: str = "00:30:00"
    container_module: str = ""
    container_bin: str = "singularity"
    startup_timeout_s: int = 600
    run_timeout_s: int = 1800
    local_fallback: Callable[[list, Path, Path, float], tuple] | None = None
    _counter: int = field(default=0, repr=False)
    _scratch: str | None = field(default=None, repr=False)

    def __call__(self, cmd: list[str], cwd: Path, out_path: Path, timeout_s: float) -> tuple[bool, str]:
        cwd, out_path = Path(cwd), Path(out_path)
        if self.remote is None:
            return self._fallback(cmd, cwd, out_path, timeout_s)
        try:
            return self._render_on_slurm(cmd, cwd, out_path, timeout_s)
        except Exception as exc:  # noqa: BLE001 - this renderer's contract is to RETURN (ok, err),
            # NEVER to throw. Besides SlurmJobError, the remote ops here (mkdir/tar/put_file) surface
            # an SSH timeout or drop as GatewayError and an SFTP miss as OSError; ANY of those leaking
            # out turned a slow report step into a fatal "Chat error" that discarded an ALREADY
            # COMPLETED run (analysis + checkpoints done — only the render failed). So catch every
            # remote failure and degrade to the local pandoc renderer; if it is missing or also
            # fails, return a diagnostic. Never re-raise.
            if self.local_fallback is not None:
                ok, ferr = self._fallback(cmd, cwd, out_path, timeout_s)
                if ok:
                    return (True, "")
                return (False, f"HPC render failed ({type(exc).__name__}: {exc}); "
                               f"local fallback also failed: {ferr}")
            return (False, f"Slurm render failed ({type(exc).__name__}): {exc}")

    # -- internals ------------------------------------------------------------

    def _fallback(self, cmd, cwd, out_path, timeout_s) -> tuple[bool, str]:
        if callable(self.local_fallback):
            return self.local_fallback(cmd, cwd, out_path, timeout_s)
        return (False, "no renderer available (no live HPC connection and no local fallback)")

    def _resolved_scratch(self) -> str:
        """Expand ``$HOME`` in ``scratch_dir`` to a concrete absolute remote path ONCE (cached).

        The scratch dir leaks into the ``singularity -B`` bind AND ``#SBATCH --output``. Slurm does
        NOT expand ``$HOME`` in ``--output``, and shlex-quoting the bind freezes ``$HOME`` as a
        literal — so a raw ``$HOME/.bioagent/report`` made the render Slurm job die instantly (exit
        127: no writable log, unbindable mount), which is why NO report ever rendered on HPC3.
        Resolving to an absolute path up front (exactly what the analysis executor does) fixes every
        use site. Keep the literal if the remote lookup can't expand it (offline/mock) so behaviour
        is unchanged rather than guessing."""
        if self._scratch is not None:
            return self._scratch
        scratch = self.scratch_dir
        if "$" in scratch and self.remote is not None:
            got = self.remote.exec(f"echo {scratch}")
            lines = (getattr(got, "stdout", "") or "").strip().splitlines()
            if getattr(got, "ok", False) and lines and "$" not in lines[0]:
                scratch = lines[0].strip()
        self._scratch = scratch
        return scratch

    def _render_on_slurm(self, cmd: list[str], cwd: Path, out_path: Path, timeout_s: float) -> tuple[bool, str]:
        self._counter += 1
        # STALE-OUTPUT GUARD. Success below is judged by "out_path exists and is non-empty" (get_file
        # is soft-failed). A PRIOR render of this same run dir (e.g. an amend/resume of a run that
        # first produced a DIFFERENT report) leaves an old report.pdf here — it would (a) be tarred
        # into the bundle and (b) survive a FAILED pandoc run, so get_file pulls the stale file back
        # (or the leftover local file is never overwritten) and we falsely report success, shipping
        # the previous report. Delete the local target up front so ONLY a freshly rendered file can
        # pass the exists() check and so the stale file is never bundled.
        try:
            out_path.unlink()
        except OSError:
            pass
        # Stage root = lowest dir containing BOTH the assets (cwd) and the output — so md, figures,
        # header/lua helpers and the output path all round-trip under one prefix we can remap.
        root = Path(os.path.commonpath([str(cwd), str(out_path)]))
        remote_root = f"{self.remote_base}/r{self._counter}_{root.name}"
        scratch = self._resolved_scratch()   # absolute — a literal $HOME here 127-killed every render

        fd, tmp = tempfile.mkstemp(suffix=".tgz")
        os.close(fd)
        try:
            with tarfile.open(tmp, "w:gz") as tar:
                tar.add(str(root), arcname=".")
            # ``remote_root`` collides across runs (per-instance counter restarts at 1, dir name is
            # the constant "artifacts"), so a previous run's stale report.pdf can linger here and be
            # pulled back if this run's pandoc fails. Wipe it first so each render starts clean and a
            # failed render leaves NO output to masquerade as success.
            r = self.remote.exec(
                f"rm -rf {shlex.quote(remote_root)} && mkdir -p {shlex.quote(remote_root)} {shlex.quote(scratch)}")
            if not r.ok:
                raise SlurmJobError("failed to make the remote render dir", detail=r.stderr)
            remote_tgz = f"{scratch}/render_{self._counter}.tgz"
            self.remote.put_file(tmp, remote_tgz)
            r = self.remote.exec(f"tar xzf {shlex.quote(remote_tgz)} -C {shlex.quote(remote_root)}")
            if not r.ok:
                raise SlurmJobError("failed to unpack the render bundle on the cluster", detail=r.stderr)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        # Remap every local path (all under `root`) to the staged remote dir.
        base = str(root)
        rcmd = [a.replace(base, remote_root) for a in cmd]
        remote_out = str(out_path).replace(base, remote_root)

        # Build the singularity line DIRECTLY (not via singularity_exec, which wraps the command in
        # `bash -lc` — the deps-only pandoc image has no bash). pandoc runs as the container command;
        # TeX's font/format cache goes under a writable HOME on the tmpfs via --env (no shell needed).
        sing = [
            self.container_bin, "exec", "--containall", "--writable-tmpfs", "--net", "--network", "none",
            "--env", "HOME=/tmp",
            "-B", f"{remote_root}:{remote_root}", "-B", f"{scratch}:{scratch}",
            self.container_image, *rcmd,
        ]
        inner = " ".join(shlex.quote(a) for a in sing)
        name = f"bioagent_report_{self._counter}"
        script = build_analysis_script(
            name, inner, partition=self.partition, cpus=self.cpus, mem_gb=self.mem_gb,
            time_limit=self.time_limit, account=self.account, gres="",
            container_module=self.container_module, log_dir=scratch)
        spec = SlurmJobSpec(script=script, job_name=name, submit_dir=scratch)
        result = run_batch_job(
            self.remote, spec,
            acquire=AcquireConfig(startup_timeout_s=self.startup_timeout_s),
            run=RunConfig(run_timeout_s=self.run_timeout_s))

        # Pull the produced output back. A MISSING remote file (the common failure — pandoc/xelatex
        # errored, so no PDF was written) makes ``get_file`` raise ``GatewayError`` (a RuntimeError,
        # NOT an OSError). Catch broadly: this renderer's contract is to RETURN (ok, err), never to
        # throw — a leaked exception here crashed the whole run instead of degrading (report step
        # has no try/except around it). We fall back to local pandoc / markdown below.
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            self.remote.get_file(remote_out, str(out_path))
        except Exception:  # noqa: BLE001 - missing output / SFTP / SSH; handled as a soft failure
            pass
        if out_path.exists() and out_path.stat().st_size > 0:
            return (True, "")

        # No output. Try the local fallback first — if it renders, the user gets their report and
        # the HPC failure is moot (markdown-only is the final safety net one level up).
        fallback_err = ""
        if self.local_fallback is not None:
            ok, fallback_err = self._fallback(cmd, cwd, out_path, timeout_s)
            if ok:
                return (True, "")

        # Truly failed. The real cause (LaTeX error, missing figure, bad bind) is in the job's Slurm
        # log — fetch its tail so the failure is DIAGNOSABLE instead of an opaque "no output".
        detail = f"render produced no output on HPC3 (Slurm state {result.state})"
        log_tail = self._fetch_log_tail(name, result.job_id)
        if log_tail:
            detail += f"\n--- {name}-{result.job_id}.log (tail) ---\n{log_tail}"
        if fallback_err:
            detail += f"\n[local pandoc fallback also failed] {fallback_err}"
        return (False, detail)

    def _fetch_log_tail(self, job_name: str, job_id: str, lines: int = 60) -> str:
        """Best-effort tail of the job's Slurm log (where pandoc/xelatex writes its real error).

        Fetched ONLY on failure, so a black-box "no output" turns into the actual LaTeX/singularity
        error. The path mirrors ``build_analysis_script``'s ``--output`` (``{log_dir}/{name}-%j``),
        using the resolved absolute scratch (``_resolved_scratch``) so it matches where Slurm actually
        wrote the log. Any failure here is swallowed — a diagnostic that itself errors must not mask
        the render failure it is trying to explain."""
        log_path = f"{self._resolved_scratch()}/{job_name}-{job_id}.log"
        try:
            r = self.remote.exec(f"tail -n {int(lines)} {log_path} 2>/dev/null")
        except Exception:  # noqa: BLE001 - diagnostics are best-effort
            return ""
        return (getattr(r, "out", "") or "").strip()
