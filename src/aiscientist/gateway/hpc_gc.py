"""Housekeeping for AiScientist's own files on HPC3.

Everything is scoped to ``<shared_root>`` and, inside it, to the session folders this HPC3 user
owns (``hpc_layout``: ``sessions/<session-uuid>/``) and the user's folders of the retired layout
(``Temp/<user>`` ...):

* :func:`ensure_shared_dirs`: create ``sessions/`` and this session's folder at connect.
* :func:`sweep_temp`: remove what sessions left once it has gone untouched for ``temp_ttl_days``
  (default 3), keeping every run's ``result/`` until it is on the server. Submitted as a Slurm batch
  job: walking trees and calling ``rm -rf`` is real filesystem work, and RCIC's login nodes are for
  logging in and SUBMITTING jobs, not for doing them. The login node runs one ``sbatch``.
* :func:`clean_session`: when a session ends, remove its folder (and the runs named to it), by
  name, after its results are on the server and checked (``deploy/hpc3/aiscientist_session_clean.sh``,
  ``stage_out``). Also a batch job.

The scripts are staged into the session's own ``scripts/`` and log to its ``logs/``. They are shell
scripts and not inline commands on purpose: an ``rm -rf`` loop should be reviewable in one place,
and a member can read it on the cluster before trusting it with their files.
"""

from __future__ import annotations

import json
import shlex
import tempfile
import time
from pathlib import Path

from . import hpc_layout as layout
from .executor import RemoteExecutor

# The sweeper as shipped in the repo, and where it is staged on the cluster.
GC_SCRIPT_SRC = Path(__file__).resolve().parents[3] / "deploy" / "hpc3" / "aiscientist_temp_gc.sh"
GC_SCRIPT_NAME = "temp_gc.sh"
GC_LOG_NAME = "sweep.log"       # the batch job's --output; also how the LAST sweep reports

# The session-end cleanup, staged beside the sweeper, with its own log.
CLEAN_SCRIPT_SRC = GC_SCRIPT_SRC.with_name("aiscientist_session_clean.sh")
CLEAN_SCRIPT_NAME = "session_clean.sh"
CLEAN_LOG_NAME = "clean.log"
# The cleanup of runs an EARLIER session left, queued by the next connect, logs here instead.
EARLIER_LOG_NAME = "clean-earlier.log"
# The copies of server-side datasets a session's runs read went to Temp/<user>/server-data/<tag>;
# the name is still how a cleanup of the retired layout finds them.
SERVER_DATA_KIND = "server-data"
# Where the stage-out manifests a cleanup checks a run's results against are staged:
# <session>/logs/stageout/<tag>/<run_id>.tsv. The cleanup removes the folder when nothing was kept.
STAGEOUT_DIR = "stageout"

# The retired per-user folders, as the retired layout's cleanup and the cutover know them.
LEGACY_USER_DIRS = ("Temp", "uploads", "pysrc", "bin")
DIR_MODE = "2775"


def session_paths(shared_root: str, session_id: str) -> dict[str, str]:
    """This session's folders under ``shared_root``."""
    base = layout.session_dir(shared_root, session_id)
    return {"root": shared_root.rstrip("/"), "sessions": layout.sessions_dir(shared_root),
            "session": base, "scripts": f"{base}/{layout.SCRIPTS}", "logs": f"{base}/{layout.LOGS}",
            "inputs": f"{base}/{layout.INPUTS}", "app": f"{base}/{layout.APP}",
            "runs": f"{base}/{layout.RUNS}", "jobs": f"{base}/{layout.JOBS}"}


def shared_paths(shared_root: str, user: str) -> dict[str, str]:
    """The user's folders of the retired layout (``Temp``, ``uploads``, ``pysrc``, ``bin``): read to
    finish what a session from before the change left, never written to otherwise."""
    root = shared_root.rstrip("/")
    paths = {"root": root}
    paths.update({kind.lower(): layout.legacy_dir(root, kind, user) for kind in LEGACY_USER_DIRS})
    return paths


def manifest_doc(*, session_id: str, record_id: str, owner: str, hpc_user: str, version: str,
                 git_sha: str, ttl_days: int, created_at: "float | None" = None) -> dict:
    """``manifest.json`` of a session folder: who it belongs to, which AiScientist made it, and when
    the sweep may remove what is left of it once nothing uses it."""
    now = created_at if created_at is not None else time.time()
    stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    return {"session_id": session_id, "record_id": record_id, "account": owner, "hpc_user": hpc_user,
            "app_version": version, "git_sha": git_sha, "created_at": stamp(now),
            "cleanup": f"after its results are on the server, or {ttl_days} days after its last change "
                       "(each run's result/ stays until it is on the server)"}


def ensure_shared_dirs(remote: RemoteExecutor, shared_root: str, user: str, session_id: str, *,
                       uploads: bool = False, manifest: "dict | None" = None) -> tuple[bool, str]:
    """Create ``sessions/`` and this session's folder (with ``scripts/`` and ``logs/``), and write its
    ``manifest.json``. ``uploads`` also creates the retired ``uploads/<user>``, still where a
    deployment WITHOUT a data root puts browser uploads. Returns ``(ok, message)``.

    ``shared_root`` itself is NOT created here — it is a deploy artifact that also holds the
    containers and model weights, and on HPC3 not every directory level is group-writable (the
    lab root ``/dfs3b/ruic20_lab`` is ``drwxr-s--- ruic20``). So its absence is reported with the
    exact command to create it, rather than letting every later job die on an unexplained mkdir.
    """
    root = shared_root.rstrip("/")
    paths = session_paths(shared_root, session_id)
    check = remote.exec(f"test -d {shlex.quote(root)}", timeout=30)
    if not check.ok:
        return False, (
            f"The AiScientist shared root {root} does not exist on HPC3. A lab-storage owner has to "
            f"create it once:\n"
            f"  mkdir -p {root} && chgrp ruic20_hpc {root} && chmod {DIR_MODE} {root}\n"
            f"Until then, point AISCIENTIST_HPC_SHARED_ROOT at a group-writable directory."
        )
    q = shlex.quote
    sessions, session = q(paths["sessions"]), q(paths["session"])
    extra = ""
    if uploads:
        up = q(layout.legacy_dir(root, "uploads", user))
        extra = f" && (umask 002 && mkdir -p {up} && {{ chmod {DIR_MODE} {up} 2>/dev/null || true; }})"
    # sessions/ is shared: group-writable, setgid and sticky, so any lab member's session can create
    # its folder and nobody can remove another's. chmod is best-effort ON PURPOSE (only its owner
    # may), and the `test -d`s are the real check. The session folder is the user's own, 0700.
    made = remote.exec(
        f"(umask 002 && mkdir -p {sessions} && {{ chmod {layout.SESSIONS_MODE} {sessions} 2>/dev/null || true; }})"
        f" && (umask 077 && mkdir -p {q(paths['scripts'])} {q(paths['logs'])} && chmod {layout.SESSION_MODE} {session})"
        f"{extra} && test -d {session}", timeout=60)
    if not made.ok:
        return False, f"Could not prepare {paths['session']}: {made.stderr.strip() or 'mkdir failed'}"
    if manifest is not None:
        try:
            with tempfile.TemporaryDirectory(prefix="aiscientist-session-") as tmp:
                local = Path(tmp) / layout.MANIFEST
                local.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
                remote.put_file(str(local), f"{paths['session']}/{layout.MANIFEST}")
        except Exception as exc:  # noqa: BLE001 - the folder works without it; say why it is missing
            print(f"[gc] manifest.json not written for {session_id}: {type(exc).__name__}: {exc}")
    return True, paths["session"]


def _stage(remote: RemoteExecutor, src: Path, dest: str) -> str:
    remote.put_file(str(src), dest)
    remote.exec(f"chmod 700 {shlex.quote(dest)}", timeout=30)
    return dest


def stage_gc_script(remote: RemoteExecutor, shared_root: str, session_id: str) -> str:
    """Upload the sweeper to ``<session>/scripts/temp_gc.sh`` and return the remote path. Through
    the transfer host (``put_file``), never a login node."""
    return _stage(remote, GC_SCRIPT_SRC, f"{session_paths(shared_root, session_id)['scripts']}/{GC_SCRIPT_NAME}")


def sweep_command(script_path: str, shared_root: str, ttl_days: int, user: str, *,
                  keep_sessions: "tuple[str, ...] | list[str]" = (), retire_legacy: bool = False,
                  dry_run: bool = False) -> str:
    """The sweeper invocation itself (the payload the batch job runs). Split out so a test can
    assert the guards — own user only, explicit root, explicit TTL, the live sessions named —
    without needing a cluster."""
    parts = [f"bash {shlex.quote(script_path)}", f"--root {shlex.quote(shared_root.rstrip('/'))}",
             f"--ttl-days {int(ttl_days)}", f"--user {shlex.quote(user)}"]
    parts += [f"--keep-session {shlex.quote(s)}" for s in dict.fromkeys(keep_sessions) if s]
    if retire_legacy:
        parts.append("--retire-legacy")
    parts.append("--quiet")
    if dry_run:
        parts.append("--dry-run")
    return " ".join(parts)


def _off_node(script_path: str) -> str:
    """Run a staged script from the node's own scratch: the cleanup removes the folder it was staged
    in, and a script deleted under a running shell is not safe on every filesystem."""
    return (f'S="${{TMPDIR:-/tmp}}/aiscientist-$$-{Path(script_path).name}"; '
            f'cp {shlex.quote(script_path)} "$S" && bash "$S"')


def submit_command(payload: str, log_path: str, user: str, *,
                   partition: str, account: str, name: str = "tempgc",
                   time_limit: str = "00:30:00") -> str:
    """The ``sbatch`` line that runs ``payload`` on a COMPUTE node.

    The sweep walks directory trees and calls ``rm -rf`` — real filesystem work, and exactly what
    RCIC's rule keeps off the login nodes ("login nodes are for logging in and submitting Slurm
    jobs"). So the login node only ever runs this `sbatch`; the `find`/`rm` happen on a compute
    node in the free CPU partition. ``--wrap`` means we do not even write a script file to submit.
    """
    flags = [
        f"--job-name=aiscientist-{name}-{user}",
        f"--partition={partition}",
        "--cpus-per-task=1", "--mem=2G", f"--time={time_limit}",
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
               *, session_id: str, keep_sessions: "tuple[str, ...] | list[str]" = (),
               retire_legacy: bool = False, partition: str = "standard", account: str = "",
               script_path: str | None = None, dry_run: bool = False) -> dict:
    """Sweep this user's session folders (and what is left of the retired ``Temp/<user>``) as a
    Slurm batch job. ``keep_sessions`` are the user's live sessions, never touched; ``session_id``
    is the one submitting, whose ``scripts/`` and ``logs/`` the job uses. ``retire_legacy`` lets the
    job remove the retired ``pysrc/<user>`` and ``bin/<user>`` once ``Temp/<user>`` is empty: the
    caller passes it only when no job of the user could still be reading them.

    Asynchronous on purpose: housekeeping must never make a user wait, and the work belongs on a
    compute node. Returns ``{"status": "submitted", "job_id", "removed", "kept", "held", ...}``
    where ``removed``/``kept``/``held`` describe the PREVIOUS sweep of this session (read from its
    log) — the one just submitted reports itself next time round.

    Never raises: housekeeping must not be able to break a session. ``ttl_days <= 0`` disables.
    """
    if ttl_days <= 0:
        return {"status": "disabled", "removed": 0, "kept": 0}
    log_path = f"{session_paths(shared_root, session_id)['logs']}/{GC_LOG_NAME}"
    try:
        last = _read_last_result(remote, log_path) or {}
        path = script_path or stage_gc_script(remote, shared_root, session_id)
        payload = sweep_command(path, shared_root, ttl_days, user,
                                keep_sessions=[session_id, *keep_sessions],
                                retire_legacy=retire_legacy, dry_run=dry_run)
        res = remote.exec(submit_command(payload, log_path, user,
                                         partition=partition, account=account), timeout=120)
    except Exception as exc:  # noqa: BLE001 - a failed sweep is reported, never fatal
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "removed": 0, "kept": 0}
    if not res.ok:
        return {"status": "error", "error": res.stderr.strip() or "sbatch failed",
                "removed": 0, "kept": 0}
    return {"status": "submitted", "job_id": res.out.split(";")[0].strip(),
            "removed": last.get("removed", 0), "kept": last.get("kept", 0),
            "held": last.get("held", 0), "ttl_days": ttl_days,
            "root": layout.sessions_dir(shared_root), "submitted_at": time.time()}


# --- session end --------------------------------------------------------------------------------

def session_mirror_base(shared_root: str, session_id: str) -> str:
    """Where ONE session keeps its copies of server-side datasets: its ``inputs/``. Per session on
    purpose: nothing is reused by the next session (it copies again from the server), and removing
    it when the session ends cannot take a file another session is reading."""
    return session_paths(shared_root, session_id)["inputs"]


def stage_clean_script(remote: RemoteExecutor, shared_root: str, session_id: str) -> str:
    """Upload the session cleanup to ``<session>/scripts/session_clean.sh``; returns the remote path.
    Staged at connect, so the disconnect itself is one ``sbatch``."""
    return _stage(remote, CLEAN_SCRIPT_SRC,
                  f"{session_paths(shared_root, session_id)['scripts']}/{CLEAN_SCRIPT_NAME}")


def session_clean_command(script_path: str, shared_root: str, user: str, *,
                          session_dirs: "tuple[str, ...] | list[str]" = (), keep_inputs: bool = False,
                          run_dirs: "tuple[str, ...] | list[str]" = (),
                          runs: "tuple[str, ...] | list[str]" = (), session: str = "",
                          legacy_mirrors: "tuple[str, ...] | list[str]" = (),
                          verify_dir: str = "", dry_run: bool = False) -> str:
    """The cleanup invocation (the payload the batch job runs): explicit targets only. Split out so a
    test can assert what a disconnect asks to be removed without needing a cluster.

    ``session_dirs`` and ``run_dirs`` are folders of the session layout; ``runs``, ``session`` and
    ``legacy_mirrors`` name a run, a server-data copy and an old mirror of the retired ``Temp/<user>``
    layout."""
    parts = [f"bash {shlex.quote(script_path)}", f"--root {shlex.quote(shared_root.rstrip('/'))}",
             f"--user {shlex.quote(user)}"]
    parts += [f"--session-dir {shlex.quote(d)}" for d in session_dirs]
    if session_dirs and keep_inputs:
        parts.append("--keep-inputs")
    parts += [f"--run-dir {shlex.quote(d)}" for d in run_dirs]
    parts += [f"--run {shlex.quote(r)}" for r in runs]
    if session:
        parts.append(f"--session {shlex.quote(session)}")
    parts += [f"--legacy-mirror {shlex.quote(t)}" for t in legacy_mirrors]
    if verify_dir:
        parts.append(f"--verify-dir {shlex.quote(verify_dir)}")
    parts.append("--quiet")
    if dry_run:
        parts.append("--dry-run")
    return " ".join(parts)


def stage_verify_manifests(remote: RemoteExecutor, logs_dir: str, tag: str,
                           manifests: "dict[str, str]") -> str:
    """Put each run's stage-out manifest (``stage_out.verify_tsv``) on HPC3 as
    ``<logs>/stageout/<tag>/<run_id>.tsv`` for the cleanup to check; returns that folder. Small
    files, through the transfer host like the scripts."""
    folder = f"{logs_dir.rstrip('/')}/{STAGEOUT_DIR}/{tag}"
    made = remote.exec(f"umask 077 && mkdir -p {shlex.quote(folder)}", timeout=30)
    if not getattr(made, "ok", True):
        raise OSError(f"could not create {folder}: {getattr(made, 'stderr', '')}".strip())
    with tempfile.TemporaryDirectory(prefix="aiscientist-stageout-") as tmp:
        for run_id, text in manifests.items():
            local = Path(tmp) / f"{run_id}.tsv"
            local.write_text(text, encoding="utf-8")
            remote.put_file(str(local), f"{folder}/{run_id}.tsv")
    return folder


def clean_session(remote: RemoteExecutor, shared_root: str, user: str, *,
                  session_id: str, session_dirs: "tuple[str, ...] | list[str]" = (),
                  keep_inputs: bool = False,
                  run_dirs: "dict[str, str] | None" = None,
                  runs: "tuple[str, ...] | list[str]" = (), session: str = "",
                  legacy_mirrors: "tuple[str, ...] | list[str]" = (),
                  verify: "dict[str, str] | None" = None, tag: str = "", log_name: str = CLEAN_LOG_NAME,
                  partition: str = "standard", account: str = "",
                  script_path: str | None = None, dry_run: bool = False) -> dict:
    """Remove an ended session's own files as a Slurm batch job, by name.

    ``session_id`` is the session queuing the job: its ``scripts/`` hold the script and its ``logs/``
    the log and the manifests. ``session_dirs`` are session folders to clear (everything in each
    but ``logs/``, ``runs/`` and ``manifest.json``); ``run_dirs`` maps a run id to its folder in the session layout;
    ``runs`` names runs of the retired ``Temp/<user>`` layout. ``verify`` maps a run to its stage-out
    manifest: the script removes a run with results only when they match it (see ``stage_out``).

    Submitted and not waited for: the caller is about to close the SSH session, and the job needs
    none. Returns ``{"status": "submitted", "job_id", "log", ...}``, ``{"status": "nothing"}`` when
    there is nothing to remove, or ``{"status": "error", "error"}``. Never raises: a cleanup that
    cannot be queued must not keep a session from closing (the sweep still removes what is left
    by age, keeping results).
    """
    run_dirs = dict(run_dirs or {})
    session_dirs = [d for d in session_dirs if d]
    if not (session_dirs or run_dirs or runs or session or legacy_mirrors):
        return {"status": "nothing", "runs": 0}
    paths = session_paths(shared_root, session_id)
    log_path = f"{paths['logs']}/{log_name}"
    try:
        path = script_path or stage_clean_script(remote, shared_root, session_id)
        verify_dir = ""
        named = set(runs) | set(run_dirs)
        wanted = {r: t for r, t in (verify or {}).items() if r in named and t}
        if wanted:
            verify_dir = stage_verify_manifests(remote, paths["logs"], tag or "session", wanted)
        payload = session_clean_command(path, shared_root, user, session_dirs=session_dirs,
                                        keep_inputs=keep_inputs, run_dirs=list(run_dirs.values()),
                                        runs=runs, session=session, legacy_mirrors=legacy_mirrors,
                                        verify_dir=verify_dir, dry_run=dry_run)
        # A short cap: the user is waiting on a disconnect, and the age sweep covers a miss.
        res = remote.exec(submit_command(_off_node(path) + payload[len(f"bash {shlex.quote(path)}"):],
                                         log_path, user, partition=partition, account=account,
                                         name="sessionclean", time_limit="01:00:00"), timeout=30)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    if not res.ok:
        return {"status": "error", "error": res.stderr.strip() or "sbatch failed"}
    out = {"status": "submitted", "job_id": res.out.split(";")[0].strip(),
           "runs": len(run_dirs) + len(runs), "session": session or ",".join(session_dirs),
           "legacy_mirrors": len(legacy_mirrors), "log": log_path}
    if verify_dir:
        out["verified_runs"] = len(wanted)
    return out
