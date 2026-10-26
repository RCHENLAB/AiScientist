"""Deterministic SCHEMATIC figures — workflow/pathway/mechanism diagrams rendered
LOCALLY from text, with **no generative AI drawing pixels**.

The privacy/integrity story matches the data figures: a schematic is described as
text (a Graphviz **DOT** string) and a deterministic renderer turns that text —
byte-for-byte — into a PNG. The LLM only ever writes the *structured description*
(auditable text: nodes + edges), never paints the image, so there is no
diffusion-model hallucination / tampering surface. (Qwen3-VL, the optional vision
layer, is read-only QA on the rendered figure — it cannot generate one.)

The renderer is Graphviz (`dot`, a single ``apt install graphviz``), the one the report's
workflow figure uses too. Without it the tool returns ``{"status": "dependency_missing", ...}``
instead of crashing (same contract as ``reporting/report.py`` for pandoc and
``tools/scrna_pack.py`` for scanpy). Diagrams in the chat are not drawn here: the model writes a
```mermaid fence there and the browser renders it.
"""

from __future__ import annotations

import math
import re
import shutil
import struct
import subprocess
import textwrap
import zlib
from pathlib import Path
from typing import Any


def dot_available() -> bool:
    return shutil.which("dot") is not None


# --- deterministic DOT builders (pure Python — no AI, no external process) ----


def _esc(label: str) -> str:
    # The backslash first: in a DOT label it starts an escape (``\N`` is the node's own name).
    return str(label).replace("\\", "\\\\").replace('"', '\\"')


# A plan step is a paragraph: "**CellQC quality control** — Run `run_cellqc` on the … " and 1,900
# characters more. Graphviz sets a label on ONE line unless told otherwise, so each box was as wide
# as its paragraph: the three-step figure of run 1e887ed2bf8e came out 14,312 x 342 px and printed
# as a strip under 4 mm tall. A box now carries the step's number and its TITLE only (the bold span
# the planner opens every step with, else the first clause), on a few short lines. The full text of
# every step is in the plan and in the Methods; the figure shows their order.
_LABEL_WRAP = 24          # characters per line of a box
_LABEL_LINES = 3          # lines per box; a longer title ends in an ellipsis

_STEP_NUMBER = re.compile(r"^(?:step\s+)?\d{1,3}\s*[.):]\s+", re.I)
_BOLD_TITLE = re.compile(r"^\*\*(.+?)\*\*")
_CLAUSE_END = re.compile(r"(?<=[.;:!?])\s|\s[—–]\s|\s-\s")


def step_title(step: str) -> str:
    """A plan step's title, as plain text: its leading ``**bold**`` span, else its first clause."""
    text = _STEP_NUMBER.sub("", " ".join(str(step or "").split()))
    bold = _BOLD_TITLE.match(text)
    title = bold.group(1) if bold else _CLAUSE_END.split(text, maxsplit=1)[0]
    return re.sub(r"[*`]+", "", title).strip(" .;:,—–-")


def _box_label(step: str, number: int) -> str:
    """The DOT label of one box: "<number>. <title>", wrapped, lines joined by DOT's ``\\n``."""
    title = step_title(step) or "(untitled step)"
    rows = textwrap.wrap(f"{number}. {title}", width=_LABEL_WRAP, max_lines=_LABEL_LINES,
                         placeholder=" …")
    return "\\n".join(_esc(row) for row in rows)


# A long plan drawn as one column is the opposite strip: 20 boxes are 24 inches tall. So the chain
# is folded into rows that are read like a snake: left to right, down one box, right to left, and
# so on, which keeps every arrow short. Three boxes across is the widest that still prints at a
# readable size on a portrait page, so a longer plan grows downwards until it has `_MAX_ROWS` rows.
_MAX_ROWS = 9
# Rendered at twice the screen resolution so the text is sharp in the PDF. Graphviz does not write
# the resolution into the PNG, and without it TeX assumes 72 dpi and prints the figure 2.7 times its
# drawn size, so `render_workflow_schematic` records it in the file.
_WORKFLOW_DPI = 192


def boxes_per_row(n_steps: int) -> int:
    """How many boxes a row holds, so the figure is neither a tall nor a wide strip."""
    if n_steps <= 3:
        return 1                                   # a short plan reads best as one column
    if n_steps <= 8:
        return 2
    return max(3, math.ceil(n_steps / _MAX_ROWS))


def workflow_schematic_dot(steps: list[str], *, title: str | None = None, rankdir: str = "TB",
                           per_row: int | None = None) -> str:
    """Build a flowchart DOT from an ordered list of plan steps: one box per step, holding its
    number and title, in rows of ``per_row`` boxes (default: chosen from the number of steps).

    Fully deterministic: same steps → same DOT → (via ``dot``) same PNG. Used to draw
    the analysis methods/workflow diagram from the steps that actually ran — zero AI.
    """
    clean = [s for s in (str(x).strip() for x in steps) if s]
    n = len(clean)
    k = boxes_per_row(n) if per_row is None else max(1, min(int(per_row), max(n, 1)))
    # A modern, clean graphviz style: soft palette, orthogonal edges, generous spacing,
    # a teal accent for the start/end nodes. Still 100% deterministic, native PNG.
    lines = [
        "digraph workflow {",
        f"  rankdir={rankdir};",
        '  bgcolor="transparent";',
        f"  dpi={_WORKFLOW_DPI};",
        "  splines=ortho; nodesep=0.4; ranksep=0.45; pad=0.25;",
        '  node [shape=box, style="rounded,filled", fillcolor="#EAF1FB", color="#3B6EA5", '
        'penwidth=1.4, fontname="Helvetica", fontcolor="#1F2D3D", fontsize=11, margin="0.18,0.11", '
        "width=2.1, height=0.6];",
        '  edge [color="#3B6EA5", penwidth=1.3, arrowsize=0.8];',
    ]
    if title:
        lines.append(f'  labelloc="t"; label="{_esc(title)}"; '
                     'fontname="Helvetica-Bold"; fontsize=15; fontcolor="#1F2D3D";')
    last = n - 1
    for i, step in enumerate(clean):
        accent = ' fillcolor="#D6EFEC", color="#2C8C82"' if i in (0, last) and last > 0 else ""
        lines.append(f'  n{i} [label="{_box_label(step, i + 1)}"{accent}];')
    if k == 1:
        lines.extend(f"  n{i} -> n{i + 1};" for i in range(n - 1))
    else:
        rows = [list(range(first, min(first + k, n))) for first in range(0, n, k)]

        def column(row_no: int, place: int) -> int:
            return place if row_no % 2 == 0 else k - 1 - place     # odd rows run right to left

        for row_no, row in enumerate(rows):
            lines.append("  { rank=same; " + " ".join(f"n{i};" for i in row) + " }")
            for a, b in zip(row, row[1:]):
                # dot puts an edge's tail to the LEFT of its head within a row. A right-to-left row
                # is therefore written head first and drawn backwards: the arrow still points a → b.
                lines.append(f"  n{a} -> n{b};" if row_no % 2 == 0 else f"  n{b} -> n{a} [dir=back];")
        for row_no in range(len(rows) - 1):
            upper = {column(row_no, place): i for place, i in enumerate(rows[row_no])}
            lower = {column(row_no + 1, place): i for place, i in enumerate(rows[row_no + 1])}
            turn = (rows[row_no][-1], rows[row_no + 1][0])           # straight down, same column
            lines.append(f"  n{turn[0]} -> n{turn[1]};")
            # Invisible edges hold the other boxes in their columns.
            lines.extend(f"  n{upper[c]} -> n{lower[c]} [style=invis];"
                         for c in sorted(upper.keys() & lower.keys()) if (upper[c], lower[c]) != turn)
    lines.append("}")
    return "\n".join(lines) + "\n"


# --- the shape of a rendered figure -------------------------------------------------------------

# A figure longer than this many times its short side prints as a strip: scaled to the width of a
# page, an 8:1 image is 2 cm tall. The report leaves such a figure out (reporting/figure_checks.py
# holds the same limit for every figure of a run); here it is what a drawing is checked against as
# soon as it is rendered, so it can be laid out again instead.
MAX_ASPECT = 8.0
# What the workflow figure is laid out to stay within (width / height).
_READABLE_ASPECT = (0.5, 4.0)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(path: Path) -> tuple[int, int] | None:
    """(width, height) in pixels from a PNG's header; ``None`` when it is not a readable PNG."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return (width, height) if width > 0 and height > 0 else None


def _aspect_text(width: int, height: int) -> str:
    return f"{width / height:.1f}:1" if width >= height else f"1:{height / width:.1f}"


def _record_png_resolution(path: Path, dpi: int) -> None:
    """Write the resolution into a PNG (a ``pHYs`` chunk after the header) unless it has one, so a
    renderer prints the image at the size it was drawn."""
    data = path.read_bytes()
    if data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        return
    at = 8
    while at + 8 <= len(data):                     # the chunks before the image data
        length, kind = struct.unpack(">I4s", data[at:at + 8])
        if kind == b"pHYs":
            return
        if kind in (b"IDAT", b"IEND"):
            break
        at += 12 + length
    per_metre = round(dpi / 0.0254)
    body = b"pHYs" + struct.pack(">IIB", per_metre, per_metre, 1)
    chunk = struct.pack(">I", 9) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    path.write_bytes(data[:33] + chunk + data[33:])            # the header chunk ends at byte 33


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
    out: dict[str, Any] = {"status": "ok", "dot_path": str(src_path), "path": str(out_path)}
    size = _png_size(out_path) if fmt == "png" else None
    if size:
        out["width"], out["height"] = size
        if max(size) / min(size) > MAX_ASPECT:
            out["warnings"] = [
                f"The figure is {size[0]} x {size[1]} px ({_aspect_text(*size)}): it would print as a "
                "thin strip, so the report will not embed it as it is. Draw it again in a shape that "
                "fits a page (break long labels with \\n, fold a long chain into rows with "
                "rank=same, or change rankdir) and render it under the same basename."]
    return out


def render_workflow_schematic(
    steps: list[str],
    out_dir: Path,
    *,
    title: str | None = None,
    basename: str = "workflow",
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Draw the report's workflow figure from the plan's steps and check the shape of what came
    out. The row length is chosen from the number of steps; if the rendered image is still outside
    the readable range (text metrics differ between hosts), it is laid out again with one box more
    or fewer per row, and the last layout stays. Returns ``render_dot``'s result plus ``per_row``."""
    clean = [s for s in (str(x).strip() for x in steps) if s]
    low, high = _READABLE_ASPECT
    k = boxes_per_row(len(clean))
    tried: set[int] = set()
    while True:
        tried.add(k)
        out = render_dot(workflow_schematic_dot(clean, title=title, per_row=k), out_dir,
                         basename=basename, timeout_s=timeout_s)
        out["per_row"] = k
        if out["status"] != "ok" or "width" not in out:
            return out
        ratio = out["width"] / out["height"]
        k_next = k + 1 if ratio < low else k - 1 if ratio > high else k
        if k_next == k or k_next in tried or not 1 <= k_next <= len(clean):
            _record_png_resolution(Path(out["path"]), _WORKFLOW_DPI)
            return out
        k = k_next


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
