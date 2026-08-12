"""Real-LLM demo of per-agent EVOLVING memory (Axis C) across TWO runs, via OpenRouter.

Run 1: the QC expert acts, the real Critic judges, an episode is written, and a REAL reflection call
(OpenRouter) distils it into a lesson on disk. Run 2 (same persistent memory dir): the QC expert
RECALLS that lesson into its brief before acting. Frozen weights — this is in-context learning via
retrieval, not fine-tuning. The Scientist is a trivial offline tool so only the memory pipe is tested.

Run:  PYTHONPATH=src .venv/bin/python scripts/dag_memory_openrouter.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv(p: Path) -> None:
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")
OR_KEY = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("BIOAGENT_LLM_API_KEY")
MODEL = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.6-35b-a3b")
if not OR_KEY:
    print("OPENROUTER_API_KEY not set — cannot run.")
    sys.exit(2)

from bioagent.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from bioagent.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from bioagent.providers.openai_compatible import OpenRouterClient  # noqa: E402

_CLIENT = OpenRouterClient(model=MODEL, api_key=OR_KEY, reasoning_effort="none", timeout_seconds=90)
BRIEFS: list[str] = []


def or_complete(messages):
    return _CLIENT.chat(messages, max_tokens=1200, temperature=0.2).content


def scientist():
    note = HarnessTool("note", "record what you did", {"type": "object",
                       "properties": {"what": {"type": "string"}}}, lambda a, c: {"status": "ok"},
                       category="analysis")

    def chat(messages, tools):
        BRIEFS.append(" ".join(str(m.get("content", "")) for m in (messages or [])))
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "did minimal QC"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function",
                "function": {"name": "note", "arguments": json.dumps({"what": "ran QC with defaults"})}}]}

    return ResearchHarness(catalog=[note], chat_fn=chat)


def main() -> None:
    print(f"Model: {MODEL}\n")
    mem = Path(tempfile.mkdtemp(prefix="dag_mem_"))
    cfg = LabConfig(planner="dag", auto_select_skill=False, max_revisions=1,
                    agent_memory=True, agent_memory_dir=str(mem))
    q = "Run scanpy QC on a retina single-cell dataset and report the metrics."

    def _run(tag):
        BRIEFS.clear()
        ev = []
        ResearchLab(HarnessContext(decisions={}, tunnel_port=1, model=MODEL),
                    cfg, complete_fn=or_complete, scientist=scientist()).run(q, on_event=ev.append)
        reads = [e for e in ev if e["type"] == "memory_read"]
        reflects = [e for e in ev if e["type"] == "memory_reflect"]
        print(f"[{tag}] memory_read={len(reads)}  memory_reflect={len(reflects)}")
        return reads, reflects

    print("=== RUN 1 (cold: no memory yet) ===")
    _run("run1")
    lessons = list(mem.glob("*/lessons.md"))
    print("\nLessons distilled to disk after run 1:")
    for lp in lessons:
        print(f"  {lp.parent.name}/lessons.md:")
        for line in lp.read_text(encoding="utf-8").splitlines():
            print(f"    {line}")

    print("\n=== RUN 2 (warm: same memory dir) ===")
    reads2, _ = _run("run2")
    recalled = any("PRIVATE memory" in b for b in BRIEFS)
    print(f"\nRun-2 expert brief carried recalled memory: {recalled}")

    ok = bool(lessons) and reads2 and recalled
    print("\nRESULT:", "PASS — memory persisted, evolved (real reflection), and was recalled next run"
          if ok else "CHECK OUTPUT ABOVE")
    print(f"(memory under {mem})")


if __name__ == "__main__":
    main()
