"""Which release is running — read from the ``VERSION`` files the release packages carry.

The app release tarball (built by ``.github/workflows/release.yml``) ships a ``VERSION`` file at its
root and the skills release ships one at the root of the skills dir. Both are ``KEY=value`` lines
(``tag``, ``sha``, ``built_at``). A dev checkout has no ``VERSION`` file, so everything degrades to
``"dev"`` / ``None`` instead of failing.
"""

from __future__ import annotations

import os
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


def _skills_dir() -> Path:
    env = os.environ.get("AISCIENTIST_SKILLS_DIR")
    return Path(env) if env else _REPO_ROOT / "skills"


def app_release() -> dict[str, Any]:
    info = _read_version_file(_REPO_ROOT / "VERSION")
    return {
        "tag": info.get("tag", "dev"),
        "sha": info.get("sha"),
        "built_at": info.get("built_at"),
    }


def skills_release() -> dict[str, Any]:
    info = _read_version_file(_skills_dir() / "VERSION")
    return {
        "tag": info.get("tag") or os.environ.get("AISCIENTIST_SKILLS_TAG") or None,
        "sha": info.get("sha"),
        "built_at": info.get("built_at"),
    }


def release_info() -> dict[str, Any]:
    return {
        "app": app_release(),
        "skills": skills_release(),
        "python": platform.python_version(),
    }
