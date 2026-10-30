"""A run's ``manifest.json``: what the run ran with, so its results can be reproduced.

It sits beside the run's ``artifacts/`` and holds:

- ``app``: the app release (tag and commit) that ran it;
- ``registry_release`` and ``skills``: the skills release, and each registry skill the run read or
  ran, with its version and the image it declares (``skills_registry.record_in_manifest``);
- ``images``: the container image each line of the session runs its HPC3 jobs in, with the pinned
  SHA-256 from ``configs/hpc3_assets.yaml`` (``record_environment``);
- ``models``: the session's model server (its model, revision and image), and at the end the models
  the run's calls went to, each with its pinned revision when the list holds it (``record_models``).

Writes are atomic (a temporary file, then a rename) and serialized: a plan's parallel steps write
to the same file. Every write is best-effort: a run never fails over its manifest.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)
LOCK = threading.RLock()

# HPCSettings field -> the line of the session that runs its jobs in that image.
IMAGE_LINES = (("analysis", "analysis_image"), ("report", "report_image"), ("variants", "vep_image"),
               ("phenotype", "lirical_image"), ("scgpt", "scgpt_image"), ("report_review", "vlreview_image"),
               ("model_server", "vllm_image"))


def manifest_path(ctx: Any) -> Path | None:
    workspace = getattr(ctx, "workspace", None)
    return Path(workspace) / "manifest.json" if workspace is not None else None


def write(path: Path, value: dict) -> None:
    # A unique temporary file per write also suits concurrent run workspaces.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2)
    os.replace(f.name, path)


def app_release() -> dict:
    """The app release this process runs: its tag and commit (no tag outside a release package)."""
    from .provenance import git_sha, release_version
    info = release_version()
    return {"tag": info.get("tag") or None, "sha": info.get("sha") or git_sha()}


def base(registry_release: str | None) -> dict:
    return {"app": app_release(), "registry_release": registry_release, "skills": []}


def init(ctx: Any, registry_release: str | None) -> None:
    path = manifest_path(ctx)
    if path is None:
        return
    with LOCK:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            write(path, base(registry_release))


def update(ctx: Any, change: Callable[[dict], bool | None]) -> None:
    """Apply ``change`` to the manifest (created when missing) and write it when ``change`` does
    not return False."""
    path = manifest_path(ctx)
    if path is None:
        return
    with LOCK:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.parent.mkdir(parents=True, exist_ok=True)
            doc = base(None)
        if change(doc) is not False:
            write(path, doc)


def _image(path: str) -> dict:
    from ..core.hpc3_assets import image_pin
    pin = image_pin(path) or {}
    return {"path": path, "name": pin.get("name"), "sha256": pin.get("sha256")}


def _model(repo: str, **extra: Any) -> dict:
    from ..core.hpc3_assets import model_pin
    pin = model_pin(repo) or {}
    return {"id": repo, "revision": pin.get("revision"), "files_sha256": pin.get("files_sha256"), **extra}


def record_environment(ctx: Any, settings: Any) -> None:
    """The images the session's HPC3 jobs run in, and its model server: called when a run starts."""
    try:
        images = {line: _image(getattr(settings, field)) for line, field in IMAGE_LINES
                  if getattr(settings, field, "")}
        server = _model(settings.vllm_model, role="session model server",
                        image=getattr(settings, "vllm_image", "") or None,
                        quantization=getattr(settings, "vllm_quantization", "") or None)

        def change(doc: dict) -> None:
            doc["images"] = images
            models = [m for m in doc.get("models", []) if m.get("role") != "session model server"]
            doc["models"] = [server, *models]
        update(ctx, change)
    except Exception as exc:  # noqa: BLE001 - the manifest is a record, never a reason to fail
        LOG.warning("manifest.json: environment not recorded: %s", exc)


def record_models(ctx: Any, calls: list[dict]) -> None:
    """The models the run's calls went to (``{"model", "provider"}`` each, from the usage table), with
    the pinned revision of each the list holds: called when the run ends."""
    try:
        used = []
        for call in calls:
            model = str(call.get("model") or "")
            if model and not any(m["id"] == model for m in used):
                used.append(_model(model, role="called", provider=call.get("provider") or None))

        def change(doc: dict) -> None:
            kept = [m for m in doc.get("models", []) if m.get("role") != "called"]
            doc["models"] = kept + used
        update(ctx, change)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("manifest.json: models not recorded: %s", exc)
