"""Planner bridge to the pinned atlas MCP image, through run_in_environment.

No MCP/scanpy dependencies run in the gateway. The image's one-shot stdio client
owns gene resolution and analysis; the existing executor owns Slurm and transfers.
"""
from __future__ import annotations

import os
import posixpath
import shlex
from typing import Any, Callable

from .mcp_bridge import READ_RESULT, PreparedCall

SKILL = "explore-atlas-expression-with-aiscientist-mcp"

# Runs inside the image after a successful CLI call. Checkpoints stay in work/;
# only deliverables enter artifacts/, which the executor mirrors to the website.
COLLECT = READ_RESULT + '''import shutil
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


def configured_atlases() -> dict[str, str]:
    """Normalized absolute atlas paths and pinned labels from deployment configuration."""
    configured = {}
    for entry in os.environ.get("AISCIENTIST_ATLAS_FILES", "").split(";"):
        label, sep, path = entry.strip().partition("=")
        if sep and label and path.startswith("/") and label.lower().split("/")[-1] != "latest":
            configured[posixpath.normpath(path)] = label
    return configured


def atlases_available() -> bool:
    """An atlas is listed in AISCIENTIST_ATLAS_FILES, or the shared-datasets store is on (a shared
    dataset's file can be attached as the primary atlas without being listed)."""
    return bool(configured_atlases() or os.environ.get("AISCIENTIST_SHARED_DATASETS_ROOT", "").strip())


def shared_release(path: str) -> "str | None":
    """``<slug>/<label>`` when ``path`` is a file the shared-datasets store lists, on the gateway host
    (``AISCIENTIST_SHARED_DATASETS_ROOT``) or in its verified HPC3 copy
    (``AISCIENTIST_HPC_SHARED_ROOT``/datasets): a shared atlas needs no AISCIENTIST_ATLAS_FILES entry.
    The version is checked against the store's records, so a look-alike path is refused."""
    path = posixpath.normpath(path or "")
    for env, sub in (("AISCIENTIST_SHARED_DATASETS_ROOT", ""), ("AISCIENTIST_HPC_SHARED_ROOT", "datasets")):
        base = os.environ.get(env, "").rstrip("/")
        base = posixpath.join(base, sub) if base and sub else base
        if not base or not path.startswith(base + "/"):
            continue
        parts = path[len(base) + 1:].split("/", 2)
        if len(parts) != 3 or not all(parts):
            continue
        slug, label, name = parts
        try:
            from ..gateway import shared_datasets as sd
            found = sd.resolve(slug, label, allow_withdrawn=True)
        except Exception:  # noqa: BLE001 - not a shared file: fall through to the configured list
            continue
        if any(f["name"] == name for f in found["files"]):
            return f"{slug}/{found['label']}"
    return None


def direct_call_refusal() -> str:
    """Keep raw shell access closed, including deployments with no usable atlas."""
    if atlases_available():
        from .mcp_bridge import protected_skill_tools
        return "Use the atlas bridge tools: " + ", ".join(protected_skill_tools(SKILL))
    return ("Atlas expression is not set up on this deployment: no atlas is listed in "
            "AISCIENTIST_ATLAS_FILES and there are no shared datasets.")


class AtlasAdapter:
    """Atlas-specific bindings and outputs; transport and tool schemas live in mcp_bridge."""

    def __init__(self, *, planner_name: Callable[[str], str]):
        flow = " -> ".join(planner_name(name) for name in
                           ("resolve_genes", "submit_query", "query_status", "get_summary"))
        plots = ", ".join(planner_name(name) for name in ("plot_dotplot", "plot_violin", "plot_featureplot"))
        self.description = (
            f" Flow: {flow}, {plots} -> {planner_name('write_report')}. "
            "Use these typed tools; run_in_environment refuses this skill. "
            "Uses the attached atlas read in place; outputs appear in gene_expression/. "
            "The outer Slurm job owns scheduling; submit_query uses inline and returns when complete.")

    def prepare(self, args: dict, executor: Any, *, tool: str = "") -> PreparedCall:
        atlas = str(getattr(executor, "dataset_path", "") or "")
        source = str(getattr(executor, "dataset_source", "") or "")
        configured = configured_atlases()
        version = configured.get(posixpath.normpath(atlas)) if atlas else None
        if not version and atlas and source:
            version = configured.get(posixpath.normpath(source))
        else:
            source = ""
        if not version and atlas:
            # A shared dataset's file (issue 100), on this host or its HPC3 copy.
            version = shared_release(atlas) or (shared_release(str(getattr(executor, "dataset_source", "") or "")))
        if not atlas.endswith(".h5ad") or not version:
            raise ValueError("Attach a primary .h5ad dataset listed in AISCIENTIST_ATLAS_FILES")
        if args.get("atlas") and posixpath.normpath(args["atlas"]) not in {
                posixpath.normpath(p) for p in (atlas, source) if p}:
            raise ValueError("atlas must be the attached primary dataset")
        setup = ('export ATLAS_H5AD="$AISCIENTIST_DATASET"\n'
                 f"export AISCI_ATLAS_RELEASE={shlex.quote(version)}\n"
                 'if [ -n "${AISCIENTIST_TOOL_CACHE:-}" ]; then\n'
                 '  export ATLAS_HGNC="$AISCIENTIST_TOOL_CACHE/atlas-expression/hgnc_complete_set.txt"\n'
                 'fi\n')
        # Both the source path and its session copy are accepted above. Let the
        # server read its bound copy through ATLAS_H5AD, never the source host path.
        arguments = {key: value for key, value in args.items() if key != "atlas"}
        return PreparedCall(arguments, setup, COLLECT, tool_cache=True, context={"copied_from": source})

    def finish(self, out: dict, payload: dict, ctx: Any, prepared: PreparedCall) -> dict:
        from . import run_manifest
        result, provenance = payload["result"], payload["atlas"]
        if not isinstance(provenance, dict):
            raise ValueError("Invalid atlas provenance")
        source = prepared.context.get("copied_from")
        recorded = {**provenance, "copied_from": source} if source else provenance
        run_manifest.update(ctx, lambda doc: doc.update(atlas=recorded))
        # Preserve the old flat Atlas API without allowing tool data to replace
        # trusted execution status, artifacts or image provenance.
        reserved = {"status", "error", "stdout", "stderr", "returncode", "artifacts", "provenance",
                    "environment", "execution_mode", "skill", "mcp", "atlas"}
        response = {**{k: v for k, v in result.items() if k not in reserved},
                    **{k: v for k, v in out.items() if k != "stdout"}, "atlas": provenance}
        if result.get("state") in {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}:
            response.update(status="error", error=f"Atlas query ended in {result['state']}")
        return response
