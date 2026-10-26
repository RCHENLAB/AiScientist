"""Which release is running — read from the ``VERSION`` files the release packages carry.

The app release tarball (built by ``.github/workflows/release.yml``) ships a ``VERSION`` file at its
root: ``KEY=value`` lines (``tag``, ``sha``, ``built_at``). A dev checkout has none, so the app
reports ``"dev"``. The skills tag is the one ``configs/skills_registry.json`` pins.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _read_version_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def skills_release() -> dict[str, Any]:
    """The skills release the app pins (``configs/skills_registry.json``), and whether it is installed."""
    from ..agents import skills_registry

    try:
        cfg = skills_registry.config()
        installed = skills_registry.release_dir(cfg).is_dir()
        return {"tag": cfg["release_tag"], "installed": installed}
    except (OSError, ValueError, KeyError):
        return {"tag": None, "installed": False}


def app_release() -> dict[str, Any]:
    info = _read_version_file(_REPO_ROOT / "VERSION")
    return {
        "tag": info.get("tag", "dev"),
        "sha": info.get("sha"),
        "built_at": info.get("built_at"),
    }


def release_info() -> dict[str, Any]:
    return {
        "app": app_release(),
        "skills": skills_release(),
        "python": platform.python_version(),
    }
