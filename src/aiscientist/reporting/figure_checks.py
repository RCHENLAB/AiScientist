"""A figure that would print as a strip is kept out of the rendered reports.

Scaled to the width of a page, an image 8 times wider than it is tall is 2 cm high. The workflow
figure of run 1e887ed2bf8e was 14,312 x 342 px (42:1): it printed under 4 mm tall, and the visual
review called the page clean. Whether an image is that elongated is a fact of its header, so it is
decided here, by arithmetic, before a report is written or rendered:

- such a figure is not offered to the report writers (``embeddable_figures``);
- an embed of one that reaches the final text anyway is removed (``remove_strip_embeds``);
- what was left out, with its size, is written to ``process/figure_checks.md`` (``write_note``) and
  handed to the technical report's Diagnostics (``diagnostics_block``).

The file itself stays in ``figures/`` and in the report's output index: only the embedding is
refused. PNG only — every figure the analysis tools and the schematic renderer write is one.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# A figure longer than this many times its short side is a strip. tools/make_schematic holds the
# same limit for a drawing it has just rendered, so that one can be laid out again instead.
MAX_ASPECT = 8.0

NOTE_NAME = "figure_checks.md"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path: Path) -> tuple[int, int] | None:
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


def is_strip(width: float, height: float) -> bool:
    """True when one side is more than ``MAX_ASPECT`` times the other."""
    return width > 0 and height > 0 and max(width, height) / min(width, height) > MAX_ASPECT


def aspect_text(width: float, height: float) -> str:
    return f"{width / height:.1f}:1" if width >= height else f"1:{height / width:.1f}"


def _figure_files(art: Path) -> list[Path]:
    figs = Path(art) / "figures"
    return sorted(figs.glob("*.png")) if figs.is_dir() else []


def strip_figures(art: Path) -> list[dict[str, Any]]:
    """The run's figures that are too elongated to print: ``{figure, width, height, aspect}``,
    with ``figure`` relative to the artifacts folder (``figures/workflow.png``)."""
    found: list[dict[str, Any]] = []
    for f in _figure_files(art):
        size = png_size(f)
        if size and is_strip(*size):
            found.append({"figure": f"figures/{f.name}", "width": size[0], "height": size[1],
                          "aspect": aspect_text(*size)})
    return found


def embeddable_figures(art: Path) -> list[Path]:
    """The figures a report may embed: the run's PNGs without the strips. A file whose size cannot
    be read is kept (the check refuses only what it has measured)."""
    kept: list[Path] = []
    for f in _figure_files(art):
        size = png_size(f)
        if not (size and is_strip(*size)):
            kept.append(f)
    return kept


# One markdown image, with the attribute block pandoc allows after it: ![alt](path "title"){...}.
# The alt text may hold one level of brackets ("… cells [n = 5]").
_EMBED = re.compile(r'!\[(?:[^\[\]\\]|\\.|\[[^\[\]]*\])*\]'
                    r'\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)(?:\{[^}\n]*\})?')


def remove_strip_embeds(md: str, art: Path) -> tuple[str, list[str]]:
    """Remove from ``md`` every embed of a PNG inside ``art`` that is a strip. Returns the text and
    the paths removed (as written). Other images, and anything outside the run's folder, are left
    to the render filter, which decides what may be embedded at all."""
    if not md or "![" not in md:
        return md, []
    root = os.path.realpath(art)
    removed: list[str] = []

    def replace(match: re.Match[str]) -> str:
        src = match.group(1)
        if "://" in src or not src.lower().endswith(".png"):
            return match.group(0)
        real = os.path.realpath(os.path.join(root, unquote(src)))
        try:
            inside = os.path.commonpath([real, root]) == root
        except ValueError:   # different drives
            inside = False
        if not inside:
            return match.group(0)
        size = png_size(Path(real))
        if not (size and is_strip(*size)):
            return match.group(0)
        removed.append(src)
        return ""

    out = _EMBED.sub(replace, md)
    if removed:
        out = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", out)     # the blank lines an embed leaves
    return out, removed


def diagnostics_block(strips: list[dict[str, Any]]) -> str:
    """What the technical report's Diagnostics says about the figures left out ('' when none)."""
    if not strips:
        return ""
    lines = [
        "### Figures left out of the reports (aspect-ratio check)",
        "",
        f"A figure longer than {MAX_ASPECT:g} times its short side prints as a strip, so it is not "
        "embedded in the manuscript or in this report. The file is in the results bundle.",
        "",
        "| Figure | Size (px) | Aspect |",
        "| --- | --- | --- |",
    ]
    lines += [f"| `{s['figure']}` | {s['width']:,} x {s['height']:,} | {s['aspect']} |" for s in strips]
    return "\n".join(lines)


def write_note(art: Path, strips: list[dict[str, Any]]) -> Path | None:
    """Write ``process/figure_checks.md`` for the figures left out; with none, remove a note an
    earlier pass over the same run left behind. Returns the note's path, or ``None``."""
    note = Path(art) / "process" / NOTE_NAME
    if not strips:
        try:
            note.unlink()
        except OSError:
            pass
        return None
    body = (
        "# Figure checks\n\n"
        f"Checked before the reports were written: the pixel size of every `figures/*.png`. "
        f"A figure longer than {MAX_ASPECT:g} times its short side would print as a strip "
        "(scaled to the width of a page it is under 2 cm tall), so it was not offered to the report "
        "writers, and an embed of it is removed before rendering. The files are unchanged.\n\n"
        "| Figure | Size (px) | Aspect | Action |\n"
        "| --- | --- | --- | --- |\n"
        + "".join(f"| `{s['figure']}` | {s['width']:,} x {s['height']:,} | {s['aspect']} | "
                  "left out of the reports |\n" for s in strips)
    )
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(body, encoding="utf-8")
    return note
