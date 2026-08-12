#!/usr/bin/env python3
"""Local Biomni smoke — run the REAL A1 with NO data lake, against a local LLM.

This is the fast local-debug loop for Biomni (no 11GB data lake, no GPU/HPC3, no
deploy). It runs the real ``biomni.agent.A1`` on a trivial task to confirm the
agent + its code-generation tool-calling work end-to-end with our LLM endpoint.

Key choices (validated against the upstream Biomni source):
  - ``expected_data_lake_files=[]`` → skips the S3 data-lake download entirely.
  - ``source="Custom"`` (OpenAI-compatible) → honors ``base_url`` (the ``Ollama``
    source ignores base_url, so it can't reach a tunnel/custom port).
  - ``use_tool_retriever=False`` → no embedding index needed for a minimal run.

Setup (one-time):
    pip install -e ../biomni langchain-openai     # core is light; this adds the OpenAI client

Run against OpenRouter (cloud, for synthetic/test data only — never real patient data):
    export BIOMNI_BASE_URL=https://openrouter.ai/api/v1
    export BIOMNI_API_KEY=sk-or-...
    export BIOMNI_MODEL=qwen/qwen3.6-35b-a3b      # or anthropic/claude-3.5-sonnet to test the ceiling
    python scripts/biomni_local_smoke.py

Run against a local Ollama (no cloud):
    export BIOMNI_BASE_URL=http://127.0.0.1:11434/v1
    export BIOMNI_API_KEY=ollama
    export BIOMNI_MODEL=qwen3.6:35b-a3b
    python scripts/biomni_local_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile

DEFAULT_TASK = (
    "What is the official HGNC gene symbol for the photoreceptor protein rhodopsin? "
    "Reply with just the gene symbol."
)


def main() -> int:
    base_url = os.environ.get("BIOMNI_BASE_URL")
    api_key = os.environ.get("BIOMNI_API_KEY", "EMPTY")
    model = os.environ.get("BIOMNI_MODEL", "qwen3.6:35b-a3b")
    task = os.environ.get("BIOMNI_TASK", DEFAULT_TASK)
    # Optional: point Biomni at a LOCAL dataset — its generated code reads the path
    # directly (the data never enters the prompt). Mirrors BiomniAdapter._analysis_task.
    dataset = os.environ.get("BIOMNI_DATASET")
    if dataset:
        task = (
            f"{task}\n\nA dataset is available locally at this path: {dataset}\n"
            "Load it directly from that path with Python (scanpy/anndata for .h5ad, pandas for "
            ".csv/.tsv) and analyze it to answer the request. Do NOT ask for the data to be uploaded."
        )
    if not base_url:
        print("Set BIOMNI_BASE_URL (and BIOMNI_API_KEY / BIOMNI_MODEL). See this file's header.", file=sys.stderr)
        return 2

    try:
        from biomni.agent import A1
    except ImportError as exc:
        print(f"biomni not importable ({exc}). Run: pip install -e ../biomni langchain-openai", file=sys.stderr)
        return 2

    path = os.environ.get("BIOMNI_DATA_PATH") or tempfile.mkdtemp(prefix="biomni-smoke-")
    print(f"== Biomni local smoke ==\n  base_url={base_url}\n  model={model}\n  path={path} (no data lake)\n")

    agent = A1(
        path=path,
        llm=model,
        source="Custom",                 # OpenAI-compatible; honors base_url (unlike "Ollama")
        base_url=base_url,
        api_key=api_key,
        use_tool_retriever=False,         # no embedding index for a minimal run
        expected_data_lake_files=[],      # <-- skip the 11GB S3 download
    )
    print(f"\n--- go() task: {task!r}\n")
    result = agent.go(task)

    # A1.go historically returns (log, final_message); stay defensive.
    if isinstance(result, tuple) and len(result) == 2:
        log, answer = result
        print("\n=== ANSWER ===")
        print(answer)
    else:
        print("\n=== RESULT ===")
        print(result)
    print("\n(smoke complete — if you got a sensible answer, Biomni's code-gen tool-calling works with this LLM)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
