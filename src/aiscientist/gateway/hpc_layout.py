"""Where AiScientist keeps its files on HPC3 (RCHENLAB/AiScientist issue 28).

HPC3 holds only environments, images, model weights and one folder per session, under the shared
root (``AISCIENTIST_HPC_SHARED_ROOT``)::

    <root>/
    ├── envs/                      environments: openspliceai/, analysis-pydeps/, run-code-pkgs/,
    │                              conda-pkgs/ (the lab's conda download cache), lab/ (environments
    │                              the agent asked for, hpc/environments.py)
    ├── containers/                images
    ├── models/                    model weights
    └── sessions/<session-uuid>/   one session, removed once its results are on the server
        ├── manifest.json          session, account, HPC3 user, app version, when it started
        ├── app/                   the AiScientist package its jobs import
        ├── scripts/  logs/        the housekeeping scripts and their logs
        ├── inputs/                datasets copied from the server for its runs
        ├── shell-home/ downloads/ the agent's shell home and its downloads
        ├── jobs/<line>/           job files that belong to no run (the CPU worker, PaperQA)
        └── runs/<run_id>/
            ├── analysis/          work/ (checkpoints), artifacts/, result/ (the run's results)
            ├── variant/ phenotype/ reports/ scgpt/ vlreview/ corpus/
            └── jobs/<line>/       the run's job scripts, args/result JSON and logs

Folder names are ids only. A session folder is readable by its HPC3 user only (0700); ``sessions/``
is group-writable with the sticky bit, so every lab member can create a session folder and no one
can remove another's.

What the old layout kept per user (``Temp/<user>``, ``pysrc/<user>``, ``bin/<user>``,
``uploads/<user>``) and the shared ``pkgs/`` and ``pydeps/`` are retired. A run that started before
the change keeps its folder in ``Temp/<user>/<kind>/<run_id>`` until it is removed: :class:`RunHome`
says where a run's folders are, in either layout, so a run continued after the change still finds
its checkpoints.

This module only builds paths; it never talks to HPC3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ENVS, CONTAINERS, MODELS, SESSIONS = "envs", "containers", "models", "sessions"
# Retired: nothing new is written there. Removed once empty (see the cutover plan).
LEGACY = ("Temp", "uploads", "pysrc", "bin", "pkgs", "pydeps")

MANIFEST = "manifest.json"
APP, SCRIPTS, LOGS, INPUTS, HOME, DOWNLOADS, JOBS, RUNS = (
    "app", "scripts", "logs", "inputs", "shell-home", "downloads", "jobs", "runs")

# sessions/ itself: setgid (files stay in the lab's group) + sticky (only a folder's owner removes it)
SESSIONS_MODE = "3775"
SESSION_MODE = "0700"

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _root(root: str) -> str:
    return root.rstrip("/")


def is_id(name: str) -> bool:
    """A folder name made of an id: letters, digits, ``_`` and ``-`` only."""
    return bool(_ID_RE.fullmatch(name or ""))


def sessions_dir(root: str) -> str:
    return f"{_root(root)}/{SESSIONS}"


def session_dir(root: str, session_id: str) -> str:
    if not is_id(session_id):
        raise ValueError(f"not a session id: {session_id!r}")
    return f"{sessions_dir(root)}/{session_id}"


def envs_dir(root: str) -> str:
    return f"{_root(root)}/{ENVS}"


def containers_dir(root: str) -> str:
    return f"{_root(root)}/{CONTAINERS}"


def models_dir(root: str) -> str:
    return f"{_root(root)}/{MODELS}"


def package_cache_root(root: str) -> str:
    """The packages ``run_code`` installs when the image lacks one, shared by the lab
    (``package_cache.py``; was ``<root>/pkgs``)."""
    return f"{envs_dir(root)}/run-code-pkgs"


def lab_envs_dir(root: str) -> str:
    """Environments built at the agent's request and shared by the lab, one folder per recipe
    (``hpc/environments.py``)."""
    return f"{envs_dir(root)}/lab"


def conda_pkgs_dir(root: str) -> str:
    """The lab's conda download cache, read and extended by image builds (was ``<root>/conda-pkgs``)."""
    return f"{envs_dir(root)}/conda-pkgs"


def legacy_dir(root: str, kind: str, user: str) -> str:
    """``<root>/<kind>/<user>`` of the retired per-user layout (``Temp``, ``uploads``, ``pysrc``,
    ``bin``)."""
    return f"{_root(root)}/{kind}/{user}"


@dataclass(frozen=True)
class RunHome:
    """Where one run's folders are on HPC3.

    ``layout`` is ``session`` (``root`` is ``sessions/<sid>/runs/<run_id>``) or ``legacy`` (``root``
    is ``Temp/<user>``, and a run's folders are ``Temp/<user>/<kind>/<run_id>``)."""

    run_id: str
    layout: str
    root: str
    session_id: str = ""

    def dir(self, kind: str) -> str:
        """The run's folder of one kind: ``analysis``, ``variant``, ``reports`` ..."""
        if self.layout == "legacy":
            return f"{self.root}/{kind}/{self.run_id}"
        return f"{self.root}/{kind}"

    def jobs(self, line: str) -> str:
        """The run's job files of one job line (``analysis``, ``runcode``, ``report`` ...)."""
        if self.layout == "legacy":
            return f"{self.root}/scratch/{line}/{self.run_id}"
        return f"{self.root}/{JOBS}/{line}"

    def to_json(self) -> dict:
        return {"run_id": self.run_id, "layout": self.layout, "root": self.root,
                "session_id": self.session_id}

    @classmethod
    def from_json(cls, data: dict) -> "RunHome | None":
        try:
            home = cls(run_id=str(data["run_id"]), layout=str(data["layout"]), root=str(data["root"]),
                       session_id=str(data.get("session_id") or ""))
        except (KeyError, TypeError):
            return None
        return home if home.layout in ("session", "legacy") and is_id(home.run_id) else None


def session_run(root: str, session_id: str, run_id: str) -> RunHome:
    if not is_id(run_id):
        raise ValueError(f"not a run id: {run_id!r}")
    return RunHome(run_id=run_id, layout="session",
                   root=f"{session_dir(root, session_id)}/{RUNS}/{run_id}", session_id=session_id)


def legacy_run(root: str, user: str, run_id: str) -> RunHome:
    if not is_id(run_id):
        raise ValueError(f"not a run id: {run_id!r}")
    return RunHome(run_id=run_id, layout="legacy", root=legacy_dir(root, "Temp", user))


def owns_path(root: str, path: str, *, user: str, session_ids: "tuple[str, ...] | list[str]" = ()) -> bool:
    """Is ``path`` one of this user's run or session paths, as a stored record names it: inside a
    session folder of ``session_ids`` (when given) or any session folder, or inside the user's
    retired ``Temp/<user>``. A clean absolute path only: no ``..``, ``.`` or empty component."""
    if not path.startswith("/") or any(c in path for c in "\t\n\r\x00"):
        return False
    parts = path.split("/")[1:]
    if not parts or any(p in ("", ".", "..") for p in parts):
        return False
    temp = legacy_dir(root, "Temp", user) + "/"
    if path.startswith(temp):
        return True
    base = sessions_dir(root) + "/"
    if not path.startswith(base):
        return False
    sid = path[len(base):].split("/", 1)[0]
    return is_id(sid) and (not session_ids or sid in session_ids)
