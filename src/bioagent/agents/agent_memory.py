"""Per-agent isolated + evolving memory (Axis C) — see docs/agent_memory_design.md.

Two tiers, both on the GATEWAY (eyeserver) disk — NOT in the ephemeral HPC3/Singularity containers:
  • long-term (disk): ``<root>/<agent_id>/episodes.jsonl`` (append-only) + ``lessons.md`` (distilled).
  • working (context): the caller injects a RETRIEVED slice into the model prompt each turn.

The store is PRIVATE per agent (isolation), PERSISTS across runs, and EVOLVES: ``reflect`` distils raw
episodes into a compact lesson set (semantic compression). Model weights are frozen — this is
in-context learning via retrieval, not fine-tuning. Every method is best-effort and NEVER raises: a
memory failure must not break a run.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

_WORD = re.compile(r"[a-z0-9]+")
_MAX_LESSONS_CHARS = 1200
_MAX_EPISODES_KEPT = 500          # per agent, before older raw episodes roll off (archived, not lost)


def slug_agent_id(name: str) -> str:
    """Stable, filesystem-safe id for an agent role (its Specialist name)."""
    s = re.sub(r"[^a-z0-9]+", "_", (name or "agent").lower()).strip("_")
    return s or "agent"


class AgentMemory:
    """Disk-backed private memory for each agent role, rooted at a PERSISTENT dir (outside any per-run
    workspace, so it survives across runs). Keyed by agent id under ``root/<agent_id>/``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # -- paths --------------------------------------------------------------
    def _dir(self, agent_id: str) -> Path:
        return self.root / slug_agent_id(agent_id)

    # -- write --------------------------------------------------------------
    def write_episode(self, agent_id: str, episode: dict[str, Any]) -> None:
        """Append one episode (what the agent did + how it was judged). Best-effort."""
        try:
            d = self._dir(agent_id)
            d.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.time(), **episode}
            with (d / "episodes.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - memory is best-effort; never break the run
            pass

    # -- read ---------------------------------------------------------------
    def _load_episodes(self, agent_id: str) -> list[dict[str, Any]]:
        try:
            p = self._dir(agent_id) / "episodes.jsonl"
            if not p.exists():
                return []
            out = []
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return out
        except Exception:  # noqa: BLE001
            return []

    def _load_lessons(self, agent_id: str) -> str:
        try:
            p = self._dir(agent_id) / "lessons.md"
            return p.read_text(encoding="utf-8").strip() if p.exists() else ""
        except Exception:  # noqa: BLE001
            return ""

    def read(self, agent_id: str, query: str, *, max_episodes: int = 3,
             max_chars: int = 1600) -> str:
        """Retrieve this agent's PRIVATE memory as a prompt block: its distilled lessons (always) +
        the most RELEVANT recent episodes (keyword overlap with ``query``, recency as tiebreak).
        Returns "" when the agent has no memory yet (graceful cold start)."""
        lessons = self._load_lessons(agent_id)
        episodes = self._load_episodes(agent_id)
        if not lessons and not episodes:
            return ""
        terms = {t for t in _WORD.findall((query or "").lower()) if len(t) > 3}

        def _score(ep: dict[str, Any]) -> int:
            hay = " ".join(str(ep.get(k, "")) for k in ("node", "action", "outcome")).lower()
            return sum(1 for t in terms if t in hay)

        # recency = append order; stable sort by score keeps recent-first within equal score
        ranked = sorted(enumerate(episodes), key=lambda iv: (_score(iv[1]), iv[0]), reverse=True)
        picked = [ep for _, ep in ranked[:max_episodes]]

        lines = ["Your PRIVATE memory from past work (advisory — steer, don't override the data):"]
        if lessons:
            lines.append("Lessons you've learned:\n" + lessons[:_MAX_LESSONS_CHARS])
        if picked:
            lines.append("Relevant past episodes:")
            for ep in picked:
                verdict = ep.get("outcome") or ep.get("verdict") or ""
                lines.append(f"  - {str(ep.get('node', ''))[:50]}: {str(ep.get('action', ''))[:90]}"
                             + (f" → {verdict}" if verdict else ""))
        block = "\n".join(lines)
        return block[:max_chars]

    # -- evolve (reflection = semantic compression) -------------------------
    def reflect(self, agent_id: str, complete_fn: Callable[[list[dict[str, str]]], str],
                *, role_hint: str = "") -> bool:
        """Distil this agent's raw episodes into an UPDATED, compact ``lessons.md`` (methodological
        heuristics only — no dataset numbers). Returns True if lessons were rewritten. Best-effort;
        cross-RUN learning, not in-step correction. Keeps raw episodes (never deletes them)."""
        episodes = self._load_episodes(agent_id)
        if not episodes:
            return False
        recent = episodes[-40:]
        digest = "\n".join(
            f"- {str(ep.get('node', ''))[:50]}: {str(ep.get('action', ''))[:120]}"
            f" → {ep.get('outcome') or ep.get('verdict') or '?'}"
            f"{(' | ' + str(ep.get('note'))[:120]) if ep.get('note') else ''}"
            for ep in recent)
        existing = self._load_lessons(agent_id)
        try:
            updated = complete_fn([
                {"role": "system", "content": _REFLECT_SYSTEM},
                {"role": "user", "content":
                    (f"Agent role: {role_hint}\n\n" if role_hint else "")
                    + f"Existing lessons:\n{existing or '(none yet)'}\n\n"
                    f"Recent episodes (what it did → how the reviewer judged it):\n{digest}\n\n"
                    "Return the UPDATED lessons as concise markdown bullets."},
            ])
        except Exception:  # noqa: BLE001 - reflection is best-effort
            return False
        updated = (updated or "").strip()
        if not updated:
            return False
        try:
            d = self._dir(agent_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "lessons.md").write_text(updated[:_MAX_LESSONS_CHARS * 2], encoding="utf-8")
            return True
        except Exception:  # noqa: BLE001
            return False


_REFLECT_SYSTEM = (
    "You maintain the PRIVATE lessons of one specialist agent in a bioinformatics research lab. Given "
    "its past episodes (what it did and how the reviewer judged the result), distil and UPDATE a SHORT "
    "list of reusable METHODOLOGICAL lessons — heuristics that help it do better next time (e.g. good "
    "default thresholds, pitfalls to check, what the reviewer tends to reject). Merge with the existing "
    "lessons, remove duplicates, keep it concise (at most ~10 bullets). METHODOLOGY ONLY: never include "
    "dataset-specific numbers, donor/sample ids, or one-off details — those don't transfer. Reply with "
    "ONLY the updated lessons as markdown bullet points."
)
