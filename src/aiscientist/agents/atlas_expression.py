"""Planner bridge to the pinned atlas MCP image, through run_in_environment.

No MCP/scanpy dependencies run in the gateway. The image's one-shot stdio client
owns gene resolution and analysis; the existing executor owns Slurm and transfers.
"""
from __future__ import annotations

import json
import os
import posixpath
import shlex
from typing import Any

SKILL = "explore-atlas-expression-with-aiscientist-mcp"

# Runs inside the image after a successful CLI call. Checkpoints stay in work/;
# only deliverables enter artifacts/, which the executor mirrors to the website.
COLLECT = '''import json, os, shutil
from pathlib import Path
result = json.loads(Path(os.environ["AISCI_ATLAS_RESULT"]).read_text())
if isinstance(result, list):
    result = json.loads("".join(result))
root = Path(os.environ["AISCI_DATA_DIR"]) / "atlas-expression-exploration"
dest = Path(os.environ["AISCIENTIST_ARTIFACTS"]) / "gene_expression"
# The server answers with absolute HPC3 paths. Name each by where the run's results keep it
# (gene_expression/<query_id>/<file>), so no server path reaches the model or the website.
roots = sorted({str(root), str(root.resolve())}, key=len, reverse=True)
def shown(value):
    if isinstance(value, dict):
        return {k: shown(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shown(v) for v in value]
    if isinstance(value, str):
        for r in roots:
            if value == r:
                return "gene_expression"
            if value.startswith(r + "/"):
                return "gene_expression/" + value[len(r) + 1:]
    return value
result = shown(result)
for state in root.glob("*/state.json"):
    query = state.parent
    for pattern in ("gene_resolution.tsv", "summary_*.tsv", "*.pdf", "*.png", "report.md"):
        for source in query.glob(pattern):
            if source.is_file() and not source.is_symlink():
                target = dest / query.name / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
# Save the complete response as evidence before bounding the transport envelope.
result.pop("markdown", None)
dest.mkdir(parents=True, exist_ok=True)
full = dest / "mcp_result.json"
full.write_text(json.dumps(result))
if "rows" in result:
    result["full_table"] = result["table"]
    result["rows_omitted"] = 0
    rows = result["rows"]
    result["rows"] = []
    size = len(json.dumps(result))
    for row in rows:
        size += len(json.dumps(row)) + 2
        if size > 12000:
            break
        result["rows"].append(row)
    result["rows_omitted"] = len(rows) - len(result["rows"])
if len(json.dumps(result)) > 14000:
    result = {k: result[k] for k in ("state", "query_id") if k in result and len(json.dumps(result[k])) < 1000}
    result.update(response_omitted=True, full_result="gene_expression/mcp_result.json")
atlas = Path(os.environ["ATLAS_H5AD"])
stat = atlas.stat()
print(json.dumps({"result": result, "atlas": {"path": str(atlas.resolve()),
    "version": os.environ["AISCI_ATLAS_RELEASE"],
    "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}}, separators=(",", ":")))
'''


def _schema(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


S = {"type": "string"}
QUERY = {"query_id": S}
TOOLS = {
    "resolve_genes": ("Resolve names first; report aliases, previous symbols, case and ambiguity.",
                      _schema({"genes": S, "atlas": S}, ("genes",))),
    "submit_query": ("Load the full atlas once for all genes on a 300 GB Slurm node. Inline: returns when complete.",
                     _schema({"genes": S, "atlas": S, "rerun": {"type": "boolean"}}, ("genes",))),
    "query_status": ("Check query state; only analyze COMPLETED queries.", _schema(QUERY, ("query_id",))),
    "get_summary": ("Donor-aware descriptive CP10k summary; report n_donors and frac_donors_detected.",
                    _schema({**QUERY, "level": {"type": "string", "enum": ["majorclass", "celltype", "region_class"]},
                             "gene": S, "top": {"type": "integer"}}, ("query_id",))),
    "plot_dotplot": ("Write PDF and PNG dotplots by class and cell type.", _schema(QUERY, ("query_id",))),
    "plot_violin": ("Write PDF and PNG violin plots.", _schema({**QUERY, "groupby": S}, ("query_id",))),
    "plot_featureplot": ("Write PDF and PNG UMAP feature plots.", _schema(QUERY, ("query_id",))),
    "write_report": ("Write report.md with resolution, donor support, figures and caveats.", _schema(QUERY, ("query_id",))),
    "list_queries": ("List this run's atlas queries and states.", _schema({})),
}


def configured_atlases() -> dict[str, str]:
    """``{normalized path: release label}`` from ``AISCIENTIST_ATLAS_FILES`` (``label=path;…``).
    An entry without an absolute path, or labelled ``latest``, is ignored."""
    configured = {}
    for entry in os.environ.get("AISCIENTIST_ATLAS_FILES", "").split(";"):
        label, sep, path = entry.strip().partition("=")
        if sep and label and path.startswith("/") and label.lower().split("/")[-1] != "latest":
            configured[posixpath.normpath(path)] = label
    return configured


def direct_call_refusal() -> str:
    """What ``run_in_environment`` answers when asked to run this skill itself."""
    if configured_atlases():
        return "Use the atlas bridge tools: " + ", ".join(TOOLS)
    return ("Atlas expression is not set up on this deployment: no atlas is listed in "
            "AISCIENTIST_ATLAS_FILES.")


def build_atlas_tools(executor: Any, environment_tool: Any) -> list:
    """Expose all nine tools only with an installed, allowed skill, an image runner and at least one
    configured atlas: without one every call would fail, so the planner is not offered them."""
    from .skills import get_skill
    from .research_harness import HarnessTool

    skill = get_skill(SKILL)
    if environment_tool is None or skill is None or not skill.image or not configured_atlases():
        return []

    def call(name: str, args: dict, ctx: Any) -> dict:
        schema = TOOLS[name][1]
        if set(args) - set(schema["properties"]) or any(k not in args for k in schema["required"]):
            return {"status": "error", "error": "Unexpected or missing atlas tool arguments"}
        atlas = str(getattr(executor, "dataset_path", "") or "")
        # A file bound as server data reaches HPC3 as this session's copy, whose path is new each
        # session: the file it was copied from is the one that has to be listed.
        source = str(getattr(executor, "dataset_source", "") or "")
        configured = configured_atlases()
        version = configured.get(posixpath.normpath(atlas)) if atlas else None
        if not version and atlas and source:
            version = configured.get(posixpath.normpath(source))
        else:
            source = ""
        if not version:
            return {"status": "error", "error": "Attach a primary dataset listed in AISCIENTIST_ATLAS_FILES"}
        if args.get("atlas") and posixpath.normpath(args["atlas"]) not in {
                posixpath.normpath(p) for p in (atlas, source) if p}:
            return {"status": "error", "error": "atlas must be the attached primary dataset"}
        command = (
            'export ATLAS_H5AD="$AISCIENTIST_DATASET"\n'
            'export AISCI_DATA_DIR="$AISCIENTIST_ENV_DIR" AISCI_EXECUTOR=inline\n'
            f"export AISCI_ATLAS_RELEASE={shlex.quote(version)}\n"
            # The HGNC table in this user's tool cache, downloaded by their first call; without
            # a cache the server keeps it in this run's folder.
            'if [ -n "${AISCIENTIST_TOOL_CACHE:-}" ]; then\n'
            '  export ATLAS_HGNC="$AISCIENTIST_TOOL_CACHE/atlas-expression/hgnc_complete_set.txt"\n'
            'fi\n'
            'export AISCI_ATLAS_RESULT=$(mktemp "$AISCIENTIST_ENV_DIR/.atlas-result.XXXXXX")\n'
            'trap \'rm -f "$AISCI_ATLAS_RESULT"\' EXIT\n'
            'aiscientist-mcp --server atlas-expression-exploration-mcp --timeout 15000 call '
            f'{name} {shlex.quote(json.dumps(args))} > "$AISCI_ATLAS_RESULT" || exit $?\n'
            "python - <<'AISCI_ATLAS_COLLECT'\n" + COLLECT + "AISCI_ATLAS_COLLECT\n")
        out = environment_tool.executor(
            {"skill": SKILL, "command": command}, ctx, _atlas_bridge=True,
            _resources=None if name == "submit_query" else (2, 16, "00:30:00"), _tool_cache=True)
        if out.get("status") != "ok":
            return out
        try:
            payload = json.loads(out.get("stdout", ""))
            result = payload["result"]
            if not isinstance(result, dict):
                raise ValueError("MCP result is not an object")
            provenance = payload["atlas"]
        except (ValueError, KeyError, TypeError) as exc:
            return {**out, "status": "error", "error": f"Invalid atlas MCP response: {exc}"}
        from . import run_manifest
        recorded = {**provenance, "copied_from": source} if source else provenance
        run_manifest.update(ctx, lambda doc: doc.update(atlas=recorded))
        out.pop("stdout", None)
        response = {**out, **result, "atlas": provenance}
        if result.get("state") in {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}:
            response.update(status="error", error=f"Atlas query ended in {result['state']}")
        return response

    return [HarnessTool(name=name, description="Atlas expression: " + description +
                        " Flow: resolve_genes -> submit_query -> query_status -> get_summary, "
                        "plot_dotplot, plot_violin, plot_featureplot -> write_report. "
                        "Use these nine typed tools; run_in_environment refuses this skill. "
                        "Uses the attached atlas read in place; outputs appear in gene_expression/.",
                        parameters=schema, executor=lambda args, ctx, n=name: call(n, args or {}, ctx),
                        reads_private_data=True, category="single-cell")
            for name, (description, schema) in TOOLS.items()]
