"""Housekeeping for AiScientist's own files on HPC3.

Two jobs, both scoped to ``<shared_root>`` and nothing else:

* :func:`ensure_shared_dirs` — create the shared project skeleton
  (``Temp/<user>``, ``uploads/<user>``, ``pysrc/<user>``) at connect time.
* :func:`sweep_temp` — delete per-run process files under ``Temp/<user>`` once their whole
  subtree has gone untouched for ``temp_ttl_days`` (default 3). Submitted as a Slurm batch job:
  walking trees and calling ``rm -rf`` is real filesystem work, and RCIC's login nodes are for
  logging in and SUBMITTING jobs, not for doing them. The login node runs one ``sbatch``.

Why this module exists: every run's work/ + artifacts/ + Slurm scripts + job logs used to be
written into each member's PERSONAL lab dir, and the product had no HPC3-side GC at all — the
only sweeper (``app._expire_old_checkpoints``) runs on the eyeserver's local run bundles. So the
cluster side grew forever, in space nobody could safely automate against. Moving process files
into one shared ``Temp`` makes them sweepable *because* nothing hand-curated lives there.

The actual sweep is ``deploy/hpc3/aiscientist_temp_gc.sh``, staged to ``<shared_root>/bin/<user>/``
so a cron backstop and the gateway run the exact same code. It is deliberately a shell script and
not an inline command: a `rm -rf` loop should be reviewable in one place, and a member can read it
on the cluster before trusting it with their files.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from .executor import RemoteExecutor

# The sweeper as shipped in the repo, and where it is staged on the cluster.
GC_SCRIPT_SRC = Path(__file__).resolve().parents[3] / "deploy" / "hpc3" / "aiscientist_temp_gc.sh"
GC_SCRIPT_NAME = "temp_gc.sh"
GC_LOG_NAME = "temp_gc.log"     # the batch job's --output; also how the LAST sweep reports

# Standard skeleton under <shared_root>. Every one of these is per-user; only ``Temp`` is swept.
# ``bin`` is per-user too — several members' sessions stage the sweeper concurrently, and one
# shared copy would mean each overwriting a file the others own.
SHARED_SUBDIRS = ("Temp", "uploads", "pysrc", "bin")

# setgid + group-write: files land in ruic20_hpc and stay writable by the group, so a single
# cron entry under ONE account can sweep everybody's Temp if we ever want that.
DIR_MODE = "2775"


def shared_paths(shared_root: str, user: str) -> dict[str, str]:
    """The per-user dirs under ``shared_root`` this session writes to."""
    root = shared_root.rstrip("/")
    paths = {"root": root}
    paths.update({kind.lower(): f"{root}/{kind}/{user}" for kind in SHARED_SUBDIRS})
    return paths


def ensure_shared_dirs(remote: RemoteExecutor, shared_root: str, user: str) -> tuple[bool, str]:
    """Create this user's dirs under the shared project root. Returns ``(ok, message)``.

    ``shared_root`` itself is NOT created here — it is a deploy artifact that also holds the
    containers and model weights, and on HPC3 not every directory level is group-writable (the
    lab root ``/dfs3b/ruic20_lab`` is ``drwxr-s--- ruic20``). So its absence is reported with the
    exact command to create it, rather than letting every later job die on an unexplained mkdir.
    """
    paths = shared_paths(shared_root, user)
    root = paths["root"]
    check = remote.exec(f"test -d {shlex.quote(root)}", timeout=30)
    if not check.ok:
        return False, (
            f"The AiScientist shared root {root} does not exist on HPC3. A lab-storage owner has to "
            f"create it once:\n"
            f"  mkdir -p {root} && chgrp ruic20_hpc {root} && chmod {DIR_MODE} {root}\n"
            f"Until then, point BIOAGENT_HPC_SHARED_ROOT at a group-writable directory."
        )
    quoted = [shlex.quote(paths[k.lower()]) for k in SHARED_SUBDIRS]
    targets = " ".join(quoted)
    exists = " && ".join(f"test -d {q}" for q in quoted)
    # umask 002 so what we create is group-writable; the explicit chmod fixes dirs that predate
    # that. chmod is best-effort ON PURPOSE — only the owner may chmod, and after a member leaves
    # (or if a dir was made by whoever connected first) a hard failure here would lock everyone
    # else out of a directory that is already perfectly usable. The `test -d`s are the real check.
    made = remote.exec(
        f"umask 002 && mkdir -p {targets} && {{ chmod {DIR_MODE} {targets} 2>/dev/null || true; }} "
        f"&& {exists}", timeout=60)
    if not made.ok:
        return False, f"Could not prepare {root}: {made.stderr.strip() or 'mkdir failed'}"
    return True, root


def stage_gc_script(remote: RemoteExecutor, shared_root: str, user: str) -> str:
    """Upload the sweeper to ``<shared_root>/bin/<user>/temp_gc.sh`` and return the remote path.

    Re-staged on every connect so the cluster copy can never drift behind the repo — it is ~4 KB,
    and ``put_file`` already routes through the transfer host, never a login node. Per-user, so
    two members connecting at once are not overwriting each other's file.
    """
    remote_path = f"{shared_paths(shared_root, user)['bin']}/{GC_SCRIPT_NAME}"
    remote.put_file(str(GC_SCRIPT_SRC), remote_path)
    remote.exec(f"chmod 775 {shlex.quote(remote_path)}", timeout=30)
    return remote_path


def sweep_command(script_path: str, shared_root: str, ttl_days: int, user: str,
                  *, dry_run: bool = False) -> str:
    """The sweeper invocation itself (the payload the batch job runs). Split out so a test can
    assert the guards — own user only, explicit root, explicit TTL — without needing a cluster."""
    cmd = (
        f"bash {shlex.quote(script_path)} --root {shlex.quote(shared_root.rstrip('/'))} "
        f"--ttl-days {int(ttl_days)} --user {shlex.quote(user)} --quiet"
    )
    return (cmd + " --dry-run") if dry_run else cmd


def submit_command(payload: str, log_path: str, user: str, *,
                   partition: str, account: str) -> str:
    """The ``sbatch`` line that runs ``payload`` on a COMPUTE node.

    The sweep walks directory trees and calls ``rm -rf`` — real filesystem work, and exactly what
    RCIC's rule keeps off the login nodes ("login nodes are for logging in and submitting Slurm
    jobs"). So the login node only ever runs this `sbatch`; the `find`/`rm` happen on a compute
    node in the free CPU partition. ``--wrap`` means we do not even write a script file to submit.
    """
    flags = [
        f"--job-name=aiscientist-tempgc-{user}",
        f"--partition={partition}",
        "--cpus-per-task=1", "--mem=2G", "--time=00:30:00",
        f"--output={shlex.quote(log_path)}", "--open-mode=truncate", "--parsable",
    ]
    if account:
        flags.insert(2, f"--account={account}")
    return f"sbatch {' '.join(flags)} --wrap {shlex.quote(payload)}"


def _read_last_result(remote: RemoteExecutor, log_path: str) -> dict | None:
    """Parse the ``GC_RESULT`` line the PREVIOUS batch sweep left in its log. One small file
    read — this is how an asynchronous sweep still reports what it did."""
    res = remote.exec(f"tail -n 5 {shlex.quote(log_path)} 2>/dev/null", timeout=30)
    if not res.ok:
        return None
    for line in res.stdout.splitlines():
        if line.startswith("GC_RESULT "):
            out = {}
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                if value.isdigit():
                    out[key] = int(value)
            return out or None
    return None


def sweep_temp(remote: RemoteExecutor, shared_root: str, user: str, ttl_days: int,
               *, partition: str = "standard", account: str = "",
               script_path: str | None = None, dry_run: bool = False) -> dict:
    """Sweep ``<shared_root>/Temp/<user>`` as a Slurm batch job.

    Asynchronous on purpose: housekeeping must never make a user wait, and the work belongs on a
    compute node. Returns ``{"status": "submitted", "job_id", "removed", "kept", ...}`` where
    ``removed``/``kept`` describe the PREVIOUS sweep (read from its log) — the one just submitted
    reports itself next time round.

    Never raises: housekeeping must not be able to break a session. ``ttl_days <= 0`` disables.
    """
    if ttl_days <= 0:
        return {"status": "disabled", "removed": 0, "kept": 0}
    root = shared_root.rstrip("/")
    log_path = f"{shared_paths(shared_root, user)['bin']}/{GC_LOG_NAME}"
    try:
        last = _read_last_result(remote, log_path) or {}
        path = script_path or stage_gc_script(remote, shared_root, user)
        payload = sweep_command(path, shared_root, ttl_days, user, dry_run=dry_run)
        res = remote.exec(submit_command(payload, log_path, user,
                                         partition=partition, account=account), timeout=120)
    except Exception as exc:  # noqa: BLE001 - a failed sweep is reported, never fatal
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "removed": 0, "kept": 0}
    if not res.ok:
        return {"status": "error", "error": res.stderr.strip() or "sbatch failed",
                "removed": 0, "kept": 0}
    return {"status": "submitted", "job_id": res.out.split(";")[0].strip(),
            "removed": last.get("removed", 0), "kept": last.get("kept", 0),
            "ttl_days": ttl_days, "root": f"{root}/Temp/{user}", "submitted_at": time.time()}
