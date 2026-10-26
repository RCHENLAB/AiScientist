"""Render an analysis report (markdown) into nicely-formatted PDF **and** DOCX —
``pandoc`` + XeLaTeX for PDF, native pandoc for DOCX. Deterministic, **no AI**. Figures
referenced in the markdown by relative path are embedded; ``assets_root`` is the base
those relative paths resolve against (so the report can live in a ``report/`` subdir
while ``figures/`` / ``tables/`` sit at the bundle root). Degrades gracefully to
markdown-only when pandoc is absent.

The report text is written by a model, from a prompt, a dataset and papers that anyone may have
shaped, so every render treats it as DATA (see ``PANDOC_READER`` and ``_lua_filter``): no part of it
reaches the TeX engine as a command, and no file is embedded that is not a figure of this run. Every
pandoc command line in the package is built in this module; a test fails if one appears elsewhere.

Install on the gateway host:  apt install pandoc texlive-xetex   (DOCX needs only pandoc)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def pandoc_available() -> bool:
    return shutil.which("pandoc") is not None


# Default render format. ``build_pdf_report(format_overrides=...)`` shallow-merges over this,
# which is what lets the visual-review loop (aiscientist.reporting.visual_review) RE-RENDER with
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
# TeX distribution (texlive-xetex / tectonic both have it). The optional landscape rotation for
# very wide tables comes from ``pdflscape`` where it is installed and from ``lscape`` (part of
# LaTeX's own graphics bundle) where it is not: the production report image lacks ``pdflscape``,
# and requiring it made EVERY PDF render there fail and fall back to the gateway host.
def _table_header_tex(fmt: dict[str, Any]) -> str:
    tf = fmt["table_font"]
    parts = [
        r"\usepackage{etoolbox}",
        r"\IfFileExists{pdflscape.sty}{\usepackage{pdflscape}}{\usepackage{lscape}}",
        rf"\AtBeginEnvironment{{longtable}}{{{tf}}}",
        rf"\AtBeginEnvironment{{tabular}}{{{tf}}}",
        r"\setlength{\tabcolsep}{4pt}",
        r"\renewcommand{\arraystretch}{1.15}",
    ]
    # Cap oversized figures so a caption/label never collides with the next element.
    if fmt.get("figure_max_width"):
        parts.append(rf"\setkeys{{Gin}}{{width={fmt['figure_max_width']},keepaspectratio}}")
    return "\n".join(parts) + "\n"


# How the report markdown is read. Pandoc's own markdown, minus the two extensions that hand text to
# the output format unescaped: ``raw_tex`` (a TeX command anywhere in a paragraph) and
# ``raw_attribute`` (a block or span marked ``{=latex}`` / ``{=openxml}``). With both off such text is
# typeset as the characters it is made of. Math (``$...$``) is a separate extension and stays on;
# nothing the pipeline writes itself relies on either of the two.
PANDOC_READER = "markdown-raw_tex-raw_attribute"

# Files a report may embed: real image files inside the run's own folder, nothing else.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".tif", ".tiff",
                             ".bmp", ".webp"})
_MAX_IMAGES = 5000

# What a formula may contain. A formula is passed to TeX as written, so it is checked word by word:
# every control word must be one of these plain math commands, or the formula is printed as text.
# A list of what is allowed, not of what is dangerous: an unknown command is refused by default.
_MATH_WORDS = """
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa varkappa lambda mu nu
xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta
Lambda Xi Pi Sigma Upsilon Phi Psi Omega
pm mp times div cdot ast star circ bullet oplus ominus otimes oslash odot dagger ddagger cap cup
uplus sqcap sqcup vee wedge land lor setminus wr diamond amalg leq le geq ge neq ne equiv sim simeq
approx cong propto ll gg prec succ preceq succeq subset supset subseteq supseteq sqsubseteq
sqsupseteq in ni notin mid nmid parallel nparallel perp models vdash dashv asymp doteq bowtie
lesssim gtrsim leqslant geqslant nleq ngeq nless ngtr approxeq triangleq
to gets leftarrow rightarrow leftrightarrow Leftarrow Rightarrow Leftrightarrow longleftarrow
longrightarrow longleftrightarrow Longleftarrow Longrightarrow Longleftrightarrow mapsto longmapsto
hookleftarrow hookrightarrow uparrow downarrow updownarrow Uparrow Downarrow Updownarrow nearrow
searrow swarrow nwarrow rightleftharpoons iff implies impliedby xrightarrow xleftarrow
sum prod coprod int iint iiint oint bigcup bigcap bigvee bigwedge bigoplus bigotimes bigodot
biguplus bigsqcup limits nolimits
log ln lg exp sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth min max sup inf lim
liminf limsup arg det dim gcd hom ker deg Pr bmod pmod mod operatorname
frac dfrac tfrac cfrac binom dbinom tbinom sqrt overline underline overbrace underbrace overset
underset stackrel substack left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl
Biggr bigm Bigm begin end over choose atop
text textrm textit textbf textsf texttt textnormal textup mbox mathrm mathbf mathit mathsf mathtt
mathcal mathbb mathfrak mathscr mathnormal boldsymbol pmb rm bf it sf tt cal displaystyle textstyle
scriptstyle scriptscriptstyle mathbin mathrel mathop mathord mathopen mathclose mathpunct
hat widehat bar tilde widetilde vec dot ddot dddot acute grave breve check mathring overrightarrow
overleftarrow overleftrightarrow not
langle rangle lvert rvert lVert rVert vert Vert lfloor rfloor lceil rceil lbrace rbrace lbrack
rbrack backslash
ldots cdots vdots ddots dots dotsc dotsb dotsm dotsi infty partial nabla forall exists nexists
emptyset varnothing neg lnot top bot angle measuredangle triangle square blacksquare prime ell hbar
hslash imath jmath Re Im wp aleph complement therefore because surd flat natural sharp checkmark
colon quad qquad enspace thinspace medspace thickspace negthinspace negmedspace negthickspace
phantom hphantom vphantom smash strut mathstrut hline tag nonumber notag boxed color textcolor
"""
_MATH_ENVS = ("matrix pmatrix bmatrix Bmatrix vmatrix Vmatrix smallmatrix cases aligned gathered "
              "split array")


def _embeddable_images(root: Path) -> list[str]:
    """The image files a report rendered from ``root`` may embed, as paths relative to it.

    Pandoc embeds whatever a markdown image points at: for DOCX it copies the file into the
    document byte for byte, image or not, from any path the process can read. So the render is
    given the list of files that are images by name AND really live inside the run's folder (a link
    that resolves outside it is left out), and the filter drops every image that is not on it."""
    real_root = os.path.realpath(root)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in _IMAGE_SUFFIXES or any(ord(c) < 32 for c in name):
                continue
            full = os.path.join(dirpath, name)
            real = os.path.realpath(full)
            try:
                inside = os.path.commonpath([real, real_root]) == real_root
            except ValueError:   # different drives
                inside = False
            if not inside or not os.path.isfile(real):
                continue
            found.append(Path(full).relative_to(root).as_posix())
            if len(found) >= _MAX_IMAGES:
                return found
    return found


# The filter every render runs, written next to the report for that one render. Two passes.
#
# Pass 1 makes the model's text data, before any writer sees it:
#   - a raw node (output-format code) is dropped. The reader no longer produces any; this holds
#     even if a reader setting is changed later.
#   - a formula is typeset only if every command in it is plain math (``_MATH_WORDS``); otherwise
#     it is printed as the text it is. DOCX needs no check: its math never reaches a TeX engine.
#   - an image is kept only if it is one of this run's own image files (``_embeddable_images``):
#     no absolute path, no ``..``, no URL, no link out of the folder.
# Pass 2 is the wide-table fit and the figure-label fix below. Both add raw LaTeX of their own, after
# pass 1, on purpose.
def _lua_filter(fmt: dict[str, Any], images: list[str]) -> str:
    image_rows = "".join(f"  [{json.dumps(p, ensure_ascii=False)}] = true,\n" for p in images)
    return (
        "-- Written by aiscientist.reporting.report for one render. Do not edit.\n"
        "local IMAGES = {\n" + image_rows + "}\n"
        "local MATH_WORDS, MATH_ENVS = {}, {}\n"
        "for w in ([[" + _MATH_WORDS + "]]):gmatch('%S+') do MATH_WORDS[w] = true end\n"
        "for w in ([[" + _MATH_ENVS + "]]):gmatch('%S+') do MATH_ENVS[w] = true end\n"
        + _SANITIZE_LUA
        + _table_lua_filter(fmt)
        + _CAPTION_LUA
        + "return { sanitize, { Table = fit_table, Figure = relabel_figure } }\n"
    )


_SANITIZE_LUA = r"""
local function math_is_plain(s)
  if s:find('^^', 1, true) then return false end
  local i, n = 1, #s
  while i <= n do
    if s:sub(i, i) == '\\' then
      local word = s:match('^[A-Za-z]+', i + 1)
      if not word then
        i = i + 2                      -- a control symbol such as \, or \{
      elseif not MATH_WORDS[word] then
        return false
      else
        i = i + 1 + #word
        if word == 'begin' or word == 'end' then
          local env, stop = s:match('^%s*{([A-Za-z]+)}()', i)
          if not env or not MATH_ENVS[env] then return false end
          i = stop
        end
      end
    else
      i = i + 1
    end
  end
  return true
end

local function image_key(src)
  local s = src:gsub('%%(%x%x)', function(h) return string.char(tonumber(h, 16)) end)
  while s:sub(1, 2) == './' do s = s:sub(3) end
  return s
end

local sanitize = {
  RawBlock = function() return {} end,
  RawInline = function() return {} end,
  Image = function(el)
    if IMAGES[image_key(el.src)] then return nil end
    if #el.caption > 0 then return pandoc.Emph(el.caption) end
    return pandoc.Emph({ pandoc.Str('[image not embedded]') })
  end,
}
if FORMAT ~= 'docx' then
  sanitize.Math = function(el)
    if math_is_plain(el.text) then return nil end
    local d = el.mathtype == 'DisplayMath' and '$$' or '$'
    return pandoc.Code(d .. el.text .. d)
  end
end

"""


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
        "local function fit_table(tbl)\n"
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


# One label per figure. The manuscript writes its own ("![Figure 2. Workflow diagram …](…)", the
# number its prose refers to) and LaTeX adds another, so the PDF read "Figure 2: Figure 2. Workflow
# diagram …" (run 1e887ed2bf8e). The same rule as for section numbers: the text's numbering is the
# one that stays. For LaTeX the written label is taken off the caption and LaTeX is told to print
# that number, so the caption reads "Figure 2: Workflow diagram …" whatever the figure's position in
# the document. A caption with no label of its own (the technical report's) is numbered by LaTeX as
# before, and a DOCX caption is printed as written: pandoc adds no label there.
# The number goes into raw LaTeX, so it is matched as up to three digits, with an optional "S" before
# or one letter after, and nothing else.
# Pandoc 3 only (the report image and the dev machines): there a figure is a block of its own. Pandoc
# 2 has no such block, the filter entry is ignored, and the caption is printed as before.
_CAPTION_LUA = r"""
local DASHES = { ['-'] = true, ['–'] = true, ['—'] = true }

local function written_label(inl)
  if #inl < 3 or inl[1].t ~= 'Str' or inl[2].t ~= 'Space' or inl[3].t ~= 'Str' then return nil end
  local word = inl[1].text:lower()
  if word ~= 'figure' and word ~= 'fig.' and word ~= 'fig' then return nil end
  local id, mark = inl[3].text:match('^(S?%d%d?%d?%a?)([.:]?)$')
  if not id then return nil end
  if mark ~= '' then return id, 3 end
  if #inl >= 5 and inl[4].t == 'Space' and inl[5].t == 'Str' and DASHES[inl[5].text] then
    return id, 5                      -- "Figure 2 — Workflow …"
  end
  return nil                          -- "Figure 2 shows …" is a sentence, not a label
end

local function take_label(inlines)
  local id, used
  local first = inlines[1]
  if first and (first.t == 'Strong' or first.t == 'Emph') then
    id, used = written_label(first.content)          -- "**Figure 2.** Workflow …"
    if not id or used ~= #first.content then return nil end
    used = 1
  else
    id, used = written_label(inlines)
    if not id then return nil end
  end
  local start = used + 1
  while inlines[start] and (inlines[start].t == 'Space' or inlines[start].t == 'SoftBreak') do
    start = start + 1
  end
  local rest = pandoc.List()
  for i = start, #inlines do rest:insert(inlines[i]) end
  if #rest == 0 then return nil end   -- the label is the whole caption: leave it
  return id, rest
end

local function numbered(id, block)
  return {
    pandoc.RawBlock('latex', '\\renewcommand{\\thefigure}{' .. id .. '}'),
    block,
    pandoc.RawBlock('latex', '\\renewcommand{\\thefigure}{\\arabic{figure}}'),
  }
end

local function relabel_figure(fig)
  if not FORMAT:match('latex') then return nil end
  local caption = fig.caption
  local first = caption and caption.long and caption.long[1]
  if not first or (first.t ~= 'Plain' and first.t ~= 'Para') then return nil end
  local id, rest = take_label(first.content)
  if not id then return nil end
  first.content = rest
  fig.caption = caption
  return numbered(id, fig)
end

"""


def _markdown_cmd(md_path: Path, out_path: Path, lua_path: Path, extra: list[str]) -> list[str]:
    """The pandoc command line that renders report markdown: always through ``PANDOC_READER`` and
    the filter. The one place such a command is built (a test fails if another appears)."""
    return ["pandoc", f"--from={PANDOC_READER}", str(md_path), "-o", str(out_path),
            "--lua-filter", str(lua_path), *extra]


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


NO_HOST_TEX = ("the PDF is typeset only on HPC3, under your own cluster account, never on the "
               "AiScientist server")


def run_pandoc_without_tex(cmd: list[str], cwd: Path, out_path: Path,
                           timeout_s: float) -> tuple[bool, str]:
    """``_run_pandoc`` for a host where a TeX engine must not typeset a report: the DOCX renders
    (pandoc alone, on text the filter has already made data), the PDF is refused before anything
    is started. TeX is a programming language with file access, and the checks above are the only
    thing between it and the report text; on a shared host that is one layer too few."""
    if Path(out_path).suffix.lower() == ".pdf" or any(
            str(a).startswith("--pdf-engine") for a in cmd):
        return False, NO_HOST_TEX
    return _run_pandoc(cmd, cwd, out_path, timeout_s)


def rendered_text(path: str | Path, timeout_s: float = 120.0) -> str:
    """The plain text of a rendered DOCX ('' when it cannot be read). ``--sandbox``: pandoc reads
    the one file named here and nothing else."""
    path = Path(path)
    if path.suffix.lower() != ".docx" or not path.exists() or not pandoc_available():
        return ""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["pandoc", "--sandbox", "--from=docx", str(path), "--to=plain"],
            capture_output=True, text=True, timeout=timeout_s)
    except Exception:  # noqa: BLE001 - an advisory read; no text is a valid answer
        return ""
    return proc.stdout or ""


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
    # The title comes from the model's own heading: quoted as a string (JSON is valid YAML), so a
    # quote or a backslash in it neither breaks the block nor adds a key to it.
    header = f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\n---\n\n" if title else ""
    md_path.write_text(header + (markdown or ""), encoding="utf-8")

    # A local runner needs pandoc here; the HPC renderer runs it in its container instead.
    if run_pandoc in (_run_pandoc, run_pandoc_without_tex) and not pandoc_available():
        return {"status": "markdown_only", "md_path": str(md_path), "pdf_path": None, "docx_path": None,
                "note": "pandoc not installed — install `pandoc` (+ `texlive-xetex` for PDF)."}

    cwd = Path(assets_root) if assets_root else out_dir
    resource = ["--resource-path", str(cwd)]  # resolve relative figures/ from the bundle root
    paths: dict[str, str | None] = {"pdf_path": None, "docx_path": None}
    errors: list[str] = []

    # Written per render, for BOTH formats: (1) a preamble snippet (smaller table font) pulled in
    # via --include-in-header for the PDF, and (2) the Lua filter — the report text made data
    # (``_lua_filter``), then wide tables forced into page-width wrapping columns. Deterministic.
    header_path = out_dir / f".{basename}_tables.tex"
    header_path.write_text(_table_header_tex(fmt), encoding="utf-8")
    lua_path = out_dir / f".{basename}_tables.lua"
    lua_path.write_text(_lua_filter(fmt, _embeddable_images(cwd)), encoding="utf-8")

    if "pdf" in formats:
        pdf_path = out_dir / f"{basename}.pdf"
        cmd = _markdown_cmd(md_path, pdf_path, lua_path, [
            *resource,
            # No --number-sections: the LLM writes its own section numbers, so pandoc's
            # auto-numbering on top double-numbers. Keep the TOC, drop the auto-numbers.
            "--pdf-engine=xelatex", "--toc", "--toc-depth=3",
            "--include-in-header", str(header_path),
            "-V", f"geometry:margin={fmt['geometry_margin']}", "-V", f"fontsize={fmt['fontsize']}",
            "-V", "colorlinks=true", "-V", "linkcolor=blue", "-V", "urlcolor=blue",
        ])
        ok, err = run_pandoc(cmd, cwd, pdf_path, timeout_s)
        paths["pdf_path"] = str(pdf_path) if ok else None
        if not ok:
            errors.append(f"pdf: {err}")

    if "docx" in formats:
        docx_path = out_dir / f"{basename}.docx"
        cmd = _markdown_cmd(md_path, docx_path, lua_path, [*resource, "--toc"])
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
