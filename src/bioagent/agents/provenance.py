"""Code provenance stamps for ``run_code`` executions — the reproducibility layer.

Mirrors the Kosmos ``CodeProvenance`` idea (``kosmos/execution/provenance.py``, paper WP8):
every CodeAct execution is stamped with what is needed to *reproduce* it —

- ``code_sha256`` : hash of the exact snippet the model wrote (before any seed preamble),
- ``seed``        : the random seed we injected (so stochastic snippets are deterministic),
- ``git_sha``     : the code version of THIS checkout that ran it,
- ``model``       : the LLM that generated the snippet,
- ``dataset_*``   : a fingerprint (and, when cheap, the full content SHA) of the input data,
- ``work_dir`` / ``artifacts_dir`` : where checkpoints/outputs went,
- ``execution_mode`` / ``returncode`` / ``status`` / timing : the outcome.

The stamp is attached to the ``run_code`` result dict, so it rides along into the step
record (``HarnessResult.steps[*].result``), the ``LabRound``, and the technical report
with NO downstream changes — capture happens at one choke point (``make_run_code_tool._exec``).

Design constraints: this runs inside a latency-sensitive tool call, so we never hash a
multi-GB dataset on the hot path more than once (memoized by path+size+mtime), and we cap
content hashing by size — falling back to a cheap ``path:size:mtime`` fingerprint and
recording WHICH method was used, so the record is always honest about what it verified.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- knobs (env-overridable, safe defaults) ---------------------------------

SEED_ENV = "BIOAGENT_RUN_CODE_SEED"          # the injected seed (default 0 = reproducible)
SEED_ENABLE_ENV = "BIOAGENT_RUN_CODE_SEED_ENABLE"   # set "0" to disable seed injection
# Above this size we do NOT content-hash the dataset on the hot path (fingerprint instead).
MAX_CONTENT_HASH_BYTES = int(os.environ.get("BIOAGENT_PROVENANCE_MAX_HASH_BYTES", str(2 * 1024**3)))


def now_iso() -> str:
    """Current UTC time, ISO-8601 (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def perf_now() -> float:
    return time.perf_counter()


def resolve_seed() -> int:
    """The seed we inject into every snippet (env-overridable, default 0)."""
    try:
        return int(os.environ.get(SEED_ENV, "0"))
    except ValueError:
        return 0


def seeding_enabled() -> bool:
    return os.environ.get(SEED_ENABLE_ENV, "1").strip() not in ("0", "false", "False", "")


def seed_preamble(seed: int) -> str:
    """A tiny, fully-guarded seeding block prepended to a snippet so stochastic code
    (random / numpy / torch) is deterministic. Every line is guarded — a missing library
    or a seeding failure can NEVER break the user's snippet. Caveat: this prepends lines,
    so a snippet whose FIRST statement is ``from __future__ import ...`` would break; that
    is not valid in analysis snippets, which is the only thing run_code runs."""
    return (
        f"import os as _bio_os; _bio_os.environ.setdefault('PYTHONHASHSEED', '{seed}')\n"
        f"try:\n    import random as _bio_r; _bio_r.seed({seed})\nexcept Exception: pass\n"
        f"try:\n    import numpy as _bio_np; _bio_np.random.seed({seed})\nexcept Exception: pass\n"
        f"try:\n    import torch as _bio_t; _bio_t.manual_seed({seed})\nexcept Exception: pass\n"
    )


# --- hashing helpers --------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


_UNSET = object()                       # sentinel: git_sha not yet computed
_git_sha_cache: str | None | object = _UNSET


def git_sha() -> str | None:
    """The code version of THIS deployment, cached for the process (never raises).

    Prefers the ``.deployed_sha`` marker the rsync deploy writes at the repo root: on the
    server the checked-in ``.git`` is a stale, rsync-frozen relic (or absent) that would
    report the WRONG commit, so the marker is the only truthful source. Falls back to a live
    ``git rev-parse HEAD`` for a real checkout (dev). None if neither is available."""
    global _git_sha_cache
    if _git_sha_cache is not _UNSET:     # already resolved (incl. cached None)
        return _git_sha_cache  # type: ignore[return-value]
    sha: str | None = None
    # 1. deploy marker (repo root / .deployed_sha) — first token is the deployed SHA.
    try:
        marker = Path(__file__).resolve().parents[3] / ".deployed_sha"
        if marker.is_file():
            tokens = marker.read_text(encoding="utf-8", errors="replace").split()
            if tokens:
                sha = tokens[0] or None
    except Exception:
        sha = None
    # 2. live git checkout (dev, or any tree with no marker).
    if sha is None:
        try:
            here = Path(__file__).resolve().parent
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(here),
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                sha = out.stdout.strip() or None
        except Exception:
            sha = None
    _git_sha_cache = sha
    return sha


_dataset_hash_cache: dict[tuple[str, int, int], tuple[str, str]] = {}


def dataset_hashes(path: Any) -> tuple[str | None, str | None, str]:
    """Return ``(fingerprint, content_sha256, method)`` for a dataset path.

    - ``fingerprint`` = ``sha256("<name>:<size>:<mtime_ns>")[:16]`` — always cheap, always set
      when the file exists (detects "same file" without reading it).
    - ``content_sha256`` = full SHA-256 of the bytes, ONLY when the file is under
      ``MAX_CONTENT_HASH_BYTES`` (else None). Memoized by (path, size, mtime) so a run's
      first ``run_code`` pays it once and the rest reuse it.
    - ``method`` = "content" | "fingerprint" | "absent"."""
    if not path:
        return None, None, "absent"
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None, None, "absent"
    key = (str(p), st.st_size, st.st_mtime_ns)
    fingerprint = sha256_text(f"{p.name}:{st.st_size}:{st.st_mtime_ns}")[:16]
    if key in _dataset_hash_cache:
        content, method = _dataset_hash_cache[key]
        return fingerprint, (content or None), method
    content: str | None = None
    method = "fingerprint"
    if st.st_size <= MAX_CONTENT_HASH_BYTES:
        try:
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
            content = h.hexdigest()
            method = "content"
        except OSError:
            content, method = None, "fingerprint"
    _dataset_hash_cache[key] = (content or "", method)
    return fingerprint, content, method


# --- the record -------------------------------------------------------------

@dataclass
class CodeProvenance:
    """One reproducibility stamp for a single ``run_code`` execution."""
    code_sha256: str
    code_chars: int
    seed: int | None                 # None if seed injection was disabled
    git_sha: str | None
    model: str | None
    dataset_path: str | None
    dataset_fingerprint: str | None  # path:size:mtime fingerprint (always cheap)
    dataset_sha256: str | None       # full content hash when computed, else None
    dataset_hash_method: str         # "content" | "fingerprint" | "absent"
    work_dir: str | None
    artifacts_dir: str | None
    execution_mode: str              # "local" | "hpc_slurm" | "unknown"
    returncode: int | None
    status: str | None
    started_at: str
    finished_at: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _executor_paths(executor: Any) -> tuple[str | None, str | None, str | None]:
    """Best-effort (dataset, work, artifacts) paths off the sandbox/executor, defensively —
    a custom or test executor may expose none of them."""
    def g(name: str) -> str | None:
        v = getattr(executor, name, None)
        return str(v) if v else None
    return g("dataset_path"), g("work_dir"), g("artifacts_dir")


def build_code_provenance(
    *,
    code: str,
    seed: int | None,
    executor: Any,
    ctx: Any,
    result: dict[str, Any] | None,
    started_at: str,
    duration_ms: int,
) -> CodeProvenance:
    """Assemble the stamp from the snippet, the executor's paths, the run context, and the
    executor's result dict. Never raises — provenance must not break a run."""
    dataset_path, work_dir, artifacts_dir = _executor_paths(executor)
    fingerprint, content_sha, method = dataset_hashes(dataset_path)
    result = result or {}
    mode = str(result.get("execution_mode") or ("hpc_slurm" if result.get("slurm_state") else "local"))
    rc = result.get("returncode")
    return CodeProvenance(
        code_sha256=sha256_text(code),
        code_chars=len(code),
        seed=seed,
        git_sha=git_sha(),
        model=getattr(ctx, "model", None),
        dataset_path=dataset_path,
        dataset_fingerprint=fingerprint,
        dataset_sha256=content_sha,
        dataset_hash_method=method,
        work_dir=work_dir,
        artifacts_dir=artifacts_dir,
        execution_mode=mode,
        returncode=rc if isinstance(rc, int) else None,
        status=(str(result.get("status")) if result.get("status") is not None else None),
        started_at=started_at,
        finished_at=now_iso(),
        duration_ms=duration_ms,
    )
