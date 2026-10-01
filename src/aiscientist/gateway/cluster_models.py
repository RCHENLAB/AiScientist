"""The cluster models: which weight sets a session can serve on the HPC3 GPU, and how to serve each.

A model is more than a repo id. Qwen3.8-27B-INT4 needs ``vllm-0.28.0.sif`` with the quantization
auto-detected; Qwen3.6-35B-A3B-AWQ needs the June ``vllm.sif`` with ``--quantization awq_marlin``.
Each entry carries its own image, quantization and extra args, and :func:`apply` writes them onto a
session's :class:`HPCSettings` before its serve job is written.

The list is shared lab-wide and admin-managed, and lives at
``<AISCIENTIST_STATE_DIR>/cluster_models.json`` next to the SSH and LLM credential stores. Until an
admin saves it, it is seeded from the env (``AISCIENTIST_VLLM_*`` = the default entry) plus
:data:`KNOWN`, so a fresh deploy shows the models that are actually staged. Removing an entry
removes it from this list only. The weights stay in the HF cache; deleting 20 GB of weights from a
web button is not a mistake worth making possible.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .settings import SHARED_ROOT, HPCSettings

_LOCK = threading.Lock()

# Recipes for weights we have served and measured on HPC3 (numbers: gateway/settings.py). Used to
# seed the list and to pre-fill the form when an admin registers weights found in the HF cache.
KNOWN: dict[str, dict[str, str]] = {
    "RedHatAI/Qwen3.8-27B-INT4": {
        "label": "Qwen3.8-27B INT4",
        "image": f"{SHARED_ROOT}/containers/vllm-0.28.0.sif",
        "quantization": "",
        "notes": "Dense 27B + vision. ~70-75 tok/s single-stream, 2.0M KV tokens on RTX6000.",
    },
    "QuantTrio/Qwen3.6-35B-A3B-AWQ": {
        "label": "Qwen3.6-35B-A3B AWQ",
        "image": f"{SHARED_ROOT}/containers/vllm.sif",
        "quantization": "awq_marlin",
        "notes": "MoE, 3B active. ~2.3x faster decode than 3.8-27B; the previous default.",
    },
}


@dataclass
class ClusterModel:
    id: str
    repo: str
    label: str = ""
    image: str = ""
    quantization: str = ""
    extra_args: str = ""
    # GPU types this model may run on, e.g. "RTX6000,A100" ("" = any card in the race). Switching a
    # live session keeps its card when the card is one of these, and releases it otherwise.
    cards: str = ""
    notes: str = ""
    enabled: bool = True
    default: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


def slug(repo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-") or "model"


def _path() -> Path:
    return Path(os.environ.get("AISCIENTIST_STATE_DIR", ".")) / "cluster_models.json"


def _from_dict(d: dict[str, Any]) -> ClusterModel:
    names = {f.name for f in fields(ClusterModel)}
    m = ClusterModel(**{"id": "", "repo": "", **{k: v for k, v in d.items() if k in names}})
    m.id = m.id or slug(m.repo)
    return m


def _seed(settings: HPCSettings) -> list[ClusterModel]:
    base = KNOWN.get(settings.vllm_model, {})
    out = [ClusterModel(id=slug(settings.vllm_model), repo=settings.vllm_model,
                        label=base.get("label") or settings.vllm_model.rsplit("/", 1)[-1],
                        image=settings.vllm_image, quantization=settings.vllm_quantization,
                        extra_args=settings.vllm_extra_args, notes=base.get("notes", ""), default=True)]
    for repo, r in KNOWN.items():
        if repo != settings.vllm_model:
            out.append(ClusterModel(id=slug(repo), repo=repo, **r))
    return out


def _normalise(models: list[ClusterModel]) -> list[ClusterModel]:
    """Exactly one default, and it is enabled. The first enabled entry takes over otherwise."""
    enabled = [m for m in models if m.enabled]
    defaults = [m for m in enabled if m.default]
    keep = defaults[0] if defaults else (enabled[0] if enabled else None)
    for m in models:
        m.default = m is keep
    return models


def load(settings: HPCSettings | None = None) -> list[ClusterModel]:
    p = _path()
    if p.exists():
        try:
            return _normalise([_from_dict(d) for d in json.loads(p.read_text()).get("models", [])])
        except (OSError, ValueError, TypeError):
            pass  # an unreadable file must not take the connect path down; fall back to the seed
    return _normalise(_seed(settings or HPCSettings.from_env()))


def save(models: list[ClusterModel]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"models": [m.public() for m in _normalise(models)]}, indent=1))
    os.replace(tmp, p)


def get(model_id: str | None, settings: HPCSettings | None = None) -> ClusterModel | None:
    """The entry for ``model_id``, or the default when it is empty. None = unknown or disabled."""
    models = load(settings)
    if not model_id:
        return next((m for m in models if m.default), None)
    m = next((m for m in models if m.id == model_id), None)
    return m if m and m.enabled else None


def upsert(data: dict[str, Any]) -> ClusterModel:
    repo = str(data.get("repo") or "").strip()
    if not repo or "/" not in repo:
        raise ValueError("repo must be a Hugging Face id like org/name")
    with _LOCK:
        models = load()
        new = _from_dict({**KNOWN.get(repo, {}), **{k: v for k, v in data.items() if v is not None},
                          "repo": repo})
        new.id = str(data.get("id") or "").strip() or slug(repo)
        new.label = new.label.strip() or repo.rsplit("/", 1)[-1]
        new.image = new.image.strip() or f"{SHARED_ROOT}/containers/vllm-0.28.0.sif"
        if new.default:
            for m in models:
                m.default = False
        old = next((i for i, m in enumerate(models) if m.id == new.id), None)
        if old is None:
            models.append(new)
        else:
            models[old] = new
        save(models)
        return next(m for m in load() if m.id == new.id)


def remove(model_id: str) -> bool:
    with _LOCK:
        models = load()
        keep = [m for m in models if m.id != model_id]
        if len(keep) == len(models):
            return False
        if not any(m.enabled for m in keep):
            raise ValueError("cannot remove the last enabled model")
        save(keep)
        return True


def set_default(model_id: str) -> ClusterModel:
    with _LOCK:
        models = load()
        target = next((m for m in models if m.id == model_id), None)
        if target is None:
            raise KeyError(model_id)
        for m in models:
            m.default = m is target
        target.enabled = True
        save(models)
        return target


def apply(settings: HPCSettings, m: ClusterModel) -> HPCSettings:
    """Point a session's serve job at ``m``: repo, image, quantization and extra args together.
    Setting only the repo is how you get the 3.8 weights handed to a vLLM that cannot read them."""
    settings.vllm_model = m.repo
    settings.vllm_image = m.image or settings.vllm_image
    settings.vllm_quantization = m.quantization
    settings.vllm_extra_args = m.extra_args
    settings.vllm_gpu_cards = m.cards
    # A new job for a card-restricted model races only the candidates it can run on. Filtered from
    # the ORIGINAL list each time, so switching back to an unrestricted model restores all of them.
    base = getattr(settings, "_base_gpu_candidates", None)
    if base is None:
        base = settings.gpu_candidates
        settings._base_gpu_candidates = base  # type: ignore[attr-defined]
    settings.gpu_candidates = _filter_candidates(base, m.cards)
    return settings


def _filter_candidates(candidates: str, cards: str) -> str:
    """Keep the race candidates whose gres TYPE is one of ``cards``. Untyped gres (``gpu:1``) stay —
    the card they land on is checked when it matters. If nothing would be left, keep them all
    rather than make the model unservable; the in-place switch still checks the actual card."""
    allowed = {c.strip().upper() for c in (cards or "").split(",") if c.strip()}
    if not allowed or not candidates:
        return candidates
    kept = []
    for chunk in [c.strip() for c in candidates.split(";") if c.strip()]:
        parts = [p.strip() for p in chunk.split(",")]
        gres = parts[1] if len(parts) > 1 else ""
        bits = gres.split(":")
        gtype = bits[1].upper() if len(bits) >= 3 else ""
        if not gtype or gtype in allowed:
            kept.append(chunk)
    return ";".join(kept) if kept else candidates


def scan_disk(executor, settings: HPCSettings) -> dict[str, float]:
    """``{repo: size_gb}`` for every model in the shared HF cache. One short login-node command
    (``ls`` + ``du`` of a few directories), no transfer, so it respects RCIC's login-node rules."""
    hub = f"{settings.hf_home}/hub"
    res = executor.exec(f"cd {hub} 2>/dev/null && for d in models--*; do "
                        f'[ -d "$d" ] && echo "$d $(du -sk "$d" 2>/dev/null | cut -f1)"; done', timeout=60)
    out: dict[str, float] = {}
    for line in (getattr(res, "out", "") or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].startswith("models--"):
            continue
        repo = parts[0][len("models--"):].replace("--", "/", 1)
        try:
            out[repo] = round(int(parts[1]) / 1024 / 1024, 1)
        except ValueError:
            continue
    return out
