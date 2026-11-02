"""Versioned plan documents kept with a run's artifacts, independent of a live worker."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiscientist.agents.plan_doc import _load, parse_plan_yaml

_LOCKS: dict[str, Any] = {}
_LOCKS_GUARD = threading.Lock()
_VERSION_FILE = re.compile(r"v(\d+)\.yaml\Z")
# A version's file is its plan document, as written, then this line, then the record of the version
# (``_review``: what produced it and its state). The whole file is one YAML mapping. The card, the
# editor, the lab and report/plan.yaml get the document alone, its header comments included.
_RECORD_MARK = "# --- Saved by AiScientist with this version; not part of the plan. ---\n"


def lock(art: Path) -> Any:
    """Serialize a read/change/write transaction for one run, including nested store calls."""
    key = str(Path(art).resolve()).casefold()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".plan-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse(text: str, what: str) -> Any:
    """``text`` read as YAML; a ValueError when it does not parse (a hand edit can break it)."""
    try:
        return _load(text)
    except Exception as exc:  # noqa: BLE001 - yaml.YAMLError, or no PyYAML
        first = str(getattr(exc, "problem", None) or exc).split("\n", 1)[0]
        raise ValueError(f"{what} does not parse: {first}") from exc


def _document(text: str, data: Any = None) -> str:
    """The plan document of ``text`` without the record of its version. A file with no mark (edited
    by hand) that still holds a ``_review`` key is written out again without it."""
    head, mark, _ = text.partition(_RECORD_MARK)
    if mark:
        return head.rstrip("\n") + "\n"
    if data is None:
        data = _parse(text, "The plan")
    if isinstance(data, dict) and "_review" in data:
        import yaml

        rest = {k: v for k, v in data.items() if k != "_review"}
        return yaml.safe_dump(rest, allow_unicode=True, sort_keys=False)
    return text.rstrip("\n") + "\n"


def _pointer(folder: Path) -> dict[str, Any] | None:
    try:
        pointer = json.loads((folder / "current.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(pointer, dict):
        raise ValueError("Invalid saved plan pointer")
    return pointer


def current_state(art: Path) -> str | None:
    """The state the pointer recorded for the current version (``shown`` …), without reading the
    version: a scan of every run skips the plans nobody waits on. None when the pointer says none."""
    with lock(art):
        pointer = _pointer(Path(art) / "plan")
    state = (pointer or {}).get("state")
    return state if isinstance(state, str) else None


def load(art: Path) -> dict[str, Any] | None:
    """Read the current document afresh; edits to the file are authoritative. Raises ValueError for
    a version that cannot be read (a bad pointer, YAML that does not parse, no record)."""
    with lock(art):
        folder = Path(art) / "plan"
        pointer = _pointer(folder)
        if pointer is None:
            return None
        filename = pointer.get("filename", "")
        if not isinstance(filename, str) or not _VERSION_FILE.fullmatch(filename):
            raise ValueError("Invalid saved plan pointer")
        text = (folder / filename).read_text(encoding="utf-8")
        data = _parse(text, f"Saved plan {filename}")
        if not isinstance(data, dict) or not isinstance(data.get("_review"), dict):
            raise ValueError("Saved plan has no review metadata")
        return {**data["_review"], "yaml": _document(text, data), "filename": filename}


def save(art: Path, plan_yaml: str, *, source: str, state: str = "shown",
         kind: str = "agenda", payload: Any = None,
         prompt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Commit a full version before publishing its pointer or showing its card."""
    import yaml

    if source not in {"draft", "reply", "edit", "question"}:
        raise ValueError("Invalid saved plan source")
    if state not in {"shown", "approved", "cancelled"}:
        raise ValueError("Invalid saved plan state")
    if plan_yaml.strip():
        document = _document(plan_yaml)
    else:
        document = yaml.safe_dump({"plan": 1, "steps": []}, sort_keys=False)
    data = _parse(document, "The plan")
    if not isinstance(data, dict):
        raise ValueError("Saved plan must be a YAML mapping")
    with lock(art):
        folder = Path(art) / "plan"
        versions = [int(match.group(1)) for path in folder.glob("v*.yaml")
                    if (match := _VERSION_FILE.fullmatch(path.name))]
        version = max(versions, default=0) + 1
        filename = f"v{version:02d}.yaml"
        metadata = {"version": version, "source": source, "state": state,
                    "kind": kind, "payload": payload, "prompt": prompt,
                    "saved_at": datetime.now(timezone.utc).isoformat()}
        record = yaml.safe_dump({"_review": metadata}, allow_unicode=True, sort_keys=False)
        _write(folder / filename, document + _RECORD_MARK + record)
        if state == "approved":
            _write(Path(art) / "report" / "plan.yaml", document)
        _write(folder / "current.json", json.dumps({"version": version, "filename": filename,
                                                    "state": state}))
        return {**metadata, "yaml": document, "filename": filename}


def build_prompt(art: Path) -> dict[str, Any] | None:
    """Rebuild the waiting card from its current file, including after a restart."""
    current = load(art)
    if current is None or current["state"] != "shown":
        return None
    prompt = dict(current.get("prompt") or {})
    expected = prompt.pop("step_outputs", None)
    if current["kind"] == "clarify":
        prompt.update(type="plan_clarify", questions=current.get("payload") or [])
    else:
        data = _parse(current["yaml"], "The plan")
        doc = parse_plan_yaml(current["yaml"], request=str(data.get("request") or ""))
        steps = doc.to_public()
        # The outputs each step is expected to leave, as the lab listed them when this version was
        # shown (its skill's declared outputs). Kept for a step whose tool and skill are unchanged.
        for step in steps:
            known = expected.get(step["id"]) if isinstance(expected, dict) else None
            if (isinstance(known, dict) and known.get("outputs")
                    and known.get("tool") == step.get("tool") and known.get("skill") == step.get("skill")):
                step["outputs"] = known["outputs"]
        prompt.update(type="plan_prompt", agenda=doc.agenda(),
                      plan={"yaml": current["yaml"], "steps": steps,
                            "datasets": prompt.get("datasets", []),
                            "unusable_inputs": prompt.get("unusable_inputs", [])})
    return prompt
