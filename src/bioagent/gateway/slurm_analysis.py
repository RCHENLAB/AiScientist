"""Run the scanpy analysis line (QC / clustering / DE / enrichment + preflight) as **HPC3 Slurm
batch jobs**, reading the dataset on dfs3b in place.

Phase 4 of the HPC3 offload. The analysis tools in :mod:`bioagent.tools.scrna_pack` normally run
IN-PROCESS on the eyeserver (uncapped subprocess-free Python — a heavy PCA/Leiden/DE competes with
every other session for the one host's CPU+RAM). This executor instead submits each step as a
contained CPU batch job on HPC3 via :func:`bioagent.tools.scrna_cli.run_tool`, so the gateway host
stays a thin I/O layer and the real memory cap is Slurm's cgroup ``--mem``.

It reuses the proven lifecycle in :mod:`bioagent.gateway.slurm_job` (submit → wait with startup
retry → wait for completion → collect) and goes entirely through the ``RemoteExecutor`` protocol, so
the whole flow runs offline against a scripted fake in tests. If no live remote is wired (offline /
mock / HPC disabled), it FALLS BACK to running the same tool in-process, so a run never hard-fails
just because HPC is unavailable — and the existing local test suite exercises that path unchanged.

Because uploads already live on dfs3b (Phase 2), the dataset + the run's ``work/``+``artifacts/`` are
all on shared DFS and bind-mounted into the container — checkpoints accumulate in place across steps
with no round-trip. Only the small figures/tables are synced back to the local run dir so the report
bundler (still on the eyeserver) can assemble them.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agents.sandbox import sandbox_network_enabled
from .executor import RemoteExecutor
from .slurm_job import (
    AcquireConfig, JobCancelled, RunConfig, SlurmJobError, SlurmJobSpec, build_analysis_script,
    run_batch_job, singularity_exec, slurm_time_to_seconds)

_ARGS_EOF = "BIOAGENT_ANALYSIS_ARGS_EOF"
_RESULT_MARKER = "BIOAGENT_RESULT_JSON "


@dataclass
class SlurmAnalysisExecutor:
    """Runs one analysis step (``tool``) as a contained CPU batch job on HPC3.

    ``remote`` is the connected ``RemoteExecutor``. ``remote_workspace`` is the run's dir on dfs3b
    (holds ``work/`` checkpoints + ``artifacts/``); ``remote_dataset`` is the dataset path on dfs3b.
    ``local_workspace`` is the eyeserver run dir the small artifacts are synced back into.
    ``local_fallback(tool, args, ctx) -> dict`` runs the tool in-process when HPC isn't available.
    """

    remote: RemoteExecutor | None
    container_image: str
    remote_workspace: str | None = None
    remote_dataset: str | None = None
    local_workspace: Path | None = None
    # dfs3b dir holding the CURRENT bioagent source (contains ``bioagent/``), bind-mounted read-only
    # and put on PYTHONPATH so the job imports the live tools WITHOUT baking them into the image —
    # so editing a tool only needs a code sync, never an image rebuild. None → rely on the image.
    source_dir: str | None = None
    scratch_dir: str = "$HOME/.bioagent/analysis"
    entrypoint: str = "python -m bioagent.tools.scrna_cli"
    job_prefix: str = "bioagent_analysis"   # squeue job-name prefix (variant line overrides it)
    mem_gb: int = 64
    cpus: int = 8
    partition: str = "standard"
    account: str = ""
    time_limit: str = "01:00:00"
    container_module: str = ""
    container_bin: str = "singularity"
    startup_timeout_s: int = 600
    # 0 = AUTO: derive the job-wait from the SBATCH --time (`time_limit`) + a margin, so the gateway
    # waits as long as Slurm allows and never scancels a healthy, still-progressing job early. The old
    # fixed 1800s (30 min) silently killed a WGS VEP job that legitimately runs ~30-60 min under a 2h
    # --time (then it retried into the same wall and never finished). A completed job still returns the
    # instant it leaves the queue (see supervise_job) — this only raises the ceiling for a STUCK job.
    # Set a positive value to pin an explicit wait (e.g. tests).
    run_timeout_s: int = 0
    local_fallback: Callable[[str, dict, Any], dict] | None = None
    fallback_on_error: bool = True
    # Optional hook fired with the VERBATIM fallback reason (e.g. the Slurm-job error tail) the moment
    # a job degrades to the in-process fallback — so the gateway can log WHY, not just that it happened.
    on_fallback: Callable[[str], None] | None = None
    # Stop button: when this fires mid-job, the in-flight Slurm analysis is scancelled and the step
    # ends at once (no local fallback — that would defeat the Stop). Set to conn.chat_stop.is_set.
    should_cancel: Callable[[], bool] | None = None
    # Extra read-only bind mounts (absolute paths) added to EVERY job — e.g. the VEP cache + ClinVar
    # VCF for the offline variant line (variant_cli). Empty for the scanpy line (no behaviour change).
    extra_ro_binds: tuple[str, ...] = ()
    # Extra READ-WRITE bind mounts (absolute paths). A nested rw bind overrides a ro parent
    # bind for that subpath — e.g. the PaperQA index dir, whose answers index must write a
    # lockfile (paper-qa opens it for writing on every query).
    extra_rw_binds: tuple[str, ...] = ()
    # Deploy config injected as DEFAULT args (the caller's args override) — lets the gateway pass
    # paths/flags the tool needs but the model never sends, e.g. the VEP cache dir + fork width.
    inject_args: dict[str, Any] = field(default_factory=dict)
    # Gateway-AUTHORITATIVE args the model must NOT override (the reverse of inject_args, which the
    # caller overrides). For the variant line: the genome assembly detected from the VCF header, and
    # max_variants=0 (the offline path annotates the WHOLE WGS VCF). Without this a model that passes
    # assembly=GRCh38 on a GRCh37 file makes VEP fail on a cache-assembly mismatch, and a model that
    # passes max_variants=5000 silently truncates a 4.9M-variant WGS study to the first 5000 = a wrong
    # sub-cohort reported as the whole thing.
    force_args: dict[str, Any] = field(default_factory=dict)
    # Memoize a tool's OK result for the RUN: an identical (tool, args) call returns the stored result
    # instead of re-submitting the job and re-writing its artifacts. Guards the expensive, idempotent
    # variant annotation against a later step re-invoking annotate_variants — which both burned a fresh
    # ~45-min WGS VEP job AND clobbered the good result tables with a repeat (sometimes degraded) run.
    # Opt-in: the scanpy analysis executor leaves it False, so legitimately re-runnable steps are
    # unaffected. Keyed on the full args, so a genuine change (different genes / AF / assembly / VCF)
    # still re-runs.
    memoize_result: bool = False
    _counter: int = field(default=0, repr=False)
    _scratch: str | None = field(default=None, repr=False)

    def run_tool(self, tool: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if self.remote is None or not self.remote_workspace:
            return self._fallback(tool, args, ctx, "no live HPC connection")
        args = args or {}
        if self.memoize_result:                 # reuse this run's identical prior result, don't re-run
            hit = self._memo_load(tool, args)
            if hit is not None:
                return hit
        try:
            out = self._run_on_slurm(tool, args)
        except JobCancelled:
            return {"status": "cancelled", "error": "Run cancelled by the user."}
        except SlurmJobError as exc:
            if self.fallback_on_error:
                return self._fallback(tool, args, ctx, f"Slurm job failed: {exc}")
            return {"status": "error", "error": f"Slurm analysis job failed: {exc}"}
        if self.memoize_result and isinstance(out, dict) and out.get("status") == "ok":
            self._memo_store(tool, args, out)   # only a SUCCESSFUL run is cached (a failure still retries)
        return out

    # -- result memoization (opt-in; guards the ~45-min variant annotation from re-runs) ------------

    def _memo_path(self, tool: str, args: dict[str, Any]) -> "Path | None":
        """On-disk cache key for (tool, args) under the run's local workspace; None if we can't cache."""
        if not self.local_workspace:
            return None
        import hashlib
        import json
        sig = hashlib.sha1(
            json.dumps({"tool": tool, "args": args}, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        return Path(self.local_workspace) / ".tool_cache" / f"{tool}.{sig}.json"

    def _memo_load(self, tool: str, args: dict[str, Any]) -> "dict[str, Any] | None":
        """This run's stored OK result for an identical (tool, args) call, else None. A repeat
        annotate_variants in a later step thus REUSES the annotation instead of re-running the
        ~45-min VEP job and overwriting the tables."""
        import json
        p = self._memo_path(tool, args)
        if p is None or not p.exists():
            return None
        try:
            out = json.loads(p.read_text())
        except (OSError, ValueError):
            return None                          # unreadable cache → fall through and run for real
        if not isinstance(out, dict):
            return None
        prior = str(out.get("note", "")).strip()
        out["reused_existing"] = True
        out["note"] = ("Reused this run's existing annotation for identical inputs — the VEP job was "
                       "NOT re-run and the result tables were left intact. Do not re-annotate; read the "
                       "already-written tables." + (" " + prior if prior else ""))
        return out

    def _memo_store(self, tool: str, args: dict[str, Any], out: dict[str, Any]) -> None:
        import json
        p = self._memo_path(tool, args)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(out))
        except OSError:
            pass                                 # a cache write failure must never break the tool

    # -- internals ------------------------------------------------------------

    def _fallback(self, tool: str, args: dict, ctx: Any, reason: str) -> dict[str, Any]:
        if callable(self.on_fallback):
            try:
                self.on_fallback(reason)
            except Exception:  # noqa: BLE001 - logging must never break the fallback itself
                pass
        if callable(self.local_fallback):
            out = self.local_fallback(tool, args or {}, ctx)
            if isinstance(out, dict):
                out.setdefault("execution_mode", "local_fallback")
                out["fallback_reason"] = reason
            return out
        return {"status": "error",
                "error": f"analysis HPC execution unavailable ({reason}) and no local fallback is set."}

    def _resolved_scratch(self) -> str:
        """Expand shell vars (e.g. ``$HOME``) in ``scratch_dir`` to a concrete remote path ONCE.

        The dir is referenced both **unquoted** in shell (``mkdir``/heredoc — ``$HOME`` expands)
        AND **shlex-quoted** as the CLI ``--args`` path. In the quoted form the single quotes stop
        the shell from expanding ``$HOME``, so the Python tool receives a literal ``$HOME/...`` and
        fails with ``No such file or directory``. It also leaks into the ``singularity -B`` binds and
        ``#SBATCH --output`` (Slurm does not expand ``$HOME`` there either). Resolving to an absolute
        path up front keeps every use site correct and lets us quote paths safely everywhere. If the
        remote lookup can't fully expand it (offline/mock), we keep the literal so behaviour is
        unchanged rather than guessing."""
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

    def _run_on_slurm(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        caller = args or {}
        # precedence: deploy defaults (inject_args) < caller args < forced args (gateway-authoritative).
        args = {**self.inject_args, **caller, **self.force_args}
        for k, v in self.force_args.items():
            if k in caller and caller[k] != v:
                print(f"[{self.job_prefix}] forced {k}={v!r} over the model-supplied {caller[k]!r} "
                      f"(gateway-authoritative for the variant line — assembly is read from the VCF "
                      f"header; the offline path annotates the whole VCF).")
        self._counter += 1
        name = f"{self.job_prefix}_{tool}_{self._counter}"
        scratch = self._resolved_scratch()
        args_f = f"{scratch}/{name}.args.json"
        res_f = f"{scratch}/{name}.result.json"
        log_f = f"{scratch}/{name}.log"

        # Stage the args as a JSON file (quoted heredoc → no shell expansion of the payload).
        payload = json.dumps(args)
        write = self.remote.exec(
            f"mkdir -p {scratch} && cat > {args_f} <<'{_ARGS_EOF}'\n{payload}\n{_ARGS_EOF}")
        if not write.ok:
            raise SlurmJobError("failed to stage analysis args on the cluster", detail=write.stderr)

        ws = self.remote_workspace
        ds = self.remote_dataset or ""
        # Bind the live source read-only + put it on PYTHONPATH so `bioagent.tools.scrna_cli` is the
        # CURRENT code — no image rebuild on tool edits.
        pysrc_env = f"export PYTHONPATH={shlex.quote(self.source_dir)}:${{PYTHONPATH:-}}; " if self.source_dir else ""
        inner_payload = (
            f"{pysrc_env}export MPLBACKEND=Agg; "
            f"{self.entrypoint} --tool {shlex.quote(tool)} --workspace {shlex.quote(ws)} "
            f"--dataset {shlex.quote(ds)} --args {shlex.quote(args_f)} > {res_f} 2> {log_f}"
        )
        binds_ro = tuple(p for p in (ds, self.source_dir, *self.extra_ro_binds) if p)
        binds_rw = tuple(p for p in (ws, scratch, *self.extra_rw_binds) if p)
        inner = singularity_exec(
            self.container_image, inner_payload,
            binds_ro=binds_ro, binds_rw=binds_rw, nv=False, network=sandbox_network_enabled(),
            container_bin=self.container_bin)
        script = build_analysis_script(
            name, inner, partition=self.partition, cpus=self.cpus, mem_gb=self.mem_gb,
            time_limit=self.time_limit, account=self.account, gres="",  # CPU-only
            container_module=self.container_module, log_dir=scratch)
        # run_timeout_s == 0 → AUTO: wait as long as the SBATCH --time allows (+5 min margin), so a
        # healthy long job (e.g. WGS VEP) isn't scancelled early. Completion still returns immediately.
        run_timeout = self.run_timeout_s if self.run_timeout_s > 0 else slurm_time_to_seconds(self.time_limit) + 300
        spec = SlurmJobSpec(script=script, job_name=name, submit_dir=scratch)
        result = run_batch_job(
            self.remote, spec,
            acquire=AcquireConfig(startup_timeout_s=self.startup_timeout_s),
            run=RunConfig(run_timeout_s=run_timeout),
            should_cancel=self.should_cancel)

        out = self._collect(res_f, log_f, result)
        self._sync_artifacts_back()
        return out

    def _collect(self, res_f: str, log_f: str, result) -> dict[str, Any]:
        # Job output is file content — a transfer — so it is read over SFTP rather than by
        # spawning `cat` on a login node (RCIC: login nodes are not data-transfer nodes).
        def _text(path: str) -> str:
            return self.remote.read_bytes(path).decode("utf-8", errors="replace")

        raw = _text(res_f)
        for line in raw.splitlines():
            if line.startswith(_RESULT_MARKER):
                try:
                    out = json.loads(line[len(_RESULT_MARKER):])
                except json.JSONDecodeError:
                    break
                if isinstance(out, dict):
                    out["execution_mode"] = "hpc_slurm"
                    out["slurm_state"] = result.state
                    return out
        # No result marker → surface the REAL failure. The inner tool's stderr goes to log_f, but a
        # container / module / sbatch failure BEFORE the tool ever runs (e.g. a singularity "FATAL:
        # container creation failed" bind-mount error) lands ONLY in the SBATCH --output log
        # ({name}-{jobid}.log — see build_analysis_script) which log_f is NOT. Read BOTH, else those
        # failures degrade to a useless "produced no result" and the model flails blind.
        err = _text(log_f).strip()
        job_id = getattr(result, "job_id", "") or ""
        job_log = f"{log_f[:-4]}-{job_id}.log" if (log_f.endswith(".log") and job_id) else ""
        jerr = _text(job_log).strip() if job_log else ""
        detail = ("\n".join(t for t in (err, jerr) if t)).strip()
        hint = " (OUT_OF_MEMORY — raise --mem or downsample)" if (result.state or "").upper().startswith("OUT_OF_MEMORY") else ""
        return {"status": "error", "execution_mode": "hpc_slurm", "slurm_state": result.state,
                "error": (detail[-2000:].strip() or f"analysis job produced no result (Slurm state {result.state}){hint}")}

    def _sync_artifacts_back(self) -> None:
        """Best-effort mirror of the run's small artifacts (figures/tables) from dfs3b back to the
        local run dir, so the still-local report bundler can assemble them. Checkpoints (.h5ad) are
        left on dfs3b for the next step. No-op if there's no local target or nothing to copy."""
        if self.local_workspace is None or not self.remote_workspace:
            return
        remote_art = f"{self.remote_workspace}/artifacts"
        listing = self.remote.exec(f"find {shlex.quote(remote_art)} -type f 2>/dev/null").stdout or ""
        for remote_file in filter(None, (ln.strip() for ln in listing.splitlines())):
            rel = remote_file[len(remote_art):].lstrip("/")
            local_file = self.local_workspace / "artifacts" / rel
            try:
                local_file.parent.mkdir(parents=True, exist_ok=True)
                self.remote.get_file(remote_file, str(local_file))
            except OSError:
                continue
