"""Sandboxed executor for the research lab's ``run_code`` (CodeAct) tool.

Runs an LLM-generated Python snippet in an **isolated subprocess** — never in the
gateway process — in a throwaway temp working dir, with a wall-clock timeout and
(on Linux) CPU + address-space rlimits. On the HPC nodes, pass ``container_image``
to run the snippet INSIDE the container (``--containall`` + read-only data binds)
for real filesystem isolation; the same Apptainer runtime already used to serve
vLLM works here too.

**Network.** Contained code is allowed to reach the network by default
(:func:`sandbox_network_enabled`; Jin Li 2026-07-08 — prioritize effectiveness, so
tools/skills that must fetch references or hit REST APIs work). Set
``AISCIENTIST_SANDBOX_NETWORK=0`` to restore full network isolation
(``--net --network none``).

This is the injectable ``CodeExecutor`` for ``ResearchLab(code_executor=...)``:
``CodeSandbox()(code) -> {"status","stdout","stderr",...}``.

Security note: the plain-subprocess path is **process isolation, not a full
security boundary** (the snippet can touch the filesystem, and — with network on —
the network, as the serving user). For untrusted code on a shared node, set
``container_image`` so it runs contained. The lab only ever sends the snippet *the
model wrote* — never raw private data — and the DataBoundaryGuard already gates
what reaches the model.

That is why the gateway builds this sandbox only on a console without accounts
(``gateway.app._host_code_allowed``). Behind accounts the serving user holds every
account's saved keys, so there ``run_code`` runs on HPC3 or not at all.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable


def sandbox_network_enabled() -> bool:
    """Whether contained code (``run_code`` + the HPC analysis tools) may reach the network.

    Default ON — Jin Li 2026-07-08: prioritize effectiveness, so tools/skills that need to fetch
    reference data or hit REST APIs (VEP, Ensembl, Europe PMC, downloads) actually work. Set
    ``AISCIENTIST_SANDBOX_NETWORK`` to ``0``/``false``/``off`` to restore full network isolation
    (``apptainer ... --net --network none``)."""
    return os.environ.get("AISCIENTIST_SANDBOX_NETWORK", "1").strip().lower() not in {"0", "false", "off", "no", ""}

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


REFERENCE_ROLE = "reference"


def input_env(inputs: "Any") -> dict[str, str]:
    """The env vars that hand a snippet the run's OTHER bound files (feature ②), as the executor
    sees them: ``AISCIENTIST_INPUTS`` = a JSON list of ``{name, role, path}``, and
    ``AISCIENTIST_REFERENCE`` = the first file the user marked as the reference. One definition for
    the local sandbox and the HPC3 executor, so a snippet reads the same names on either."""
    rows = [{"name": str(i.get("name") or ""), "role": i.get("role") or None, "path": str(i["path"])}
            for i in (inputs or ()) if isinstance(i, dict) and i.get("path")]
    if not rows:
        return {}
    env = {"AISCIENTIST_INPUTS": json.dumps(rows, ensure_ascii=False)}
    ref = next((r["path"] for r in rows if r["role"] == REFERENCE_ROLE), None)
    if ref:
        env["AISCIENTIST_REFERENCE"] = ref
    return env


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
    root = getattr(executor, "dataset_root", None)
    inputs = [i for i in (getattr(executor, "inputs", None) or ()) if isinstance(i, dict) and i.get("path")]
    mem_mb = getattr(executor, "mem_mb", None)
    if not any((dataset, work, artifacts, uploads, root, inputs)):
        return ""
    parts = [
        "",
        "EXECUTION ENVIRONMENT — read before writing code:",
        "- The working directory (CWD) is a FRESH, EMPTY throwaway temp dir. Relative paths like "
        "sc.read_h5ad('adata_clustered.h5ad') WILL fail with FileNotFoundError. ALWAYS build paths "
        "from the env vars below (os.environ / os.path.join).",
    ]
    if dataset:
        parts.append(f"- AISCIENTIST_DATASET = {dataset}  (raw input dataset)")
    if dataset and str(dataset).lower().endswith(".h5"):
        # Run f3b8268c4fd4 hand-parsed a Cell Ranger .h5 with h5py: 6 of 11 snippets failed on a
        # guessed v2 layout and on the matrix orientation, before any number was computed.
        parts.append(
            "- A .h5 with a top-level 'matrix' group (or one group per genome) is a 10x Cell Ranger "
            "feature-barcode matrix, NOT an .h5ad: read it with sc.read_10x_h5(path) -> AnnData of "
            "cells x genes, raw integer UMI counts in X, Ensembl ids in var['gene_ids'] "
            "(gex_only=False keeps antibody/CRISPR features). Do not hand-parse it with h5py: the "
            "matrix is CSC with ONE COLUMN PER BARCODE and shape = [n_features, n_barcodes]. A "
            "delivery's raw_feature_bc_matrix.h5 reads the same way (mostly empty droplets).")
    if root:
        parts.append(
            f"- AISCIENTIST_DATASET_ROOT = {root}  (the whole folder the user bound; "
            "AISCIENTIST_DATASET is the one primary file picked inside it. Read its other files — "
            "e.g. a Cell Ranger delivery's raw_feature_bc_matrix, analysis/, web_summary.html — "
            "from here, read-only.)"
        )
    if work:
        parts.append(
            f"- AISCIENTIST_WORK = {work}  (pipeline checkpoints: adata_qc.h5ad after QC, "
            "adata_clustered.h5ad after clustering, adata_de.h5ad after DE — clustering adds a "
            "'leiden' obs column. Call os.listdir(os.environ['AISCIENTIST_WORK']) to see what exists "
            "NOW instead of assuming a file is already there.)"
        )
    if artifacts:
        parts.append(
            f"- AISCIENTIST_ARTIFACTS = {artifacts}  (write NEW figures under figures/, tables under tables/)"
        )
    if uploads:
        parts.append(
            f"- AISCIENTIST_UPLOADS = {uploads}  (EVERY file/folder the user uploaded this session, "
            "with nested subfolders preserved. Use os.walk(os.environ['AISCIENTIST_UPLOADS']) to "
            "discover all inputs — incl. folders added after the primary dataset.)"
        )
    if inputs:
        parts.append(
            "- AISCIENTIST_INPUTS = the run's OTHER attached files (read-only), as a JSON list of "
            "{name, role, path} — json.loads(os.environ['AISCIENTIST_INPUTS']):")
        parts.extend(f"  - {i.get('name') or i['path']} ({i.get('role') or 'extra file'}): {i['path']}"
                     for i in inputs)
        ref = input_env(inputs).get("AISCIENTIST_REFERENCE")
        if ref:
            parts.append(
                f"- AISCIENTIST_REFERENCE = {ref}  (the file the user marked as the REFERENCE, e.g. "
                "a cell-type atlas: evidence to compare the primary dataset against — never QC, "
                "cluster or test it as the study's own data)")
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

    supports_skill_policy = True
    timeout_s: float = 180.0   # CodeAct now does real data work (read the dataset, compute) — give it room
    mem_mb: int = 2048
    python_bin: str = sys.executable
    container_image: str | None = None     # set on HPC nodes for real isolation
    container_bin: str = "apptainer"     # the HPC3 runtime (HPCSettings.container_bin)
    # Network access for contained code. Default from AISCIENTIST_SANDBOX_NETWORK (ON) — see
    # sandbox_network_enabled(); an explicit True/False still overrides per-instance.
    allow_network: bool = field(default_factory=sandbox_network_enabled)
    max_output: int = 20_000
    # Per-run data access (set by the gateway): the snippet sees these as the env vars
    # AISCIENTIST_DATASET / AISCIENTIST_WORK / AISCIENTIST_ARTIFACTS so CodeAct can read the
    # dataset + the run's checkpoints/outputs and write new artifacts.
    dataset_path: str | None = None
    work_dir: str | None = None
    artifacts_dir: str | None = None
    # The whole per-user uploads tree (all files/folders the user uploaded this session).
    # Exposed as AISCIENTIST_UPLOADS so run_code can reach EVERY upload — incl. folders added
    # after the run's primary dataset — just like a general file workspace.
    uploads_dir: str | None = None
    # The folder the primary dataset was picked from, when the user bound a folder. Exposed as
    # AISCIENTIST_DATASET_ROOT: a delivery's other files (the raw matrix beside the filtered one)
    # are part of the dataset, not separate uploads.
    dataset_root: str | None = None
    # The run's OTHER bound files ({name, role, path}, local paths), exposed read-only as
    # AISCIENTIST_INPUTS / AISCIENTIST_REFERENCE (see input_env).
    inputs: list[dict] | None = None
    # Stop button: polled while the snippet runs so a Stop KILLS the in-flight subprocess (and
    # its whole process group) within ~a second, instead of the old blocking subprocess.run that
    # ignored Stop until the 180s timeout. Set to conn.chat_stop.is_set.
    should_cancel: Callable[[], bool] | None = None

    def __call__(self, code: str) -> dict[str, object]:
        return self.run(code)

    def run(self, code: str) -> dict[str, object]:
        from .skills_registry import POLICY
        policy = POLICY.get()
        if policy and (not self.container_image or policy["gpu"] or policy["scheduler"] == "required"):
            return {"status": "error", "error": "local executor cannot enforce this skill's isolation/GPU/scheduler requirements"}
        code = (code or "").strip()
        if not code:
            return {"status": "error", "error": "no code provided"}
        with tempfile.TemporaryDirectory(prefix="aiscientist-code-") as workdir:
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

    def _input_paths(self) -> list[str]:
        """Existing host paths of the other bound files not already under a data path above."""
        covered = [p.rstrip("/") + "/" for p in self._data_paths()]
        return [str(i["path"]) for i in (self.inputs or ())
                if isinstance(i, dict) and i.get("path") and os.path.exists(str(i["path"]))
                and not any(str(i["path"]).startswith(c) for c in covered)]

    def _command(self, script: str, workdir: str):
        if self.container_image:
            from .skills_registry import POLICY, network_allowed
            policy = POLICY.get()
            network = network_allowed(policy, self.allow_network)
            net = [] if network else ["--net", "--network", "none"]
            if policy:
                net += ["--cleanenv"]
            binds = ["-B", f"{workdir}:{workdir}"]
            for p in self._data_paths():            # mount the dataset + run dirs read/write
                binds += ["-B", f"{p}:{p}"]
            for p in self._input_paths():           # the other bound files, read-only
                binds += ["-B", f"{p}:{p}:ro"]
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
        # path (Apptainer --memory / cgroups), not from rlimits on this process.

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
        # default ($HOME/.config + $HOME/.cache) is often read-only inside the Apptainer
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
            env["AISCIENTIST_DATASET"] = str(self.dataset_path)
        if self.work_dir:
            env["AISCIENTIST_WORK"] = str(self.work_dir)
        if self.artifacts_dir:
            env["AISCIENTIST_ARTIFACTS"] = str(self.artifacts_dir)
        if self.uploads_dir:
            env["AISCIENTIST_UPLOADS"] = str(self.uploads_dir)
        if self.dataset_root:
            env["AISCIENTIST_DATASET_ROOT"] = str(self.dataset_root)
        env.update(input_env(self.inputs))
        from .skills_registry import POLICY
        policy = POLICY.get()
        if policy:
            env.update({"APPTAINERENV_" + k: v for k, v in policy["secret_values"].items()})
        return env
