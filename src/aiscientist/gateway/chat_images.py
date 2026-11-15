"""Images in a chat (RCHENLAB/AiScientist issue 115): kept under the account, shown with the message,
and read by a model that accepts images.

An image is uploaded to its conversation before the message is sent (``POST
/api/conversations/{cid}/images``), so a paste or a drop shows a thumbnail at once and needs no HPC3
connection. The bytes are checked by their content, not by what the browser says they are: PNG, JPEG,
WebP and GIF only, at most ``MAX_IMAGE_BYTES`` each, at most ``MAX_IMAGES_PER_MESSAGE`` in a message.
Each is one file under the account's own folder::

    <data root>/users/<account>/chat-images/<conversation>/<image id>.<ext>      (or, with no data root,
    <results dir>/<account>/chat-images/<conversation>/...)  and a sidecar <image id>.json with its name

Where it lives and how long it stays: chat images belong to their conversation. They are not datasets,
so the datasets' retention period does not apply to them; deleting the chat deletes its images, and
nothing else removes them. A message keeps references (id, name, type, size, url), never the bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

MAX_IMAGES_PER_MESSAGE = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
_FILE_RE = re.compile(r"^([0-9a-f]{16})\.(png|jpg|webp|gif)$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class ChatImageError(ValueError):
    """An image that cannot be kept; the message is for the user."""


def sniff(data: bytes) -> "str | None":
    """The extension (png, jpg, webp, gif) the bytes are, from their first bytes, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _account(username: str) -> str:
    return "".join(c for c in (username or "") if c.isalnum() or c in "-_") or "guest"


def root_for(username: str) -> Path:
    """The folder of this account's chat images."""
    from . import dataset_storage
    account = _account(username)
    if dataset_storage.configured():
        return dataset_storage.user_dir(account) / "chat-images"
    results = os.environ.get("AISCIENTIST_RESULTS_DIR")
    if results:
        return Path(results) / account / "chat-images"
    return Path(__file__).resolve().parents[3] / "runs" / "console" / account / "chat-images"


def _dir(username: str, cid: int) -> Path:
    return root_for(username) / str(int(cid))


def url_of(cid: int, image_id: str, ext: str) -> str:
    return f"/api/chat-images/{int(cid)}/{image_id}.{ext}"


def _public(cid: int, image_id: str, meta: dict) -> dict:
    return {"id": image_id, "name": meta.get("name") or "image", "mime": _TYPES[meta["ext"]],
            "size": int(meta.get("size") or 0), "url": url_of(cid, image_id, meta["ext"])}


def save(username: str, cid: int, data: bytes, name: str = "") -> dict:
    """Keep one uploaded image for the conversation; returns its reference. Raises ChatImageError."""
    if not data:
        raise ChatImageError("The image is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ChatImageError(f"The image is {len(data) / 1048576:.1f} MB; the limit is "
                             f"{MAX_IMAGE_BYTES // 1048576} MB each.")
    ext = sniff(data)
    if ext is None:
        raise ChatImageError("Only PNG, JPEG, WebP and GIF images can be attached.")
    folder = _dir(username, cid)
    folder.mkdir(parents=True, exist_ok=True)
    image_id = uuid.uuid4().hex[:16]
    clean = re.sub(r"[^\w .()-]", "_", os.path.basename(name or ""))[:120].strip() or f"image.{ext}"
    meta = {"name": clean, "ext": ext, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "created": time.time()}
    (folder / f"{image_id}.{ext}").write_bytes(data)
    (folder / f"{image_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    return _public(cid, image_id, meta)


def _meta(folder: Path, image_id: str) -> "dict | None":
    if not _ID_RE.match(str(image_id)):
        return None
    try:
        meta = json.loads((folder / f"{image_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ext = meta.get("ext")
    if ext not in _TYPES or not (folder / f"{image_id}.{ext}").is_file():
        return None
    return meta


def path_of(username: str, cid: int, filename: str) -> "tuple[Path, str] | None":
    """The file and its media type for ``<id>.<ext>`` of this account's conversation, or None."""
    m = _FILE_RE.match(filename or "")
    if not m:
        return None
    path = _dir(username, cid) / filename
    return (path, _TYPES[m.group(2)]) if path.is_file() else None


def resolve(username: str, cid: int, refs: Any) -> list[dict]:
    """The trusted references for the image ids a message names (each an id, or a dict with an ``id``):
    only images uploaded to this conversation by this account, at most ``MAX_IMAGES_PER_MESSAGE``.
    Raises ChatImageError for an unknown id or too many."""
    if not isinstance(refs, list):
        raise ChatImageError("images must be a list")
    ids = []
    for r in refs:
        image_id = str(r.get("id") if isinstance(r, dict) else r)
        if image_id not in ids:
            ids.append(image_id)
    if len(ids) > MAX_IMAGES_PER_MESSAGE:
        raise ChatImageError(f"At most {MAX_IMAGES_PER_MESSAGE} images per message.")
    folder = _dir(username, cid)
    out = []
    for image_id in ids:
        meta = _meta(folder, image_id)
        if meta is None:
            raise ChatImageError("One of the images is no longer available. Attach it again.")
        out.append(_public(cid, image_id, meta))
    return out


def load(username: str, cid: int, refs: Any) -> list[dict]:
    """The images as the model path wants them: ``{"name", "media_type", "data"}`` (``data`` the bytes)."""
    out = []
    for ref in resolve(username, cid, refs):
        ext = ref["url"].rsplit(".", 1)[-1]
        data = (_dir(username, cid) / f"{ref['id']}.{ext}").read_bytes()
        out.append({"name": ref["name"], "media_type": ref["mime"], "data": data})
    return out


def data_url(image: dict) -> str:
    return f"data:{image['media_type']};base64,{base64.b64encode(image['data']).decode('ascii')}"


def remove_conversation(username: str, cid: int) -> int:
    """Delete a conversation's images (with the conversation). Returns how many files went."""
    folder = _dir(username, cid)
    if not folder.is_dir():
        return 0
    n = sum(1 for _ in folder.iterdir())
    shutil.rmtree(folder, ignore_errors=True)
    return n


# --- which models read images --------------------------------------------------------------------------

#: Models whose names say they accept images, for an endpoint that does not say so itself. Override
#: with AISCIENTIST_VISION_MODELS: a comma list of name fragments, or ``all`` / ``none``.
_VISION_NAME = re.compile(
    r"(?i)(gpt-4o|gpt-4\.1|gpt-4-turbo|gpt-5|(?<![a-z])o[134](?:-|$)|gemini|claude|llava|vision|"
    r"(?<![a-z0-9])vl(?![a-z])|[-_]vl[-_.0-9]|pixtral|gemma-?3|minicpm-?v|internvl|molmo|grok-[0-9.]*-?vision)")


def accepts_images(model: str, *, native_anthropic: bool = False, env: "dict[str, str] | None" = None) -> bool:
    """Whether the session's model reads images. A native Anthropic key does; any other endpoint by its
    model's name or the deployment's AISCIENTIST_VISION_MODELS."""
    setting = ((env if env is not None else os.environ).get("AISCIENTIST_VISION_MODELS") or "").strip().lower()
    if setting == "none":
        return False
    if setting == "all" or native_anthropic:
        return True
    if setting:
        return any(part.strip() and part.strip() in (model or "").lower() for part in setting.split(","))
    return bool(_VISION_NAME.search(model or ""))
