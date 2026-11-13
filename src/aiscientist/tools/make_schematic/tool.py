"""Legacy schematic tool; deterministic report rendering is shared independently."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from .._lib import schematics as _schematics
from .._lib.schematics import (
    render_dot as render_dot,
    render_workflow_schematic as render_workflow_schematic,
    workflow_schematic_dot as workflow_schematic_dot,
)


def __getattr__(name: str) -> Any:
    # shutil/subprocess objects and helper imports keep their historical identity.
    return getattr(_schematics, name)


# --- harness tool -------------------------------------------------------------


def _figdir(ctx: Any) -> Path:
    ws = getattr(ctx, "workspace", None)
    if ws is None:
        raise ValueError("make_schematic needs ctx.workspace to write the figure")
    figs = Path(ws) / "artifacts" / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    return figs


def _exec_make_schematic(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    source = str(args.get("source", "")).strip()
    if not source:
        return {"status": "error", "error": "no diagram source provided"}
    engine = str(args.get("engine") or "dot").lower()
    if engine != "dot":
        # D2 and Mermaid were offered once; neither binary was ever installed on the gateway.
        return {"status": "error",
                "error": f"engine {engine!r} is not available: write the diagram in Graphviz DOT"}
    basename = str(args.get("basename", "schematic")).strip() or "schematic"
    # keep the filename safe + namespaced so it lands in the figure gallery
    basename = "schematic_" + "".join(c for c in basename if c.isalnum() or c in "-_")[:48]
    figs = _figdir(ctx)
    art = figs.parent
    out = render_dot(source, figs, basename=basename)             # graphviz PNG (PDF-friendly)
    # return a path relative to the artifacts root (the files-browser/report key)
    if out.get("path"):
        rel = Path(out["path"]).relative_to(art).as_posix()
        out["figure"] = rel
    return out


def make_schematic_tool() -> Any:
    """The schematic tool: the agent writes a Graphviz DOT description of a workflow / pathway /
    mechanism, rendered deterministically into the figure gallery.

    Imported lazily so ``tools.schematic`` has no import dependency on the agents pkg.
    """
    from ..sdk import HarnessTool

    return HarnessTool(
        "make_schematic",
        "Draw a SCHEMATIC diagram (analysis workflow, signaling pathway, or mechanism) "
        "by writing its structure as text — a deterministic renderer turns it into a "
        "PNG figure in the gallery (NO AI draws pixels). Write it in Graphviz DOT. Use for "
        "conceptual figures, not data plots. Example: "
        "'digraph { QC -> Clustering -> \"DE per cluster\" -> Enrichment }'.",
        {"type": "object", "properties": {
            "source": {"type": "string", "description": "the diagram in Graphviz DOT"},
            "basename": {"type": "string", "description": "short name for the figure file"}},
         "required": ["source"]},
        _exec_make_schematic,
        reads_private_data=False, category="figure", requires=("graphviz",),
    )
