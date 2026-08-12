"""Sandboxed executor for the research lab's ``run_code`` (CodeAct) tool.

Runs an LLM-generated Python snippet in an **isolated subprocess** — never in the
gateway process — in a throwaway temp working dir, with a wall-clock timeout and
(on Linux) CPU + address-space rlimits. On the HPC nodes, pass ``container_image``
to run the snippet INSIDE the container (``--containall`` + read-only data binds)
for real filesystem isolation; the same Singularity image already used to serve
vLLM works here too (RCIC HPC3 provides Singularity, not Apptainer).

**Network.** Contained code is allowed to reach the network by default
(:func:`sandbox_network_enabled`; Jin Li 2026-07-08 — prioritize effectiveness, so
tools/skills that must fetch references or hit REST APIs work). Set
``BIOAGENT_SANDBOX_NETWORK=0`` to restore full network isolation
(``--net --network none``).

This is the injectable ``CodeExecutor`` for ``ResearchLab(code_executor=...)``:
``CodeSandbox()(code) -> {"status","stdout","stderr",...}``.

Security note: the plain-subprocess path is **process isolation, not a full
security boundary** (the snippet can touch the filesystem, and — with network on —
the network, as the serving user). For untrusted code on a shared node, set
``container_image`` so it runs contained. The lab only ever sends the snippet *the
model wrote* — never raw private data — and the DataBoundaryGuard already gates
what reaches the model.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable


def sandbox_network_enabled() -> bool:
    """Whether contained code (``run_code`` + the HPC analysis tools) may reach the network.

    Default ON — Jin Li 2026-07-08: prioritize effectiveness, so tools/skills that need to fetch
    reference data or hit REST APIs (VEP, Ensembl, Europe PMC, downloads) actually work. Set
    ``BIOAGENT_SANDBOX_NETWORK`` to ``0``/``false``/``off`` to restore full network isolation
    (``singularity ... --net --network none``)."""
    return os.environ.get("BIOAGENT_SANDBOX_NETWORK", "1").strip().lower() not in {"0", "false", "off", "no", ""}

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover - non-POSIX
    resource = None  # type: ignore[assignment]


def _tail(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else "…(truncated)…\n" + text[-n:]


def describe_dataset_obs(dataset_path: str | None, *, max_cols: int = 40, max_levels: int = 12) -> str:
    """A compact, human-readable summary of the dataset's cell metadata (``obs``) — column names,
    dtypes, and the categorical levels — so the CodeAct model uses the EXACT column names/values
    that exist (e.g. ``sampleid`` with levels ``DDX41``/``WT``) instead of guessing a column named
    ``DDX41``. Read from a backed AnnData handle (obs only — X is never loaded), so it is cheap.
    Returns "" on any failure (no dataset, anndata not installed, unreadable file)."""
    if not dataset_path or not os.path.exists(dataset_path):
        return ""
    try:
        import anndata as ad
        import pandas as pd
    except Exception:  # pragma: no cover - analysis extra not installed
        return ""
    adata = None
    try:
        adata = ad.read_h5ad(dataset_path, backed="r")
        obs = adata.obs
        lines = [f"obs: {obs.shape[1]} columns over {adata.n_obs} cells (var: {adata.n_vars} genes)"]
        for col in list(obs.columns)[:max_cols]:
            series = obs[col]
            dtype = str(series.dtype)
            try:
                nunique = int(series.nunique(dropna=True))
            except Exception:
                nunique = None
            if dtype in ("category", "object", "bool") or (nunique is not None and nunique <= max_levels):
                try:
                    levels = [str(v) for v in list(pd.unique(series))[:max_levels]]
                    more = " …" if (nunique or 0) > len(levels) else ""
                    lines.append(f"  - {col} [{dtype}]: {', '.join(levels)}{more}")
                except Exception:
                    lines.append(f"  - {col} [{dtype}]")
            else:
                lines.append(f"  - {col} [{dtype}] (continuous)")
        return "\n".join(lines)
    except Exception:
        return ""
    finally:
        try:
            if adata is not None and getattr(adata, "isbacked", False) and adata.file is not None:
                adata.file.close()
        except Exception:
            pass


def build_run_code_context(executor: object) -> str:
    """Build the live 'execution environment' guidance appended to the ``run_code`` tool
    description, so the CodeAct model stops guessing paths / column names / working directory.

    ``executor`` is duck-typed: any object exposing ``dataset_path`` / ``work_dir`` /
    ``artifacts_dir`` (i.e. a ``CodeSandbox``) contributes context; anything else (or ``None``)
    yields "". Everything here is STATIC for the run, so it is computed once at catalog-build time.
    """
    dataset = getattr(executor, "dataset_path", None)
    work = getattr(executor, "work_dir", None)
    artifacts = getattr(executor, "artifacts_dir", None)
    uploads = getattr(executor, "uploads_dir", None)
    mem_mb = getattr(executor, "mem_mb", None)
    if not any((dataset, work, artifacts, uploads)):
        return ""
    parts = [
        "",
        "EXECUTION ENVIRONMENT — read before writing code:",
        "- The working directory (CWD) is a FRESH, EMPTY throwaway temp dir. Relative paths like "
        "sc.read_h5ad('adata_clustered.h5ad') WILL fail with FileNotFoundError. ALWAYS build paths "
        "from the env vars below (os.environ / os.path.join).",
    ]
    if dataset:
        parts.append(f"- BIOAGENT_DATASET = {dataset}  (raw input dataset)")
    if work:
        parts.append(
            f"- BIOAGENT_WORK = {work}  (pipeline checkpoints: adata_qc.h5ad after QC, "
            "adata_clustered.h5ad after clustering, adata_de.h5ad after DE — clustering adds a "
            "'leiden' obs column. Call os.listdir(os.environ['BIOAGENT_WORK']) to see what exists "
            "NOW instead of assuming a file is already there.)"
        )
    if artifacts:
        parts.append(
            f"- BIOAGENT_ARTIFACTS = {artifacts}  (write NEW figures under figures/, tables under tables/)"
        )
    if uploads:
        parts.append(
            f"- BIOAGENT_UPLOADS = {uploads}  (EVERY file/folder the user uploaded this session, "
            "with nested subfolders preserved. Use os.walk(os.environ['BIOAGENT_UPLOADS']) to "
            "discover all inputs — incl. folders added after the primary dataset.)"
        )
    if mem_mb:
        parts.append(
            f"- Memory is capped (~{int(mem_mb)} MB). Do NOT load the full AnnData and .copy() large "
            "subsets in a loop — subset with a view, drop intermediates, or aggregate. An "
            "over-budget snippet is killed with SIGKILL (returncode -9)."
        )
    schema = describe_dataset_obs(dataset)
    if schema:
        parts.append(
            "- Cell metadata — use these EXACT obs column names + values, do NOT invent a column "
            "(e.g. group a DDX41-vs-WT contrast on the real 'sampleid' column, not a 'DDX41' column):"
        )
        parts.append("\n".join("  " + ln for ln in schema.splitlines()))
    return "\n".join(parts)


@dataclass
class CodeSandbox:
    """A callable ``CodeExecutor``. Default = restricted subprocess; set
    ``container_image`` for container isolation on the HPC nodes."""

    timeout_s: float = 180.0   # CodeAct now does real data work (read the dataset, compute) — give it room
    mem_mb: int = 2048
    python_bin: str = sys.executable
    container_image: str | None = None     # set on HPC nodes for real isolation
    container_bin: str = "singularity"     # RCIC HPC3 runtime (NOT apptainer)
    # Network access for contained code. Default from BIOAGENT_SANDBOX_NETWORK (ON) — see
    # sandbox_network_enabled(); an explicit True/False still overrides per-instance.
    allow_network: bool = field(default_factory=sandbox_network_enabled)
    max_output: int = 20_000
    # Per-run data access (set by the gateway): the snippet sees these as the env vars
    # BIOAGENT_DATASET / BIOAGENT_WORK / BIOAGENT_ARTIFACTS so CodeAct can read the
    # dataset + the run's checkpoints/outputs and write new artifacts.
    dataset_path: str | None = None
    work_dir: str | None = None
    artifacts_dir: str | None = None
    # The whole per-user uploads tree (all files/folders the user uploaded this session).
    # Exposed as BIOAGENT_UPLOADS so run_code can reach EVERY upload — incl. folders added
    # after the run's primary dataset — just like a general file workspace.
    uploads_dir: str | None = None
    # Stop button: polled while the snippet runs so a Stop KILLS the in-flight subprocess (and
    # its whole process group) within ~a second, instead of the old blocking subprocess.run that
    # ignored Stop until the 180s timeout. Set to conn.chat_stop.is_set.
    should_cancel: Callable[[], bool] | None = None

    def __call__(self, code: str) -> dict[str, object]:
        return self.run(code)

    def run(self, code: str) -> dict[str, object]:
        code = (code or "").strip()
        if not code:
            return {"status": "error", "error": "no code provided"}
        with tempfile.TemporaryDirectory(prefix="bioagent-code-") as workdir:
            script = os.path.join(workdir, "snippet.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(code)
            cmd, preexec = self._command(script, workdir)
            try:
                # start_new_session so the child is a process-group leader we can kill WHOLE
                # (snippet + any grandchildren it spawns), not just the direct process.
                proc = subprocess.Popen(  # noqa: S603 - intentional sandboxed exec of model code
                    cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, preexec_fn=preexec, env=self._env(), start_new_session=True,
                )
            except FileNotFoundError as exc:
                return {"status": "error", "error": f"sandbox runner not found: {exc}"}

            deadline = time.monotonic() + self.timeout_s
            poll = 0.4
            while True:
                try:
                    stdout, stderr = proc.communicate(timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    if self.should_cancel is not None and self.should_cancel():
                        self._killpg(proc)
                        so, se = proc.communicate()
                        return {"status": "cancelled", "error": "Run cancelled by the user.",
                                "returncode": None,
                                "stdout": _tail(so, self.max_output), "stderr": _tail(se, self.max_output)}
                    if time.monotonic() >= deadline:
                        self._killpg(proc)
                        so, se = proc.communicate()
                        return {"status": "timeout",
                                "error": f"code exceeded the {self.timeout_s:g}s time limit",
                                "stdout": _tail(so, self.max_output), "stderr": _tail(se, self.max_output)}

            status = "ok" if proc.returncode == 0 else "error"
            out: dict[str, object] = {
                "status": status,
                "returncode": proc.returncode,
                "stdout": _tail(stdout, self.max_output),
                "stderr": _tail(stderr, self.max_output),
            }
            if status == "error":
                out["error"] = (_tail(stderr, 2000).strip() or f"exited with code {proc.returncode}")
            return out

    @staticmethod
    def _killpg(proc: "subprocess.Popen") -> None:
        """SIGKILL the child's whole process group (falls back to the single process)."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    # -- internals ------------------------------------------------------------

    def _data_paths(self) -> list[str]:
        """Existing host paths the snippet needs read access to (dataset + run dirs)."""
        out = []
        for p in (self.dataset_path, self.work_dir, self.artifacts_dir, self.uploads_dir):
            if p and os.path.exists(p):
                out.append(p)
        return out

    def _command(self, script: str, workdir: str):
        if self.container_image:
            net = [] if self.allow_network else ["--net", "--network", "none"]
            binds = ["-B", f"{workdir}:{workdir}"]
            for p in self._data_paths():            # mount the dataset + run dirs read/write
                binds += ["-B", f"{p}:{p}"]
            cmd = [
                self.container_bin, "exec", "--containall", "--writable-tmpfs", *net,
                *binds, self.container_image, "python", script,
            ]
            return cmd, None  # isolation is handled by the container
        return [self.python_bin, script], self._limits

    def _limits(self) -> None:  # preexec_fn (POSIX child, after fork)
        if resource is None:
            return
        cpu = max(1, int(self.timeout_s) + 1)
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        except (ValueError, OSError):
            pass
        # NOTE: we deliberately do NOT set RLIMIT_AS. The scientific stack
        # (numpy/scipy/scanpy/anndata + BLAS/OpenMP) reserves huge *virtual* address
        # space that has little to do with resident memory, so an AS cap kills
        # `import scanpy` outright. Real memory isolation comes from the container
        # path (Singularity --memory / cgroups), not from rlimits on this process.

    def _env(self) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("TMPDIR", tempfile.gettempdir()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # Carry caches/locale so scanpy/anndata/etc. can find data if installed.
        for key in ("HF_HOME", "TMPDIR", "LANG", "LC_ALL", "MPLBACKEND"):
            if key in os.environ:
                env[key] = os.environ[key]
        env.setdefault("MPLBACKEND", "Agg")  # never try to open a display
        # matplotlib needs a WRITABLE config/cache dir to build its font cache. The
        # default ($HOME/.config + $HOME/.cache) is often read-only inside the Singularity
        # container — that surfaced as "Permission denied creating matplotlib cache
        # directories" and killed every plotting snippet. Pin both to a run-owned writable
        # dir so scanpy/matplotlib figures render. (XDG_CACHE_HOME also covers fontconfig.)
        cache_base = self.work_dir or self.artifacts_dir or env["HOME"]
        mpl_dir = os.path.join(str(cache_base), ".mplconfig")
        try:
            os.makedirs(mpl_dir, exist_ok=True)
        except OSError:
            mpl_dir = env["HOME"]
        env["MPLCONFIGDIR"] = mpl_dir
        env.setdefault("XDG_CACHE_HOME", os.path.join(str(cache_base), ".cache"))
        # Expose the run's data to CodeAct so a snippet can read the dataset/checkpoints
        # and write new outputs (raw data still never enters the LLM prompt — only the
        # model-written code reads these paths).
        if self.dataset_path:
            env["BIOAGENT_DATASET"] = str(self.dataset_path)
        if self.work_dir:
            env["BIOAGENT_WORK"] = str(self.work_dir)
        if self.artifacts_dir:
            env["BIOAGENT_ARTIFACTS"] = str(self.artifacts_dir)
        if self.uploads_dir:
            env["BIOAGENT_UPLOADS"] = str(self.uploads_dir)
        return env
