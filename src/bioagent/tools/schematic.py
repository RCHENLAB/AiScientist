"""Deterministic SCHEMATIC figures — workflow/pathway/mechanism diagrams rendered
LOCALLY from text, with **no generative AI drawing pixels**.

The privacy/integrity story matches the data figures: a schematic is described as
text (a Graphviz **DOT** string, or a Mermaid string) and a deterministic renderer
turns that text — byte-for-byte — into a PNG/SVG. The LLM only ever writes the
*structured description* (auditable text: nodes + edges), never paints the image, so
there is no diffusion-model hallucination / tampering surface. (Qwen3-VL, the optional
vision layer, is read-only QA on the rendered figure — it cannot generate one.)

Two renderers, graphviz preferred:

* **Graphviz** (`dot`) — a single ``apt install graphviz``; the primary path.
* **Mermaid** (`mmdc`) — optional; needs Node + headless Chromium, so it is a
  best-effort fallback only.

Both lazy/graceful: a renderer that isn't installed returns
``{"status": "dependency_missing", ...}`` instead of crashing (same contract as
``tools/report.py`` for pandoc and ``tools/scrna_pack.py`` for scanpy).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def dot_available() -> bool:
    return shutil.which("dot") is not None


def mermaid_available() -> bool:
    return shutil.which("mmdc") is not None


def d2_available() -> bool:
    return shutil.which("d2") is not None


# --- deterministic DOT builders (pure Python — no AI, no external process) ----


def _esc(label: str) -> str:
    return str(label).replace('"', '\\"')


def workflow_schematic_dot(steps: list[str], *, title: str | None = None, rankdir: str = "TB") -> str:
    """Build a top-to-bottom flowchart DOT from an ordered list of step labels.

    Fully deterministic: same steps → same DOT → (via ``dot``) same PNG. Used to draw
    the analysis methods/workflow diagram from the steps that actually ran — zero AI.
    """
    # A modern, clean graphviz style: soft palette, orthogonal edges, generous spacing,
    # a teal accent for the start/end nodes. Still 100% deterministic, native PNG.
    lines = [
        "digraph workflow {",
        f"  rankdir={rankdir};",
        '  bgcolor="transparent";',
        "  splines=ortho; nodesep=0.5; ranksep=0.55; pad=0.3;",
        '  node [shape=box, style="rounded,filled", fillcolor="#EAF1FB", color="#3B6EA5", '
        'penwidth=1.4, fontname="Helvetica", fontcolor="#1F2D3D", fontsize=11, margin="0.22,0.14"];',
        '  edge [color="#3B6EA5", penwidth=1.3, arrowsize=0.8];',
    ]
    if title:
        lines.append(f'  labelloc="t"; label="{_esc(title)}"; '
                     'fontname="Helvetica-Bold"; fontsize=15; fontcolor="#1F2D3D";')
    clean = [s for s in (str(x).strip() for x in steps) if s]
    last = len(clean) - 1
    for i, step in enumerate(clean):
        accent = ' fillcolor="#D6EFEC", color="#2C8C82"' if i in (0, last) and last > 0 else ""
        lines.append(f'  n{i} [label="{_esc(step)}"{accent}];')
    for i in range(len(clean) - 1):
        lines.append(f"  n{i} -> n{i + 1};")
    lines.append("}")
    return "\n".join(lines) + "\n"


# --- renderers (deterministic external process; graceful when absent) --------


def render_dot(
    dot_source: str,
    out_dir: Path,
    *,
    basename: str = "schematic",
    fmt: str = "png",
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Render a Graphviz DOT string to ``<out_dir>/<basename>.<fmt>`` via the ``dot``
    binary. Always writes the ``.dot`` source too (so the diagram is reproducible /
    auditable even on a host without graphviz)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src_path = out_dir / f"{basename}.dot"
    src_path.write_text(dot_source, encoding="utf-8")

    if not dot_available():
        return {"status": "dependency_missing", "dependency": "graphviz",
                "dot_path": str(src_path), "path": None,
                "note": "graphviz `dot` not installed — `apt install graphviz` on the server for rendered schematics."}

    out_path = out_dir / f"{basename}.{fmt}"
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, source via stdin, no shell
            ["dot", f"-T{fmt}", "-o", str(out_path)],
            input=dot_source, text=True, capture_output=True, timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - report any failure, keep the .dot
        return {"status": "error", "dot_path": str(src_path), "path": None,
                "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0 or not out_path.exists():
        return {"status": "error", "dot_path": str(src_path), "path": None,
                "error": (proc.stderr or proc.stdout or "")[-2000:]}
    return {"status": "ok", "dot_path": str(src_path), "path": str(out_path)}


def render_mermaid(
    mermaid_source: str,
    out_dir: Path,
    *,
    basename: str = "schematic",
    fmt: str = "png",
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    """Render a Mermaid string via ``mmdc`` (optional; needs Node + headless Chromium).
    Best-effort fallback to graphviz."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src_path = out_dir / f"{basename}.mmd"
    src_path.write_text(mermaid_source, encoding="utf-8")
    if not mermaid_available():
        return {"status": "dependency_missing", "dependency": "mermaid-cli",
                "src_path": str(src_path), "path": None,
                "note": "mermaid `mmdc` not installed — prefer graphviz/DOT, or `npm i -g @mermaid-js/mermaid-cli`."}
    out_path = out_dir / f"{basename}.{fmt}"
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["mmdc", "-i", str(src_path), "-o", str(out_path)],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "src_path": str(src_path), "path": None,
                "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0 or not out_path.exists():
        return {"status": "error", "src_path": str(src_path), "path": None,
                "error": (proc.stderr or proc.stdout or "")[-2000:]}
    return {"status": "ok", "src_path": str(src_path), "path": str(out_path)}


def render_d2(
    d2_source: str,
    out_dir: Path,
    *,
    basename: str = "schematic",
    fmt: str = "svg",
    theme: int = 0,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Render a D2 string to a designer-grade **SVG** via the ``d2`` binary (a single
    Go binary; ``curl -fsSL https://d2lang.com/install.sh | sh -s --``). SVG is D2's
    NATIVE output — crisp + themeable + browser-renderable (great for the web preview).
    PNG export needs a headless browser, so we default to SVG and keep graphviz PNG for
    the PDF. Lazy/graceful when ``d2`` is absent (always keeps the ``.d2`` source)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src_path = out_dir / f"{basename}.d2"
    src_path.write_text(d2_source, encoding="utf-8")
    if not d2_available():
        return {"status": "dependency_missing", "dependency": "d2",
                "src_path": str(src_path), "path": None,
                "note": "d2 not installed — `curl -fsSL https://d2lang.com/install.sh | sh -s --` for pretty SVG schematics."}
    out_path = out_dir / f"{basename}.{fmt}"
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["d2", "--theme", str(theme), str(src_path), str(out_path)],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "src_path": str(src_path), "path": None,
                "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0 or not out_path.exists():
        return {"status": "error", "src_path": str(src_path), "path": None,
                "error": (proc.stderr or proc.stdout or "")[-2000:]}
    return {"status": "ok", "src_path": str(src_path), "path": str(out_path)}


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
    engine = str(args.get("engine", "dot")).lower()
    basename = str(args.get("basename", "schematic")).strip() or "schematic"
    # keep the filename safe + namespaced so it lands in the figure gallery
    basename = "schematic_" + "".join(c for c in basename if c.isalnum() or c in "-_")[:48]
    figs = _figdir(ctx)
    art = figs.parent
    if engine == "d2":
        out = render_d2(source, figs, basename=basename)          # native, pretty SVG
    elif engine == "mermaid":
        out = render_mermaid(source, figs, basename=basename)
    else:
        out = render_dot(source, figs, basename=basename)         # graphviz PNG (PDF-friendly)
    # return a path relative to the artifacts root (the files-browser/report key)
    if out.get("path"):
        rel = Path(out["path"]).relative_to(art).as_posix()
        out["figure"] = rel
    return out


def make_schematic_tool() -> Any:
    """The schematic tool: the agent writes a Graphviz DOT (or Mermaid) description of a
    workflow / pathway / mechanism, rendered deterministically into the figure gallery.

    Imported lazily so ``tools.schematic`` has no import dependency on the agents pkg.
    """
    from ..agents.research_harness import HarnessTool

    return HarnessTool(
        "make_schematic",
        "Draw a SCHEMATIC diagram (analysis workflow, signaling pathway, or mechanism) "
        "by writing its structure as text — a deterministic renderer turns it into a "
        "figure in the gallery (NO AI draws pixels). Engines: `dot` (Graphviz, PNG, "
        "best for the PDF; default), `d2` (designer-grade SVG, best for web preview), "
        "`mermaid` (SVG/PNG). Use for conceptual figures, not data plots. Example DOT: "
        "'digraph { QC -> Clustering -> \"DE per cluster\" -> Enrichment }'.",
        {"type": "object", "properties": {
            "source": {"type": "string", "description": "diagram text in the chosen engine's syntax"},
            "engine": {"type": "string", "enum": ["dot", "d2", "mermaid"]},
            "basename": {"type": "string", "description": "short name for the figure file"}},
         "required": ["source"]},
        _exec_make_schematic,
        reads_private_data=False, category="figure", requires=("graphviz",),
    )
