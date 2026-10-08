"""In-container CLI: run ONE analysis-line step against a workspace + dataset, emit result JSON.

Phase 4 of the HPC3 offload. This runs INSIDE ``analysis.sif`` on an HPC3 CPU node, driven by
:class:`aiscientist.gateway.slurm_analysis.SlurmAnalysisExecutor`. It imports the SAME tool functions
the eyeserver uses (the ``runs_on: hpc:analysis`` tool folders, and the dataset preflight) and calls
the named one with a reconstructed context, so identical code runs locally (fallback) or on HPC3 —
no forked analysis logic.

The dataset + the run's ``work/``+``artifacts/`` all live on shared DFS (dfs3b), bind-mounted into
the container, so checkpoints accumulate in place across steps with no round-trip.

Result contract: the tool's result dict is printed on a single marked line::

    AISCIENTIST_RESULT_JSON {"status": "ok", ...}

so the runner can parse it out of the captured stdout regardless of any other tool logging.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RESULT_MARKER = "AISCIENTIST_RESULT_JSON "
INSTALL_DEPENDENCY = "_install_dependency"


class _Ctx:
    """Minimal stand-in for the harness context the scrna tools read (``.workspace`` +
    ``.decisions['dataset_path']``, and ``['dataset_root']`` for a folder dataset)."""

    def __init__(self, workspace: str, dataset_path: str | None, dataset_root: str | None = None) -> None:
        self.workspace = Path(workspace)
        self.decisions: dict[str, Any] = {"dataset_path": dataset_path} if dataset_path else {}
        if dataset_root:
            self.decisions["dataset_root"] = dataset_root


def _analysis_tools(only: str | None = None) -> dict[str, Any]:
    """Every tool whose manifest says ``runs_on: hpc:analysis``, by name -> its executor (or just
    ``only``, when given: a job builds the one tool it runs, which matters when that tool's job runs
    in its own image, where another tool's module need not import).

    Read from the tool folders' TOOL.md, the same source the registry routes from, so a tool the
    registry sends to this image is always one this dispatcher knows (the hand-kept list once
    lagged the routing, and the missing tools ran on the gateway against an empty work/)."""
    from . import catalog
    return {t.name: t.executor for t in catalog.build_tools(
        where=lambda m: m.runs_on == "hpc:analysis" and (only is None or m.name == only))}


def run_tool(tool: str, workspace: str, dataset_path: str | None, args: dict[str, Any] | None,
             dataset_root: str | None = None) -> dict[str, Any]:
    """Dispatch ONE step against the workspace. ``preflight`` is the dataset smoke analysis
    (different signature); the rest are the standard ``(args, ctx) -> dict`` scrna tools."""
    args = args or {}
    if tool == "preflight":
        from .datasets import run_dataset_smoke_analysis
        if not dataset_path:
            return {"status": "error", "error": "preflight needs a dataset path"}
        out_dir = Path(workspace) / "artifacts" / "data"
        return run_dataset_smoke_analysis(Path(dataset_path), out_dir)
    if tool == INSTALL_DEPENDENCY:
        # Not a model-facing tool: the Slurm executor submits it when a tool reports a DECLARED
        # dependency missing (see run_deps.ALLOWED); anything else is refused there.
        from .run_deps import install
        return install(workspace, str(args.get("dependency", "")))
    fn = _analysis_tools(only=tool).get(tool)
    if fn is None:
        return {"status": "error", "error": f"unknown analysis tool: {tool}"}
    return fn(args, _Ctx(workspace, dataset_path, dataset_root))


def _load_args(raw: str) -> dict[str, Any]:
    """``--args`` is either inline JSON or a path to a JSON file (the runner stages a file)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(Path(raw).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one AiScientist analysis step in-container.")
    ap.add_argument("--tool", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dataset", default="")
    ap.add_argument("--dataset-root", default="")
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args(argv)
    try:
        args = _load_args(ns.args)
    except (json.JSONDecodeError, OSError) as exc:
        result: dict[str, Any] = {"status": "error", "error": f"bad --args: {exc}"}
    else:
        result = run_tool(ns.tool, ns.workspace, ns.dataset or None, args,
                          dataset_root=ns.dataset_root or None)
    print(RESULT_MARKER + json.dumps(result))
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
