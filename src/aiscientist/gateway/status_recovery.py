"""Preserve the prior collected evidence before a reviewed same-run status recovery."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .report_revision import confined


def archive(art: Path, state: dict, draft: dict, instructions: str) -> str:
    """Keep bounded, exact original process/plan records; never follow links into other runs."""
    source = confined(art, "process/run_state.json")
    if json.loads(source.read_text()) != state:
        raise ValueError("The original run changed while status recovery was being prepared; review it again.")
    names = {"process/run_state.json", "process/lab_result.json", "process/agenda.json",
             "report/plan.yaml", "report/plan.md", "report/report.md", "report/summary.md"}
    names.update(str(p.relative_to(art)) for p in (art / "process").glob("round_[0-9]*.json"))
    contents, total = {}, 0
    for name in sorted(names):
        path = confined(art, name)
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Original recovery evidence exceeds the per-file archive limit.")
        data = path.read_bytes()
        total += len(data)
        if total > 128 * 1024 * 1024:
            raise ValueError("Original recovery evidence exceeds the archive limit.")
        contents[name] = data
    identifier = uuid4().hex[:12]
    folder = confined(art, "process/status_recovery/" + identifier)
    folder.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name, data in contents.items():
        target = folder / "before" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    (folder / "request.txt").write_text(instructions)
    (folder / "draft-plan.yaml").write_text(draft["approved_plan_yaml"])
    (folder / "manifest.json").write_text(json.dumps({"state": "prepared_for_review",
        "source_run": art.parent.name, "sha256": hashes}, indent=2) + "\n")
    return identifier
