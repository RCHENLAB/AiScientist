"""In-process CLI: run ONE deep-literature (PaperQA) query against the pre-built HPC3 index,
emit result JSON. The literature-line counterpart of :mod:`bioagent.tools.variant_cli`.

Runs ON HPC3 (where the PubMedBERT index + PDF corpus live under ``/dfs3b`` and the served
Qwen is reachable), driven by :class:`bioagent.gateway.slurm_analysis.SlurmAnalysisExecutor`.
It imports the SAME :func:`bioagent.tools.paperqa_search.run_paperqa` the eyeserver would call,
so identical code runs locally (fallback) or on HPC3 — no forked logic.

Why this exists (vs just running the tool in-process on the gateway): the eyeserver does NOT
mount ``/dfs3b``, so it cannot read the index in place. This CLI is invoked on an HPC3 node
that CAN, and reaches the served Qwen at the GPU node's own ``host:port`` (``--args.llm_base_url``)
rather than through the eyeserver SSH tunnel.

Deployment config the LLM never sends (index dir, papers dir, manifest, embedding model, and the
Qwen endpoint) arrives in ``--args`` — the executor injects it there before submitting. Because
``paperqa_search`` reads its index/embedding defaults from the environment AT IMPORT TIME, this
CLI sets those env vars BEFORE importing it.

Result contract (same as ``variant_cli`` / ``scrna_cli``): the tool's result dict is printed on
one marked line::

    BIOAGENT_RESULT_JSON {"status": "ok", ...}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

RESULT_MARKER = "BIOAGENT_RESULT_JSON "

# --args keys that carry PaperQA deploy config → the env vars paperqa_search reads at import.
# Set these BEFORE importing paperqa_search, or its module-level index/embedding defaults miss them.
_ARG_TO_ENV = {
    "embedding": "BIOAGENT_PAPERQA_EMBEDDING",
    "papers": "BIOAGENT_PAPERQA_PAPERS",
    "index_dir": "BIOAGENT_PAPERQA_INDEX_DIR",
    "index_name": "BIOAGENT_PAPERQA_INDEX_NAME",
    "manifest": "BIOAGENT_PAPERQA_MANIFEST",
    "llm_base_url": "BIOAGENT_PAPERQA_LLM_BASE_URL",
    # Retrieval breadth / determinism. These MUST travel as args: the .sif runs with
    # ``--containall``, so nothing the eyeserver exports reaches this process. Absent keys are
    # skipped below, leaving paperqa_search's own defaults in charge.
    "search_count": "BIOAGENT_PAPERQA_SEARCH_COUNT",
    "evidence_k": "BIOAGENT_PAPERQA_EVIDENCE_K",
    "max_sources": "BIOAGENT_PAPERQA_MAX_SOURCES",
    "answer_length": "BIOAGENT_PAPERQA_ANSWER_LENGTH",
    "concurrency": "BIOAGENT_PAPERQA_CONCURRENCY",
    "temperature": "BIOAGENT_PAPERQA_TEMPERATURE",
}


def run_tool(tool: str, workspace: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Dispatch ONE literature step. Only ``deep_literature`` (PaperQA RAG) exists today."""
    args = args or {}
    if tool != "deep_literature":
        return {"status": "error", "error": f"unknown literature tool: {tool}"}
    question = str(args.get("question", "")).strip()
    if not question:
        return {"status": "error", "error": "deep_literature needs a 'question'"}

    # Inject deploy config into the environment BEFORE importing paperqa_search (see module docstring).
    for arg_key, env_key in _ARG_TO_ENV.items():
        val = args.get(arg_key)
        if val:
            os.environ[env_key] = str(val)

    # Point Hugging Face at the pre-staged OFFLINE model cache so the container's
    # sentence-transformers loads the PubMedBERT embedding WITHOUT hitting the network. The
    # PaperQA job runs the .sif with ``--containall`` (host env is NOT inherited) and
    # ``HF_HUB_OFFLINE=1``, so with HF_HOME unset it looks in an empty in-container cache, tries
    # huggingface.co, and fails offline (OSError: couldn't connect / not in cached files). The
    # embed job stages the model into ``<corpus-root>/hf_cache`` (a sibling of the papers dir),
    # which is already bind-mounted read-only, so DERIVE HF_HOME from ``--args.papers`` — no extra
    # config to pass. Must be set BEFORE importing paperqa_search (the embedding default resolves
    # at import, and sentence-transformers reads these at load time).
    _papers = args.get("papers") or os.environ.get("BIOAGENT_PAPERQA_PAPERS")
    if _papers:
        _hf = os.path.join(os.path.dirname(str(_papers)), "hf_cache")
        if os.path.isdir(_hf):
            # UNCONDITIONAL override (not "only if unset"): the .sif bakes its OWN HF_HOME into the
            # image %environment, so a "set only if unset" guard keeps that baked (empty, in-container)
            # path and the offline model load still fails with the exact OSError we saw. Force the DFS
            # cache to win. HF_HUB_CACHE points at the ``hub/`` subdir (where snapshot_download looks);
            # SENTENCE_TRANSFORMERS_HOME covers the ST loader's own cache lookup.
            os.environ["HF_HOME"] = _hf
            os.environ["HF_HUB_CACHE"] = os.path.join(_hf, "hub")
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"


    import types

    from .paperqa_search import run_paperqa

    # Build the same context object the in-process harness threads onto a run. ``llm_base_url``
    # (the GPU node's OpenAI-compatible endpoint) takes precedence over ``tunnel_port`` inside
    # paperqa_search._local_endpoint; ``model`` must match vLLM's --served-model-name.
    ctx = types.SimpleNamespace(
        model=args.get("model"),
        llm_base_url=args.get("llm_base_url"),
        tunnel_port=args.get("tunnel_port"),
        workspace=workspace,
    )
    return run_paperqa({"question": question}, ctx)


def _load_args(raw: str) -> dict[str, Any]:
    """``--args`` is either inline JSON or a path to a JSON file (the runner stages a file)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(Path(raw).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one bioagent deep-literature (PaperQA) query on HPC3.")
    ap.add_argument("--tool", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dataset", default="")   # unused (literature needs no dataset); kept for executor uniformity
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args(argv)
    try:
        args = _load_args(ns.args)
    except (json.JSONDecodeError, OSError) as exc:
        result: dict[str, Any] = {"status": "error", "error": f"bad --args: {exc}"}
    else:
        result = run_tool(ns.tool, ns.workspace, args)
    print(RESULT_MARKER + json.dumps(result))
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
