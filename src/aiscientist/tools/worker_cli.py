"""Platform worker for dataset preflight, dependency installation and result handoff.

Runs inside the retained analysis image. It exposes no scientific tool dispatcher.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

RESULT_MARKER = "AISCIENTIST_RESULT_JSON "
INSTALL_DEPENDENCY = "_install_dependency"
EXPORT_RESULT_DATASET = "_export_result_dataset"
ADOPT_RESULT_DATASET = "_adopt_result_dataset"






def run_tool(tool: str, workspace: str, dataset_path: str | None, args: dict[str, Any] | None,
             dataset_root: str | None = None,
             input_folders: "list[dict] | None" = None) -> dict[str, Any]:
    """Dispatch one of the four platform-only operations against a run workspace."""
    args = args or {}
    if tool == "preflight":
        from ._lib.dataset_preflight import run_dataset_preflight
        if not dataset_path:
            return {"status": "error", "error": "preflight needs a dataset path"}
        out_dir = Path(workspace) / "artifacts" / "data"
        return {key: str(value) if isinstance(value, Path) else value
                for key, value in run_dataset_preflight(Path(dataset_path), out_dir).items()}
    if tool == INSTALL_DEPENDENCY:
        # Not a model-facing tool: the Slurm executor submits it when a tool reports a DECLARED
        # dependency missing (see run_deps.ALLOWED); anything else is refused there.
        from .run_deps import install
        return install(workspace, str(args.get("dependency", "")))
    if tool == EXPORT_RESULT_DATASET:
        # Not a model-facing tool: the gateway submits it once at the end of a run, before the
        # checkpoints are released, to hand on the processed dataset (see _lib/result_dataset).
        from ._lib.result_dataset import export
        return export(workspace, str(args.get("name", "")), args.get("meta") or {})
    if tool == ADOPT_RESULT_DATASET:
        # Not a model-facing tool: the gateway submits it once at the start of a run that continues
        # an earlier one, so the dataset that run handed on becomes this run's checkpoints.
        from ._lib.result_dataset import adopt
        if not dataset_path:
            return {"status": "skipped", "reason": "no dataset is bound to this run"}
        return adopt(workspace, dataset_path)
    return {"status": "error", "error": f"unknown platform operation: {tool}"}


def _load_args(raw: str) -> dict[str, Any]:
    """``--args`` is either inline JSON or a path to a JSON file (the runner stages a file)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(Path(raw).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one AiScientist platform operation in-container.")
    ap.add_argument("--tool", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--dataset", default="")
    ap.add_argument("--dataset-root", default="")
    # Every folder bound to the run, as JSON ``[{"name", "path"}]`` (inline, or a file like --args).
    ap.add_argument("--input-folders", default="")
    ap.add_argument("--args", default="{}")
    ns = ap.parse_args(argv)
    try:
        args = _load_args(ns.args)
        folders = _load_args(ns.input_folders) if ns.input_folders else None
    except (json.JSONDecodeError, OSError) as exc:
        result: dict[str, Any] = {"status": "error", "error": f"bad --args / --input-folders: {exc}"}
    else:
        result = run_tool(ns.tool, ns.workspace, ns.dataset or None, args,
                          dataset_root=ns.dataset_root or None,
                          input_folders=folders if isinstance(folders, list) else None)
    print(RESULT_MARKER + json.dumps(result))
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
