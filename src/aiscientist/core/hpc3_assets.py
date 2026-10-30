"""The pinned list of what AiScientist runs on HPC3 (``configs/hpc3_assets.yaml``): container images
with their SHA-256, model weights with their revision and per-file SHA-256.

A run's ``manifest.json`` records the entry of each image and model it used
(``agents/run_manifest.py``); ``scripts/hpc3_assets_check.py`` compares the list with HPC3.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
_CACHE: dict[str, Any] = {"key": None, "doc": {}}


def list_path() -> Path:
    return Path(os.environ.get("AISCIENTIST_HPC3_ASSETS", ROOT / "configs" / "hpc3_assets.yaml"))


def load() -> dict:
    """The list, re-read when the file changes. Empty when it is missing or unreadable."""
    path = list_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _CACHE["key"] != key:
            import yaml
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            _CACHE.update(key=key, doc=doc if isinstance(doc, dict) else {})
    except (OSError, ImportError, ValueError):
        return {}
    except Exception:  # noqa: BLE001 - a malformed list must not break a run (yaml.YAMLError)
        return {}
    return _CACHE["doc"]


def image_pin(path: str) -> dict | None:
    """The list's entry for the image at ``path`` (absolute on HPC3, or relative to the shared root),
    as ``{"name", "sha256", "source", "version"}``; None for an image the list does not hold."""
    if not path:
        return None
    for name, entry in (load().get("images") or {}).items():
        rel = str((entry or {}).get("path") or "").strip("/")
        if rel and (path == rel or path.endswith("/" + rel)):
            return {"name": name, "sha256": entry.get("sha256"), "source": entry.get("source"),
                    "version": entry.get("version")}
    return None


def files_digest(files: dict) -> str | None:
    """One SHA-256 over a model's weight files (``name sha256`` lines, sorted), so a manifest names
    the exact set of weights without listing every file."""
    if not files:
        return None
    lines = "".join(f"{name} {digest}\n" for name, digest in sorted(files.items()))
    return hashlib.sha256(lines.encode()).hexdigest()


def model_pin(repo: str) -> dict | None:
    """The list's entry for model ``repo`` as ``{"revision", "source", "files", "files_sha256"}``
    (``files`` = how many weight files are pinned); None for a model the list does not hold."""
    entry = (load().get("models") or {}).get(repo)
    if not isinstance(entry, dict):
        return None
    files = entry.get("files") or {}
    return {"revision": entry.get("revision"), "source": entry.get("source"), "files": len(files),
            "files_sha256": files_digest(files)}
