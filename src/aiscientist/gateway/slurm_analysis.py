"""Run the scanpy analysis line (QC / clustering / DE / enrichment + preflight) as **HPC3 Slurm
batch jobs**, reading the dataset on dfs3b in place.

Phase 4 of the HPC3 offload. The analysis tools in :mod:`aiscientist.tools.scrna_pack` normally run
IN-PROCESS on the eyeserver (uncapped subprocess-free Python — a heavy PCA/Leiden/DE competes with
every other session for the one host's CPU+RAM). This executor instead submits each step as a
contained CPU batch job on HPC3 via :func:`aiscientist.tools.scrna_cli.run_tool`, so the gateway host
stays a thin I/O layer and the real memory cap is Slurm's cgroup ``--mem``.

It reuses the proven lifecycle in :mod:`aiscientist.gateway.slurm_job` (submit → wait with startup
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
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agents.sandbox import sandbox_network_enabled
from ..tools.api import ANALYSIS_ENTRYPOINT
from .executor import RemoteExecutor
from .slurm_job import (
    AcquireConfig, JobCancelled, RunConfig, SlurmJobError, SlurmJobSpec, build_analysis_script,
    run_batch_job, singularity_exec, slurm_time_to_seconds)

_ARGS_EOF = "AISCIENTIST_ANALYSIS_ARGS_EOF"
_RESULT_MARKER = "AISCIENTIST_RESULT_JSON "
# The marker before the 2026-09-30 rename: a job submitted by the previous deploy prints this one.
_LEGACY_RESULT_MARKER = "BIOAGENT_RESULT_JSON "


def provision_image(remote: Any, ref: str, images_dir: str, *, scratch: str, job_name: str,
                    partition: str, account: str = "", container_module: str = "",
                    container_bin: str = "singularity", startup_timeout_s: int = 600,
                    should_cancel: "Callable[[], bool] | None" = None, pkgs_cache: str = "",
                    say: "Callable[[str, str], None] | None" = None) -> str:
    """The dfs3b path of image ``ref`` in ``images_dir``, provisioned first if it is not there.

    One image per recipe, shared by every run and every lab member: a tool's TOOL.md or a skill's
    SKILL.md names the image, and the first job that needs it pulls it (``docker://``) or builds it
    (``bioconda:``) in a Slurm job on a compute node (RCIC bans downloads on login nodes). A changed
    recipe is a new image, so two environments never share installed packages."""
    from .tool_images import image_sif_name, is_build_ref, provision_command, provision_resources
    notify = say or (lambda _level, _msg: None)
    sif = f"{images_dir.rstrip('/')}/{image_sif_name(ref)}"
    if remote.exec(f"test -s {shlex.quote(sif)}").ok:
        return sif
    verb = "building" if is_build_ref(ref) else "pulling"
    notify("info", f"First use of {ref}: {verb} it into the shared containers directory on HPC3. "
                   "This happens once; every later run reuses it.")
    made = remote.exec(f"mkdir -p {scratch} {shlex.quote(images_dir)}")
    if not made.ok:
        raise SlurmJobError("could not create the image directories on HPC3", detail=made.stderr)
    cpus, mem_gb, time_limit = provision_resources(ref)
    script = build_analysis_script(
        job_name, provision_command(ref, sif, container_bin, pkgs_cache=pkgs_cache),
        partition=partition, cpus=cpus, mem_gb=mem_gb, time_limit=time_limit, account=account,
        container_module=container_module, log_dir=scratch)
    result = run_batch_job(
        remote, SlurmJobSpec(script=script, job_name=job_name, submit_dir=scratch),
        acquire=AcquireConfig(startup_timeout_s=startup_timeout_s),
        run=RunConfig(run_timeout_s=slurm_time_to_seconds(time_limit) + 300),
        should_cancel=should_cancel)
    if not remote.exec(f"test -s {shlex.quote(sif)}").ok:
        job_id = getattr(result, "job_id", "") or ""
        log = f"{scratch}/{job_name}-{job_id}.log"
        try:
            tail = remote.read_bytes(log).decode("utf-8", errors="replace")[-1500:]
        except Exception:  # noqa: BLE001 - the log is a diagnostic, not a requirement
            tail = ""
        raise SlurmJobError(f"could not provision {ref} on HPC3 (Slurm state {result.state})",
                            job_id=job_id, detail=tail)
    notify("success", f"{ref} is ready on HPC3.")
    return sif


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
    # dfs3b dir holding the CURRENT AiScientist source (contains ``aiscientist/``), bind-mounted read-only
    # and put on PYTHONPATH so the job imports the live tools WITHOUT baking them into the image —
    # so editing a tool only needs a code sync, never an image rebuild. None → rely on the image.
    source_dir: str | None = None
    # dfs3b dir holding EXTRA pure-Python deps the image lacks (e.g. pydeseq2), installed with
    # `pip install --no-deps --target` so nothing in it shadows the image's numpy/scipy stack.
    # Appended to PYTHONPATH after `source_dir` and bind-mounted read-only. None -> the tools'
    # own loud fallbacks cover the gap.
    deps_dir: str | None = None
    scratch_dir: str = "$HOME/.bioagent/analysis"
    entrypoint: str = f"python -m {ANALYSIS_ENTRYPOINT}"
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
    # After a FAILED Slurm job, run the tool in-process on the gateway host? Default OFF: the
    # eyeserver is a thin I/O layer with nowhere near the memory for scanpy on a real object, and
    # a silent fallback there both starves the host and hides the Slurm failure the model should
    # be reacting to. The error now carries the Slurm reason so the model can retry ON HPC3. Set
    # AISCIENTIST_HPC_LOCAL_FALLBACK=1 to restore the old behaviour (dev boxes without HPC3). The
    # "no live HPC connection" branch (mock / offline dev) is a separate path and unchanged.
    fallback_on_error: bool = field(default_factory=lambda: os.environ.get(
        "AISCIENTIST_HPC_LOCAL_FALLBACK", "").strip().lower() in ("1", "true", "yes"))
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
    # Deploy config that can CHANGE during a run, evaluated at every job and layered over
    # inject_args. The LLM endpoint is one: the vLLM serve job hits its time limit mid-run and comes
    # back on another node and port, and a URL captured when the executor was built then points at
    # nothing (run f3731e0b7136: every deep_literature job after the 05:20 swap got
    # "Connection error" from the dead m54-02:49336 while vLLM served on m54-01:39783).
    live_args: Callable[[], dict[str, Any]] | None = None
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
    # When a tool reports a DECLARED dependency missing (tools.run_deps.ALLOWED), install it into
    # this run's own workspace (`<remote_workspace>/_deps`) as a Slurm job, then retry the tool once.
    # Off by default: only the scanpy line's tools declare dependencies, and the gateway turns it on
    # for that executor (AISCIENTIST_AUTO_INSTALL_DEPS=0 turns it off). `release_run_deps` removes the
    # install when the run is published.
    auto_install_deps: bool = False
    # Progress lines for the console, e.g. "installing scikit-image for this run": (level, message).
    notify: Callable[[str, str], None] | None = None
    # Shared dfs3b dir for the container images tools declare in TOOL.md (``image:``): a tool's image
    # is pulled here on its first use, by any run, and reused after (see gateway/tool_images.py).
    # None -> a tool that declares an image cannot run on HPC3 and says so.
    images_dir: str | None = None
    # The lab's shared conda download cache on dfs3b: a ``bioconda:`` image build reads packages an
    # earlier build already downloaded instead of fetching them again (see tool_images.build_command).
    pkgs_cache_dir: str = ""
    # A FOLDER dataset's root on dfs3b (e.g. a cohort of Cell Ranger libraries). Bound read-only and
    # handed to the job as ``--dataset-root``, so a tool can read the whole tree, not only the one
    # primary file ``remote_dataset`` names. None for a single-file dataset.
    dataset_root: str | None = None
    _images: dict[str, str] = field(default_factory=dict, repr=False)
    _counter: int = field(default=0, repr=False)
    _scratch: str | None = field(default=None, repr=False)
    _run_deps: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _deps_failed: dict[str, str] = field(default_factory=dict, repr=False)
    _deps_attempted: bool = field(default=False, repr=False)

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
            out = self._install_and_retry(tool, args, out)
        except JobCancelled:
            return {"status": "cancelled", "error": "Run cancelled by the user."}
        except SlurmJobError as exc:
            if self.fallback_on_error:
                return self._fallback(tool, args, ctx, f"Slurm job failed: {exc}")
            if callable(self.on_fallback):
                try:
                    self.on_fallback(f"Slurm job failed (no local fallback): {exc}")
                except Exception:  # noqa: BLE001
                    pass
            return {"status": "error", "execution_mode": "hpc_slurm",
                    "error": (f"HPC3 analysis job failed: {exc}. Not run on the gateway host "
                              "(local fallback is disabled). Fix the cause and call the tool again "
                              "— it will be resubmitted to HPC3.")}
        if self.memoize_result and isinstance(out, dict) and out.get("status") == "ok":
            self._memo_store(tool, args, out)   # only a SUCCESSFUL run is cached (a failure still retries)
        return out

    # -- a tool's declared dependency, installed for this run only ------------------------------

    def _say(self, level: str, message: str) -> None:
        if callable(self.notify):
            try:
                self.notify(level, message)
            except Exception:  # noqa: BLE001 - a console line must never break the step
                pass

    def _install_and_retry(self, tool: str, args: dict[str, Any], out: Any) -> Any:
        """If ``out`` says a DECLARED dependency is missing, install it for this run and run the tool
        again, once. Anything else — an undeclared name, the feature off — is returned unchanged."""
        if not (self.auto_install_deps and isinstance(out, dict)
                and out.get("status") == "dependency_missing"):
            return out
        from ..tools.api import resolve_run_dependency as resolve
        from ..tools.api import INSTALL_DEPENDENCY
        dep = str(out.get("dependency") or "").strip()
        entry = resolve(dep)
        if entry is None:
            return out
        requirement = entry[0]
        if dep in self._deps_failed:
            return {**out, "dependency_install": {"status": "failed", "package": requirement,
                                                  "reason": self._deps_failed[dep]}}
        if dep not in self._run_deps:
            self._deps_attempted = True
            self._say("info", f"{tool} needs {requirement}, which the analysis image lacks — "
                              "installing it on HPC3 for this run only.")
            try:
                inst = self._run_on_slurm(INSTALL_DEPENDENCY, {"dependency": dep})
            except SlurmJobError as exc:        # the install job never ran: keep the tool's own answer
                inst = {"status": "error", "error": f"the install job failed: {exc}"}
            if inst.get("status") not in ("ok", "already_available"):
                reason = str(inst.get("error") or inst.get("status") or "unknown")[:600]
                self._deps_failed[dep] = reason
                self._say("warning", f"Could not install {requirement} for this run: {reason[:200]}")
                return {**out, "dependency_install": {"status": "failed", "package": requirement,
                                                      "reason": reason}}
            self._run_deps[dep] = {"path": inst["path"], "site": inst["site"],
                                   "installed": inst.get("installed") or requirement}
            self._say("success", f"Installed {requirement} for this run; running {tool} again.")
        retry = self._run_on_slurm(tool, args)
        if isinstance(retry, dict):
            retry["dependency_installed"] = {
                "package": self._run_deps[dep]["installed"],
                "scope": "installed for this run only; removed when the run is published"}
        return retry

    def _deps_env(self) -> str:
        """Shell that puts this run's installed packages on the tool job's path — APPENDED after the
        image's site-packages by the generated sitecustomize, whose dir goes first on PYTHONPATH."""
        if not self._run_deps:
            return ""
        dirs = ":".join(dict.fromkeys(d["path"] for d in self._run_deps.values()))
        site = next(iter(self._run_deps.values()))["site"]
        return (f"export AISCIENTIST_PKG_CACHE={shlex.quote(dirs)}; "
                f"export PYTHONPATH={shlex.quote(site)}:${{PYTHONPATH:-}}; ")

    def release_run_deps(self) -> list[str]:
        """Delete this run's installed packages from HPC3 (the whole ``<workspace>/_deps``, including
        a half-finished install). Returns the requirements that had been installed. A no-op for a
        run that never tried to install anything."""
        if not self._deps_attempted or self.remote is None or not self.remote_workspace:
            return []
        from ..tools.api import DEPS_DIRNAME
        installed = [d["installed"] for d in self._run_deps.values()]
        self.remote.exec(f"rm -rf {shlex.quote(self.remote_workspace.rstrip('/') + '/' + DEPS_DIRNAME)}")
        self._run_deps.clear()
        self._deps_attempted = False
        return installed

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

    # -- a tool's own container and resources (TOOL.md image / cpus / mem_gb / time_limit) --------

    def _job_settings(self, tool: str) -> dict[str, Any]:
        """The image and Slurm resources ``tool``'s job runs with: what its TOOL.md declares, else
        this line's defaults. A declared image is pulled on HPC3 first if it is not there yet."""
        from ..tools import catalog
        m = catalog.manifest(tool)
        image = self.container_image
        if m is not None and m.image:
            image = self._ensure_image(m.image)
        return {
            "image": image,
            "own_image": bool(m is not None and m.image),
            "cpus": (m.cpus if m is not None and m.cpus else self.cpus),
            "mem_gb": (m.mem_gb if m is not None and m.mem_gb else self.mem_gb),
            "time_limit": (m.time_limit if m is not None and m.time_limit else self.time_limit),
        }

    def _ensure_image(self, ref: str) -> str:
        """The dfs3b path of image ``ref``, provisioned (pulled, or built from a ``bioconda:`` recipe)
        in a Slurm job the first time any run needs it."""
        if ref in self._images:
            return self._images[ref]
        if not self.images_dir:
            raise SlurmJobError(f"this tool runs in {ref}, but no shared containers directory is "
                                "configured for HPC3 images")
        self._counter += 1
        sif = provision_image(
            self.remote, ref, self.images_dir, scratch=self._resolved_scratch(),
            job_name=f"{self.job_prefix}_image_{self._counter}", partition=self.partition,
            account=self.account, container_module=self.container_module,
            container_bin=self.container_bin, startup_timeout_s=self.startup_timeout_s,
            should_cancel=self.should_cancel, pkgs_cache=self.pkgs_cache_dir, say=self._say)
        self._images[ref] = sif
        return sif

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
        live: dict[str, Any] = {}
        if callable(self.live_args):
            try:
                live = dict(self.live_args() or {})
            except Exception:  # noqa: BLE001 - fall back to the values captured at build time
                live = {}
        args = {**self.inject_args, **live, **caller, **self.force_args}
        for k, v in self.force_args.items():
            if k in caller and caller[k] != v:
                print(f"[{self.job_prefix}] forced {k}={v!r} over the model-supplied {caller[k]!r} "
                      f"(gateway-authoritative for the variant line — assembly is read from the VCF "
                      f"header; the offline path annotates the whole VCF).")
        job = self._job_settings(tool)          # may pull the tool's image first (once, ever)
        self._counter += 1
        name = f"{self.job_prefix}_{tool}_{self._counter}"
        scratch = self._resolved_scratch()
        args_f = f"{scratch}/{name}.args.json"
        res_f = f"{scratch}/{name}.result.json"
        log_f = f"{scratch}/{name}.log"

        # Stage the args as a JSON file (quoted heredoc → no shell expansion of the payload).
        payload = json.dumps(args)
        # Create the WORKSPACE too, not only scratch. Singularity refuses to bind-mount a source
        # that does not exist ("mount source … doesn't exist" → FATAL, exit 127), and the paperqa
        # executor's workspace (`Temp/<user>/paperqa`) was never created by anything: every
        # deep_literature job on HPC3 — 28 of 28 across two users since the feature shipped — died
        # in one second, and each literature step silently fell back to Europe PMC keyword search.
        # The scrna path happened to survive because the gateway mkdir'd its workspace elsewhere.
        ws_mk = f"mkdir -p {shlex.quote(self.remote_workspace)} && " if self.remote_workspace else ""
        write = self.remote.exec(
            f"{ws_mk}mkdir -p {scratch} && cat > {args_f} <<'{_ARGS_EOF}'\n{payload}\n{_ARGS_EOF}")
        if not write.ok:
            raise SlurmJobError("failed to stage analysis args on the cluster", detail=write.stderr)

        ws = self.remote_workspace
        ds = self.remote_dataset or ""
        root = self.dataset_root or ""
        # Bind the live source read-only + put it on PYTHONPATH so `aiscientist.tools.scrna_cli` is the
        # CURRENT code — no image rebuild on tool edits. ``deps_dir`` and the run's installed deps are
        # built for analysis.sif's Python; a tool in its OWN image gets neither, or they could shadow
        # that image's packages.
        deps_dir = None if job["own_image"] else self.deps_dir
        pypath = ":".join(p for p in (self.source_dir, deps_dir) if p)
        pysrc_env = f"export PYTHONPATH={shlex.quote(pypath)}:${{PYTHONPATH:-}}; " if pypath else ""
        deps_env = "" if job["own_image"] else self._deps_env()
        # The job's CPU count, for a tool that runs a multi-threaded pipeline of its own.
        cpus_env = f"export AISCIENTIST_JOB_CPUS={int(job['cpus'])}; "
        root_arg = f" --dataset-root {shlex.quote(root)}" if root else ""
        inner_payload = (
            f"{pysrc_env}{deps_env}{cpus_env}export MPLBACKEND=Agg; "
            f"{self.entrypoint} --tool {shlex.quote(tool)} --workspace {shlex.quote(ws)} "
            f"--dataset {shlex.quote(ds)}{root_arg} --args {shlex.quote(args_f)} > {res_f} 2> {log_f}"
        )
        # Bind the dataset's PARENT DIRECTORY, never the bare file: a file bind FATALs with
        # "destination ... doesn't exist in container" when that path is absent from the image
        # (scgpt_job binds dirname() for the same reason). And when the dataset already lives
        # under the rw-bound workspace, skip the extra bind entirely - the workspace bind
        # already exposes it, and a duplicate ro/rw bind of the same tree can conflict.
        def _outside_ws(path: str) -> str:
            if path and ws and (path.rstrip("/") + "/").startswith(ws.rstrip("/") + "/"):
                return ""
            return path

        ds_dir = _outside_ws(str(Path(ds).parent) if ds else "")
        root_dir = _outside_ws(root)
        if root_dir and ds_dir and (ds_dir.rstrip("/") + "/").startswith(root_dir.rstrip("/") + "/"):
            ds_dir = ""                          # the folder bind already exposes the primary file
        binds_ro = tuple(p for p in (root_dir, ds_dir, self.source_dir, deps_dir,
                                     *self.extra_ro_binds) if p)
        binds_rw = tuple(p for p in (ws, scratch, *self.extra_rw_binds) if p)
        inner = singularity_exec(
            job["image"], inner_payload,
            binds_ro=binds_ro, binds_rw=binds_rw, nv=False, network=sandbox_network_enabled(),
            container_bin=self.container_bin)
        script = build_analysis_script(
            name, inner, partition=self.partition, cpus=job["cpus"], mem_gb=job["mem_gb"],
            time_limit=job["time_limit"], account=self.account, gres="",  # CPU-only
            container_module=self.container_module, log_dir=scratch)
        # run_timeout_s == 0 → AUTO: wait as long as the SBATCH --time allows (+5 min margin), so a
        # healthy long job (e.g. WGS VEP) isn't scancelled early. Completion still returns immediately.
        run_timeout = (self.run_timeout_s if self.run_timeout_s > 0
                       else slurm_time_to_seconds(job["time_limit"]) + 300)
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
            marker = next((m for m in (_RESULT_MARKER, _LEGACY_RESULT_MARKER) if line.startswith(m)), None)
            if marker:
                try:
                    out = json.loads(line[len(marker):])
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
