"""Which release is running — read from the ``VERSION`` files the release packages carry.

The app release tarball (built by ``.github/workflows/release.yml``) ships a ``VERSION`` file at its
root: ``KEY=value`` lines (``tag``, ``sha``, ``built_at``). An rsync deploy or a checkout has none: it
reports no tag and the commit it runs (``.deployed_sha`` or git). The skills tag is the one
``configs/skills_registry.json`` pins.
"""

from __future__ import annotations

import json
import platform
import time
from typing import Any

from ..agents import provenance
from ..agents.provenance import read_version_file as _read_version_file  # noqa: F401  (tests)


def skills_release() -> dict[str, Any]:
    """The skills release the app reads: the one ``skills/current`` points at when that link exists,
    else the tag ``configs/skills_registry.json`` pins. ``installed`` says whether it is on disk,
    ``loaded`` which release the running library came from, ``sha`` the commit of its tag as
    ``deploy/deploy_skills.sh`` recorded it at install."""
    from ..agents import skills_registry

    try:
        cfg = skills_registry.config()
        root = skills_registry.release_dir(cfg)
        out: dict[str, Any] = {"tag": cfg["release_tag"], "installed": root.is_dir()}
    except (OSError, ValueError, KeyError) as exc:
        return {"tag": None, "installed": False, "error": str(exc)}
    if cfg["pinned_tag"] != cfg["release_tag"]:
        out["pinned"] = cfg["pinned_tag"]
    try:
        out["sha"] = json.loads((root.parent / f"{root.name}.install.json").read_text())["commit"] or None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    status = skills_registry.STATUS
    if status["tag"] or status["error"]:
        out.update(loaded=status["tag"], skills=status["skills"])
        if status["error"]:
            out["error"] = status["error"]
    return out


_REFRESHED_AT = 0.0


def loaded_skills_tag() -> str | None:
    """The skills release the running library came from, after bringing the library up to date with
    the disk (at most every 2 s: ``/health`` is public). A switched ``skills/current`` is read here, so
    ``deploy/deploy_skills.sh`` sees the switch take or the release refused."""
    global _REFRESHED_AT
    from ..agents import skills_registry
    from ..agents.skills import refresh_skills

    now = time.monotonic()
    if now - _REFRESHED_AT >= 2.0:
        _REFRESHED_AT = now
        try:
            refresh_skills()
        except Exception:  # noqa: BLE001 - a liveness probe never fails on the skill library
            pass
    return skills_registry.STATUS["tag"]


def app_tag() -> str | None:
    """The release tag this process runs, or None outside a release package. Reads one small file."""
    return provenance.release_version().get("tag") or None


def app_release() -> dict[str, Any]:
    info = provenance.release_version()
    if info.get("tag"):
        return {"tag": info["tag"], "sha": info.get("sha"), "built_at": info.get("built_at")}
    return {"tag": None, "sha": provenance.git_sha(), "built_at": None}


def release_info() -> dict[str, Any]:
    return {
        "app": app_release(),
        "skills": skills_release(),
        "python": platform.python_version(),
    }
