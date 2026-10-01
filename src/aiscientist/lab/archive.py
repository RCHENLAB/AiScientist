"""DRAFT — the Lab Archive: durable, append-only, resumable store for a multi-agent lab.

Proposed for the week-of-2026-06-22 discussion (HANDOFF "2026-06-18 (later)"). NOT wired
into the gateway. The point: canonical lab state lives OFF the ephemeral compute node (pass
a durable ``root``, e.g. on dfs3b / eye-server ``/data``), so each expert's memory + meeting
transcripts + artifacts survive an ``srun`` reclaim, a gateway restart, or a week between
sessions. Compute jobs become stateless workers: read inputs from here, write outputs back.

Principles:
- Off-node single source of truth (durable ``root``).
- Append-only logs (memory / transcripts / events) — a crash loses at most the last line.
- Atomic whole-file writes (manifest / checkpoints) via write-temp + ``os.replace``.
- Independent per-agent memory (``agents/<id>/memory.jsonl``) — no shared context.
- Schema-versioned manifest; resume = rehydrate from the latest checkpoint.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _now() -> float:
    return time.time()


def _atomic_write(path: Path, text: str) -> None:
    """Whole-file write that can't leave a half-written file (write temp + atomic rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    """Append one JSON line + fsync — a Slurm reclaim loses at most this single in-flight line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class LabArchive:
    """One research project's durable record, rooted at ``<root>/labs/<lab_id>/``."""

    def __init__(self, root: str | Path, lab_id: str) -> None:
        self.lab_id = lab_id
        self.root = Path(root) / "labs" / lab_id
        for sub in ("", "agents", "meetings", "checkpoints", "artifacts"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # -- manifest / agenda / team --------------------------------------------
    def init_manifest(self, question: str) -> None:
        _atomic_write(self.root / "manifest.json", json.dumps({
            "lab_id": self.lab_id, "question": question, "status": "new",
            "schema_version": SCHEMA_VERSION, "created": _now(), "updated": _now(),
        }, ensure_ascii=False, indent=2))

    def load_manifest(self) -> dict[str, Any]:
        p = self.root / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def set_status(self, status: str) -> None:
        m = self.load_manifest()
        m["status"] = status
        m["updated"] = _now()
        _atomic_write(self.root / "manifest.json", json.dumps(m, ensure_ascii=False, indent=2))

    def write_agenda(self, agenda: list[str]) -> None:
        _atomic_write(self.root / "agenda.json", json.dumps({"agenda": agenda}, ensure_ascii=False, indent=2))

    def write_team(self, team: list[dict[str, Any]]) -> None:
        _atomic_write(self.root / "team.json", json.dumps({"team": team}, ensure_ascii=False, indent=2))

    # -- global event log -----------------------------------------------------
    def append_event(self, etype: str, **data: Any) -> None:
        _append_jsonl(self.root / "events.jsonl", {"ts": _now(), "type": etype, **data})

    # -- independent per-agent memory ----------------------------------------
    def append_memory(self, agent_id: str, role: str, content: str, **meta: Any) -> None:
        _append_jsonl(self.root / "agents" / agent_id / "memory.jsonl",
                      {"ts": _now(), "role": role, "content": content, **meta})

    def read_memory(self, agent_id: str) -> list[dict[str, str]]:
        """Rehydrate this agent's OWN prior context as chat messages (role/content only)."""
        return [{"role": r["role"], "content": r["content"]}
                for r in _read_jsonl(self.root / "agents" / agent_id / "memory.jsonl")]

    # -- meetings -------------------------------------------------------------
    def write_meeting(self, meeting_id: str, meta: dict[str, Any]) -> None:
        _atomic_write(self.root / "meetings" / meeting_id / "meeting.json",
                      json.dumps({**meta, "ts": _now()}, ensure_ascii=False, indent=2))

    def append_turn(self, meeting_id: str, speaker: str, content: str, **meta: Any) -> None:
        _append_jsonl(self.root / "meetings" / meeting_id / "transcript.jsonl",
                      {"ts": _now(), "speaker": speaker, "content": content, **meta})

    def write_summary(self, meeting_id: str, markdown: str) -> None:
        _atomic_write(self.root / "meetings" / meeting_id / "summary.md", markdown)

    # -- checkpoints (resume) -------------------------------------------------
    def write_checkpoint(self, seq: int, state: dict[str, Any]) -> None:
        _atomic_write(self.root / "checkpoints" / f"{seq:06d}.json",
                      json.dumps({"seq": seq, "ts": _now(), "state": state}, ensure_ascii=False))

    def latest_checkpoint(self) -> tuple[int, dict[str, Any]] | None:
        """Newest (seq, state) for resume, or None if the lab hasn't checkpointed yet."""
        files = sorted((self.root / "checkpoints").glob("*.json"))
        if not files:
            return None
        doc = json.loads(files[-1].read_text(encoding="utf-8"))
        return int(doc["seq"]), doc["state"]

    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"
