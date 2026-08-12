#!/usr/bin/env python3
"""Run the multi-agent research lab on a real dataset via OpenRouter's Qwen3.6.

OpenRouter stands in for the HPC3 vLLM — both are OpenAI-compatible /v1, and the
model (``qwen/qwen3.6-35b-a3b``) is the SAME family we serve on the cluster — so
this realistically simulates the production signal without needing the GPU.

Flow: load the .h5ad into a derived ``dataset_result`` (h5py preflight — never the
raw matrix into the prompt) → drive ``ResearchLab`` (PI → Scientist → Critic →
converge) with OpenRouter as the LLM and a real ``CodeSandbox`` for run_code →
stream every event, then print + save the result.

Needs ``OPENROUTER_API_KEY`` (read from ./.env). Usage:
    PYTHONPATH=src python scripts/lab_openrouter_demo.py --dataset Ddx41_DEG.h5ad
    PYTHONPATH=src python scripts/lab_openrouter_demo.py --dataset f.h5ad --model qwen/qwen3.6-35b-a3b --rounds 4
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bioagent.agents.research_harness import HarnessContext, ResearchHarness, default_catalog  # noqa: E402
from bioagent.agents.research_lab import LabConfig, ResearchLab, make_run_code_tool  # noqa: E402
from bioagent.agents.sandbox import CodeSandbox  # noqa: E402
from bioagent.core.config import load_project_env  # noqa: E402
from bioagent.tools.datasets import run_dataset_smoke_analysis  # noqa: E402

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def _openrouter(base_url: str, api_key: str, model: str):
    """Return (complete_fn, chat_tools_fn) bound to OpenRouter's OpenAI /v1 API."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/KrimsonSun/BioAgentPrototype",
        "X-Title": "AiScientist research lab",
    }

    def _post(payload: dict) -> dict:
        data = json.dumps({"model": model, **payload}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 - OpenRouter
            return json.loads(resp.read().decode("utf-8", "replace"))

    def complete(messages: list[dict]) -> str:
        body = _post({"messages": messages, "stream": False})
        return _strip_think((body["choices"][0]["message"] or {}).get("content") or "")

    def chat_tools(messages: list[dict], tools: list[dict]) -> dict:
        body = _post({"messages": messages, "tools": tools, "tool_choice": "auto", "stream": False})
        msg = body["choices"][0]["message"] or {}
        return {"content": _strip_think(msg.get("content") or ""), "tool_calls": msg.get("tool_calls") or []}

    return complete, chat_tools


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", required=True, type=Path)
    p.add_argument("--model", default="qwen/qwen3.6-35b-a3b")
    p.add_argument("--base-url", default=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--question", default=None)
    p.add_argument("--with-code", action="store_true",
                   help="Add the run_code CodeAct tool (needs scanpy + the dataset bound into the sandbox).")
    args = p.parse_args(argv)

    load_project_env(Path.cwd())
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("OPENROUTER_API_KEY not set (looked in ./.env).", file=sys.stderr)
        return 2
    if not args.dataset.exists():
        print(f"dataset not found: {args.dataset}", file=sys.stderr)
        return 2

    workspace = Path("runs") / f"lab-demo-{args.dataset.stem}"
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"== Loading {args.dataset.name} (h5py preflight — raw matrix never enters the prompt) ==")
    # run_dataset_smoke_analysis returns a wrapper with artifact paths + the analysis
    # under ["result"] — that nested dict is what the QC/DE tool builders read.
    dataset_result = run_dataset_smoke_analysis(args.dataset, workspace / "artifacts")["result"]
    print(f"   dataset_kind={dataset_result.get('dataset_kind')} "
          f"cells={dataset_result.get('cells')} genes={dataset_result.get('genes')}")

    question = args.question or (
        f"Analyze the single-cell dataset '{args.dataset.name}' (a DDX41 differential-expression "
        "study): run quality control, identify the most informative marker genes, and interpret what "
        "they suggest — treating every conclusion as a hypothesis to validate."
    )

    complete_fn, chat_tools_fn = _openrouter(args.base_url, api_key, args.model)
    ctx = HarnessContext(decisions={"dataset_result": dataset_result, "dataset_path": str(args.dataset)},
                         workspace=workspace, model=args.model)
    catalog = list(default_catalog())
    if args.with_code:
        catalog.append(make_run_code_tool(CodeSandbox(timeout_s=20)))
    scientist = ResearchHarness(catalog=catalog, chat_fn=chat_tools_fn)
    lab = ResearchLab(ctx, LabConfig(max_rounds=args.rounds), complete_fn=complete_fn, scientist=scientist)

    events: list[dict] = []

    def on_event(ev: dict) -> None:
        events.append(ev)
        t = ev.get("type")
        if t == "pi_agenda":
            print("\n[PI] agenda:")
            for i, s in enumerate(ev["agenda"], 1):
                print(f"   {i}. {s}")
        elif t == "scientist_start":
            print(f"\n[Scientist] working on: {ev['step']}")
        elif t == "tool_start":
            print(f"   -> tool {ev['tool']}({json.dumps(ev.get('args', {}))[:80]})")
        elif t == "tool_result":
            print(f"      = {ev['tool']}: {ev['summary']}")
        elif t == "tool_error":
            print(f"      ! {ev.get('tool')}: {ev['error'][:160]}")
        elif t == "finish":
            print(f"   [Scientist] finish: {ev['answer_preview'][:160]}")
        elif t == "critic":
            print(f"[Critic] {ev['step'][:50]} -> {ev['verdict'].upper()} (score {ev['score']:.2f})")
        elif t == "step_force_advance":
            print(f"[lab] giving up revising '{ev['step'][:50]}', advancing")
        elif t == "synthesize":
            print(f"\n[PI] synthesizing final report from {ev['accepted']} accepted step(s) ...")

    print(f"\n== Running lab with model={args.model} (max_rounds={args.rounds}) ==")
    result = lab.run(question, on_event=on_event)

    print("\n" + "=" * 72)
    print(f"CONVERGED={result.converged}  accepted_steps={result.accepted_steps}/{len(result.agenda)}  "
          f"rounds={len(result.rounds)}")
    print("=" * 72)
    print("\n=== FINAL REPORT ===\n")
    print(result.final_answer)

    out = workspace / "lab_result.json"
    out.write_text(json.dumps({"result": result.to_dict(), "events": events}, indent=2), encoding="utf-8")
    (workspace / "final_report.md").write_text(result.final_answer or "", encoding="utf-8")
    print(f"\n(saved: {out} and final_report.md)")
    return 0 if result.converged else 1


if __name__ == "__main__":
    raise SystemExit(main())
