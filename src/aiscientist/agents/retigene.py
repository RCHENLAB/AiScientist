"""Planner bridge to the pinned retigene-rag MCP image, through run_in_environment.

The server searches the RetiGene papers and runs a corpus-wide gene curation (RCHENLAB/AiScientist issue
95). Its index is read in place: the deployment names its HPC3 copy (``AISCIENTIST_RETIGENE_INDEX``), which
the job binds read-only. The curation reads with the model of the user's session: the job sends each
prompt back to the gateway (``RETIGENE_PROVIDER=relay``), which answers it with that session's model and
records it against the user's usage (``gateway/mcp_model_relay``). No key and no model address enters
the job. The model's replies are kept in the user's tool cache, so a question asked again reads nothing
twice; the curation's tables and report are copied into the run's results under ``retigene/``.
"""
from __future__ import annotations

import os
import posixpath
import shlex
from typing import Any, Callable

from .mcp_bridge import READ_RESULT, PreparedCall

SKILL = "query-retigene-papers-with-aiscientist-mcp"
SERVER = "retigene-rag"

# The collector's transport bound (the bridge's 14,000 characters) and the search pages that fit in it.
MAX_SENTENCE_LINES = 40
MAX_EVIDENCE_ROWS = 20
MAX_GENE_ROWS = 100

# Runs inside the image after a successful CLI call. Only deliverables enter artifacts/, which the executor
# mirrors to the website; every server path in the result is named by where the run's results keep it.
COLLECT = READ_RESULT + '''import shutil
root = Path(os.environ["AISCI_DATA_DIR"]) / "retigene-rag"
dest = Path(os.environ["AISCIENTIST_ARTIFACTS"]) / "retigene"
names = [(str(root), "retigene"), (str(root.resolve()), "retigene")]
for var, label in (("RETIGENE_INDEX", "the RetiGene index"), ("RETIGENE_CACHE", "the reply cache"),
                   ("RETIGENE_RELAY_DIR", "the model relay"), ("AISCIENTIST_TOOL_CACHE", "the tool cache"),
                   ("AISCIENTIST_ENV_DIR", "the server folder")):
    if os.environ.get(var):
        names += [(os.environ[var], label), (str(Path(os.environ[var]).resolve()), label)]
names.sort(key=lambda pair: len(pair[0]), reverse=True)
def shown(value):
    if isinstance(value, dict):
        return {k: shown(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shown(v) for v in value]
    if isinstance(value, str):
        for path, label in names:
            if path and path in value:
                value = value.replace(path, label)
    return value
result = shown(result)
cid = result.get("curation_id")
if isinstance(cid, str) and cid and "/" not in cid and cid not in (".", ".."):
    folder = root / cid
    for name in ("genes.csv", "sentences.csv", "report.md"):
        source = folder / name
        if source.is_file() and not source.is_symlink():
            target = dest / cid / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
dest.mkdir(parents=True, exist_ok=True)
full = dest / (os.environ["AISCI_MCP_TOOL"] + ".json")
full.write_text(json.dumps(result))
def size():
    return len(json.dumps(result))
for key in ("rows", "passages", "curations"):
    items = result.get(key)
    if isinstance(items, list) and size() > 12000:
        kept, budget = [], 12000 - (size() - len(json.dumps(items)))
        for item in items:
            budget -= len(json.dumps(item)) + 2
            if budget < 0:
                break
            kept.append(item)
        result[key] = kept
        result[key + "_omitted"] = len(items) - len(kept)
        result["full_result"] = "retigene/" + full.name
lines = result.get("sentences")
if isinstance(lines, str) and size() > 12000:
    rows = lines.split("\\n")
    keep = rows[:]
    while keep and size() - len(json.dumps(lines)) + len(json.dumps("\\n".join(keep))) > 12000:
        keep.pop()
    result["sentences"] = "\\n".join(keep)
    result["sentences_omitted"] = len(rows) - len(keep)
    result["full_result"] = "retigene/" + full.name
if isinstance(result.get("markdown"), str) and size() > 14000:
    result["markdown"] = result["markdown"][:8000] + "\\n... (the whole report is in retigene/" + str(cid) + "/report.md)"
if size() > 14000:
    result = {k: result[k] for k in ("curation_id", "state", "error") if k in result and len(json.dumps(result[k])) < 1000}
    result.update(response_omitted=True, full_result="retigene/" + full.name)
print(json.dumps({"result": result}, separators=(",", ":")))
'''


def configured_index() -> str:
    """The HPC3 path of the RetiGene index the deployment names (``AISCIENTIST_RETIGENE_INDEX``: a folder
    with ``mechanism_cache/sentences.jsonl``, ``chunks.jsonl``, ``chunk_vecs.npy``, ``manifest.csv`` and
    ``hgnc_complete_set.txt``), or "" when none is named. Only an absolute path is taken."""
    path = os.environ.get("AISCIENTIST_RETIGENE_INDEX", "").strip()
    return posixpath.normpath(path) if path.startswith("/") else ""


def index_available() -> bool:
    return bool(configured_index())


class RetigeneAdapter:
    """retigene-rag's bindings and outputs; transport and tool schemas live in mcp_bridge."""

    def __init__(self, *, planner_name: Callable[[str], str]):
        search = " / ".join(planner_name(n) for n in ("search_sentences", "search_passages"))
        tables = ", ".join(planner_name(n) for n in ("get_genes", "get_evidence", "evaluate"))
        self.description = (
            f" Flow: {search} -> {planner_name('read_passage')}; for every gene with a property, "
            f"{planner_name('submit_curation')} (estimate_only first, then the curation; it returns when the "
            f"curation ends) -> {tables} -> {planner_name('write_report')}. The curation reads with this "
            "session's own model, on the user's own key or the session's cluster model, recorded in their "
            "usage; its tables and report appear in retigene/<curation_id>/. The index is read in place.")

    def prepare(self, args: dict, executor: Any, *, tool: str = "") -> PreparedCall:
        index = configured_index()
        if not index:
            raise ValueError("The RetiGene index is not set up on this deployment (AISCIENTIST_RETIGENE_INDEX)")
        args = dict(args)
        if tool == "search_sentences":
            args["limit"] = max(1, min(int(args.get("limit") or MAX_SENTENCE_LINES), MAX_SENTENCE_LINES))
        elif tool == "get_evidence":
            args["limit"] = max(1, min(int(args.get("limit") or MAX_EVIDENCE_ROWS), MAX_EVIDENCE_ROWS))
        elif tool == "get_genes":
            args["top"] = max(1, min(int(args.get("top") or MAX_GENE_ROWS), MAX_GENE_ROWS))
        elif tool == "evaluate" and str(args.get("reference") or "").strip():
            # The job can read only its own folders; a reference path would name a file outside them.
            raise ValueError("evaluate scores against the bundled curator reference only: leave reference empty")
        setup = (f"export RETIGENE_INDEX={shlex.quote(index)}\n"
                 'if [ -n "${AISCIENTIST_TOOL_CACHE:-}" ]; then\n'
                 '  export RETIGENE_CACHE="$AISCIENTIST_TOOL_CACHE/retigene-rag/cache"\n'
                 'else\n'
                 '  export RETIGENE_CACHE="$AISCIENTIST_ENV_DIR/retigene-rag-cache"\n'
                 'fi\n')
        around = None
        context: dict[str, Any] = {}
        if tool == "submit_curation":
            relay = getattr(executor, "model_relay", None)
            if relay is None:
                raise ValueError("This session cannot answer the curation's model calls (no HPC3 session or "
                                 "no model): the curation was not started")
            call = relay.open("retigene")
            b = call.binding
            setup += (f"export RETIGENE_PROVIDER=relay RETIGENE_RELAY_DIR={shlex.quote(call.folder)}\n"
                      f"export RETIGENE_CHECK_MODEL={shlex.quote(b.model)} RETIGENE_REVIEW_MODEL={shlex.quote(b.model)}\n"
                      f"export RETIGENE_PRICE_IN={b.rates[0]!r} RETIGENE_PRICE_OUT={b.rates[1]!r}\n")
            if b.cap is not None:
                asked = args.get("max_cost_usd")
                args["max_cost_usd"] = round(min(float(asked), b.cap) if asked is not None else b.cap, 4)
            if not args.get("estimate_only"):
                around = call.serving
            context = {"model": b.model, "metered": b.metered, "relay": call}
        return PreparedCall(args, setup, COLLECT, tool_cache=True, read_only=(index,), around=around,
                            context=context)

    def finish(self, out: dict, payload: dict, ctx: Any, prepared: PreparedCall) -> dict:
        from . import run_manifest
        result = payload["result"]
        index = result.get("index")
        if isinstance(index, dict):
            recorded = {k: index[k] for k in ("dataset", "version", "version_uuid", "fingerprint") if k in index}
            run_manifest.update(ctx, lambda doc: doc.update(retigene_index=recorded))
        reserved = {"status", "error", "stdout", "stderr", "returncode", "artifacts", "provenance",
                    "environment", "execution_mode", "skill", "mcp"}
        response = {**{k: v for k, v in out.items() if k != "stdout"},
                    "result": {k: v for k, v in result.items() if k not in reserved}}
        if result.get("error"):
            response["result"]["error"] = result["error"]
        relay = prepared.context.get("relay")
        stats = getattr(relay, "stats", None)
        if stats is not None and stats.stopped:
            response["relay_stopped"] = stats.stopped
        if result.get("state") in {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}:
            response.update(status="error", error=f"The curation ended in {result['state']}: "
                                                  f"{result.get('error') or 'see curation_status'}")
        return response
