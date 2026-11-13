"""Curated marker references shared by report validation and legacy annotation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REFERENCES = Path(__file__).with_name("marker_references")

def available_references() -> list[str]:
    """The tissues a curated reference panel exists for (``reference`` values)."""
    return sorted(p.stem for p in _REFERENCES.glob("*.json"))


def load_reference(name: str) -> "dict[str, Any] | None":
    path = _REFERENCES / f"{name}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
