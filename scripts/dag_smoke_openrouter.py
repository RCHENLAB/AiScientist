"""Real-LLM smoke test for the DAG planner (branch feat/dag-planner).

Validates that the NEW prompts — the agenda→DAG structure pass and the Coordinator — actually
produce a parseable dependency DAG and valid scheduling choices with a real Qwen3.6 model (via
OpenRouter), not just injected fakes. The Scientist uses a trivial offline tool (no scanpy/data), so
this exercises ONLY the orchestration LLM calls: PI plan → DAG structure → Coordinator → Critic →
synthesize.

Run:  PYTHONPATH=src .venv/bin/python scripts/dag_smoke_openrouter.py
Reads OPENROUTER_API_KEY / OPENROUTER_MODEL from .env (or the environment).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")

from bioagent.agents.research_harness import HarnessContext, HarnessTool, ResearchHarness  # noqa: E402
from bioagent.agents.research_lab import LabConfig, ResearchLab  # noqa: E402
from bioagent.providers.openai_compatible import OpenRouterClient  # noqa: E402


def make_complete_fn():
    client = OpenRouterClient(reasoning_effort="none", timeout_seconds=90)
    if not client.available:
        print("OPENROUTER_API_KEY not set — cannot run the real-LLM smoke test.")
        sys.exit(2)
    print(f"Using OpenRouter model: {client.model}\n")
    calls = {"n": 0}

    def complete(messages):
        calls["n"] += 1
        resp = client.chat(messages, max_tokens=1500, temperature=0.2)
        return resp.content

    return complete, calls


def fake_scientist():
    """A Scientist that calls one trivial offline tool, then finishes — no scanpy/data needed."""
    def _note_exec(args, _ctx):
        return {"status": "ok", "note": str(args.get("what", "did the task"))}

    note = HarnessTool(
        "note", "Record what you did for this task.",
        {"type": "object", "properties": {"what": {"type": "string"}}, "required": ["what"]},
        _note_exec, category="analysis",
    )

    def chat(messages, tools):
        if any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"id": "f", "type": "function",
                    "function": {"name": "finish", "arguments": json.dumps({"answer": "task done"})}}]}
        return {"content": "", "tool_calls": [{"id": "t", "type": "function",
                "function": {"name": "note", "arguments": json.dumps({"what": "did the task"})}}]}

    return ResearchHarness(catalog=[note], chat_fn=chat)


def main() -> None:
    complete, calls = make_complete_fn()
    # A pre-annotated dataset (majorclass/celltype in obs) — surfaces the annotation callout so the PI
    # plans an annotation step and the structure pass can flag it as a human decision point.
    ctx = HarnessContext(decisions={"dataset_result": {
        "cells": 11977, "genes": 36601,
        "obs_categoricals": {"majorclass": {"n": 6, "values": ["AC", "BC", "Cone", "HC", "MG", "Rod"]},
                             "celltype": {"n": 66, "values": []}},
        "obs_keys": ["majorclass", "celltype", "leiden"],
    }}, tunnel_port=1, model="smoke")
    # max_revisions=0 isolates SCHEDULING: the fake Scientist does no real work, so a real Critic
    # would reject every node and the revisions would exhaust the round budget before the branch is
    # reached. With revisions off, each node runs once and advances, so the branch is scheduled and
    # the Coordinator is exercised. (Real runs use a real Scientist that gets accepted.)
    lab = ResearchLab(ctx, LabConfig(planner="dag", auto_select_skill=False, max_revisions=0,
                                     multi_agent=True),   # experts CLAIM nodes by expertise
                      complete_fn=complete, scientist=fake_scientist())

    question = (
        "Analyze this retina scRNA-seq dataset: run QC, cluster the cells, annotate the cell types, "
        "identify marker genes per cell class, run pathway enrichment on the markers, and separately "
        "search the published literature for the cell-type markers. Keep it to about 5-6 steps."
    )

    # HITL: auto-answer any decision point (a human would click a chip). Counts invocations so we can
    # confirm the pause+inject path fired end-to-end with the real model.
    decisions = {"n": 0}

    def decision_review(node):
        decisions["n"] += 1
        choice = node.options[0] if node.options else "Use the existing cell-type annotations"
        print(f"\n🔀 Decision point hit: {node.goal[:70]}\n   options={list(node.options)}\n   → auto-choosing: {choice}")
        return {"action": "proceed", "choice": choice}

    events: list[dict] = []
    plan_nodes = {"v": None}

    def on_event(ev):
        events.append(ev)
        t = ev.get("type")
        if t == "pi_agenda":
            print("PI agenda:")
            for s in ev.get("agenda", []):
                print(f"   - {s}")
            print()
        elif t == "lab_plan_dag":
            plan_nodes["v"] = ev.get("nodes")
            print("Structured DAG:")
            for n in ev.get("nodes", []):
                dep = f"  depends_on={n['depends_on']}" if n.get("depends_on") else "  (root)"
                print(f"   [{n['id']}] {n['goal'][:70]}{dep}")
            print()
        elif t == "coordinator_pick":
            print(f"Coordinator picked {ev['next']} from ready={ev['ready']}")
        elif t == "node_claim":
            print(f"   🙋 {ev.get('specialist')} claimed {ev.get('node')}")
        elif t == "decision_made":
            print(f"   ✅ decision recorded: {ev.get('choice')}")
        elif t == "lab_done":
            print(f"\nlab_done: converged={ev.get('converged')} accepted={ev.get('accepted_steps')}/{ev.get('agenda')}")

    result = lab.run(question, on_event=on_event, decision_review=decision_review)

    print("\n" + "=" * 70)
    print(f"LLM calls: {calls['n']}")
    print(f"Rounds executed (schedule order): {[r.step[:50] for r in result.rounds]}")
    nodes = plan_nodes["v"] or []
    has_dep = any(n.get("depends_on") for n in nodes)
    # a real branch = two nodes sharing a dependency (both ready at once)
    dep_targets = [tuple(n.get("depends_on", [])) for n in nodes if n.get("depends_on")]
    branched = len(dep_targets) != len({d for d in dep_targets}) or (
        any(list(dep_targets).count(d) > 1 for d in dep_targets))
    coordinated = any(e["type"] == "coordinator_pick" for e in events)
    n_decision_nodes = sum(1 for n in nodes if n.get("decision"))
    print(f"DAG has real dependencies: {has_dep}")
    print(f"DAG has an independent branch (>1 node ready at once): {branched}")
    print(f"Coordinator was invoked (chose among a branch): {coordinated}")
    print(f"Structure pass flagged decision node(s): {n_decision_nodes}")
    print(f"HITL decision_review fired: {decisions['n']} time(s)")
    claims = [(e["node"], e["specialist"]) for e in events if e["type"] == "node_claim"]
    print(f"Multi-agent claims ({len(claims)}): " + ", ".join(f"{n}→{s.split()[0]}" for n, s in claims))
    print(f"Converged + synthesized: {result.converged}, report chars={len(result.final_answer or '')}")
    ok = bool(nodes) and has_dep and result.final_answer
    print("\nRESULT:", "PASS" if ok else "CHECK OUTPUT ABOVE")


if __name__ == "__main__":
    main()
