"""Render an analysis report (markdown) into nicely-formatted PDF **and** DOCX —
``pandoc`` + XeLaTeX for PDF, native pandoc for DOCX. Deterministic, **no AI**. Figures
referenced in the markdown by relative path are embedded; ``assets_root`` is the base
those relative paths resolve against (so the report can live in a ``report/`` subdir
while ``figures/`` / ``tables/`` sit at the bundle root). Degrades gracefully to
markdown-only when pandoc is absent.

Install on the gateway host:  apt install pandoc texlive-xetex   (DOCX needs only pandoc)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def pandoc_available() -> bool:
    return shutil.which("pandoc") is not None


# Default render format. ``build_pdf_report(format_overrides=...)`` shallow-merges over this,
# which is what lets the visual-review loop (bioagent.tools.visual_review) RE-RENDER with
# escalated settings when a VL pass finds text overlap / clipping / table overflow. Every knob
# here is a deterministic pandoc/LaTeX setting — the render itself stays AI-free.
DEFAULT_FORMAT: dict[str, Any] = {
    "fontsize": "11pt",           # body font (-> "10pt" when body text overlaps/overflows)
    "geometry_margin": "1in",     # page margin (-> "0.75in" when text is clipped at the edge)
    "table_font": r"\footnotesize",   # wide-table font (-> \scriptsize -> \tiny on overflow)
    "wide_table_cols": 4,         # wrap tables with >= this many columns (lower to wrap more)
    "landscape_wide_tables": False,   # rotate wide tables to landscape (last resort for width)
    "figure_max_width": None,     # cap figure width, e.g. r"0.85\linewidth" (fig/caption clash)
}


def _resolve_format(overrides: dict[str, Any] | None) -> dict[str, Any]:
    fmt = dict(DEFAULT_FORMAT)
    if overrides:
        fmt.update({k: v for k, v in overrides.items() if k in DEFAULT_FORMAT})
    return fmt


# LaTeX injected into the PDF preamble so wide data tables (pathway enrichment, marker
# panels) degrade gracefully instead of overprinting adjacent columns: shrink table type,
# let long cells wrap, and keep long tables left-aligned. ``etoolbox`` ships with every
# TeX distribution (texlive-xetex / tectonic both have it). ``pdflscape`` (also standard)
# powers the optional landscape rotation for very wide tables.
def _table_header_tex(fmt: dict[str, Any]) -> str:
    tf = fmt["table_font"]
    parts = [
        r"\usepackage{etoolbox}",
        r"\usepackage{pdflscape}",
        rf"\AtBeginEnvironment{{longtable}}{{{tf}}}",
        rf"\AtBeginEnvironment{{tabular}}{{{tf}}}",
        r"\setlength{\tabcolsep}{4pt}",
        r"\renewcommand{\arraystretch}{1.15}",
    ]
    # Cap oversized figures so a caption/label never collides with the next element.
    if fmt.get("figure_max_width"):
        parts.append(rf"\setkeys{{Gin}}{{width={fmt['figure_max_width']},keepaspectratio}}")
    return "\n".join(parts) + "\n"


# Deterministic overflow fix (NO vision model): a pandoc Lua filter that forces every wide
# table (>= ``wide_table_cols`` columns) into equal page-width ``p{}`` columns. Pandoc then
# emits wrapping paragraph columns sized to \linewidth, so long cells wrap onto extra lines
# instead of overrunning the page and overprinting their neighbours. Narrower tables keep
# pandoc's content-sized default. When ``landscape_wide_tables`` is set, those wide tables are
# additionally wrapped in a ``landscape`` environment (PDF). Applies to PDF and DOCX alike.
def _table_lua_filter(fmt: dict[str, Any]) -> str:
    threshold = int(fmt["wide_table_cols"])
    landscape = "true" if fmt.get("landscape_wide_tables") else "false"
    return (
        "local THRESHOLD = %d\n"
        "local LANDSCAPE = %s\n"
        "function Table(tbl)\n"
        "  local n = #tbl.colspecs\n"
        "  if n >= THRESHOLD then\n"
        "    for i = 1, n do\n"
        "      tbl.colspecs[i] = { tbl.colspecs[i][1], 1.0 / n }\n"
        "    end\n"
        "    if LANDSCAPE and FORMAT:match('latex') then\n"
        "      return { pandoc.RawBlock('latex', '\\\\begin{landscape}'), tbl,\n"
        "               pandoc.RawBlock('latex', '\\\\end{landscape}') }\n"
        "    end\n"
        "  end\n"
        "  return tbl\n"
        "end\n"
    ) % (threshold, landscape)


def _run_pandoc(cmd: list[str], cwd: Path, out_path: Path, timeout_s: float) -> tuple[bool, str]:
    """Run one pandoc invocation. Returns (ok, error_tail)."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - report any failure, keep the .md
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0 or not out_path.exists():
        return False, (proc.stderr or proc.stdout or "")[-2000:]
    return True, ""


def build_pdf_report(
    markdown: str,
    out_dir: Path,
    *,
    title: str | None = None,
    basename: str = "report",
    timeout_s: float = 180.0,
    assets_root: Path | None = None,
    formats: tuple[str, ...] = ("pdf", "docx"),
    format_overrides: dict[str, Any] | None = None,
    render_fn: Any = None,
) -> dict[str, Any]:
    """Write ``<basename>.md`` and render the requested ``formats`` via pandoc.

    ``format_overrides`` shallow-merges over :data:`DEFAULT_FORMAT` to tune font size, margins,
    table font/wrap threshold, landscape rotation, and figure width — the knobs the visual
    review loop escalates to re-render away text overlap / clipping / table overflow.

    Returns ``{status, md_path, pdf_path|None, docx_path|None, ...}``:
      - ``ok``            — at least one rendered format succeeded.
      - ``markdown_only`` — pandoc absent; only the .md was written.
      - ``pdf_failed`` / ``error`` — pandoc ran but every format failed; the .md remains.
    """
    # Injectable renderer with the same (cmd, cwd, out_path, timeout_s) -> (ok, err) contract as
    # ``_run_pandoc``. Default = local subprocess; the gateway passes a SlurmReportRenderer to run
    # pandoc/xelatex as an HPC3 CPU job instead (report_on_hpc), keeping texlive off the eyeserver.
    run_pandoc = render_fn or _run_pandoc
    fmt = _resolve_format(format_overrides)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{basename}.md"
    header = f'---\ntitle: "{title}"\n---\n\n' if title else ""
    md_path.write_text(header + (markdown or ""), encoding="utf-8")

    if render_fn is None and not pandoc_available():   # HPC renderer runs pandoc in-container instead
        return {"status": "markdown_only", "md_path": str(md_path), "pdf_path": None, "docx_path": None,
                "note": "pandoc not installed — install `pandoc` (+ `texlive-xetex` for PDF)."}

    cwd = Path(assets_root) if assets_root else out_dir
    resource = ["--resource-path", str(cwd)]  # resolve relative figures/ from the bundle root
    paths: dict[str, str | None] = {"pdf_path": None, "docx_path": None}
    errors: list[str] = []

    # Wide-table safety net: (1) a preamble snippet (smaller table font) pulled in via
    # --include-in-header for the PDF, and (2) a Lua filter that forces wide tables into
    # page-width wrapping columns, applied to BOTH formats. Both are deterministic — no AI.
    header_path = out_dir / f".{basename}_tables.tex"
    header_path.write_text(_table_header_tex(fmt), encoding="utf-8")
    lua_path = out_dir / f".{basename}_tables.lua"
    lua_path.write_text(_table_lua_filter(fmt), encoding="utf-8")
    table_fit = ["--lua-filter", str(lua_path)]

    if "pdf" in formats:
        pdf_path = out_dir / f"{basename}.pdf"
        cmd = [
            "pandoc", str(md_path), "-o", str(pdf_path), *resource, *table_fit,
            # No --number-sections: the LLM writes its own section numbers, so pandoc's
            # auto-numbering on top double-numbers. Keep the TOC, drop the auto-numbers.
            "--pdf-engine=xelatex", "--toc", "--toc-depth=3",
            "--include-in-header", str(header_path),
            "-V", f"geometry:margin={fmt['geometry_margin']}", "-V", f"fontsize={fmt['fontsize']}",
            "-V", "colorlinks=true", "-V", "linkcolor=blue", "-V", "urlcolor=blue",
        ]
        ok, err = run_pandoc(cmd, cwd, pdf_path, timeout_s)
        paths["pdf_path"] = str(pdf_path) if ok else None
        if not ok:
            errors.append(f"pdf: {err}")

    if "docx" in formats:
        docx_path = out_dir / f"{basename}.docx"
        cmd = ["pandoc", str(md_path), "-o", str(docx_path), *resource, *table_fit, "--toc"]
        ok, err = run_pandoc(cmd, cwd, docx_path, timeout_s)
        paths["docx_path"] = str(docx_path) if ok else None
        if not ok:
            errors.append(f"docx: {err}")

    rendered_any = paths["pdf_path"] or paths["docx_path"]
    return {
        "status": "ok" if rendered_any else "pdf_failed",
        "md_path": str(md_path),
        **paths,
        "error": "; ".join(errors)[-2000:] if errors else None,
    }
