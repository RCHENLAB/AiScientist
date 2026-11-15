"""Which file of a shared dataset version a request means (RCHENLAB/AiScientist issue 114).

A shared dataset such as HECA holds many member files (13 ``.h5ad``: HRCA, Iris, Lens, ON, ... each as
scRNA and snRNA). Choosing the biggest one for every question silently answered "this retina atlas"
with the 19 GB snRNA file. The member is resolved from what the request says instead, using the
members' facets (tissue, modality):

* a file named in the request is that file;
* a tissue (``retina``, ``iris``, ``optic nerve``) and/or an assay (``scRNA``, ``snRNA``) narrows the
  members; exactly one per requested tissue and assay is a choice, two assays of one tissue are two
  bindings (a two-atlas request);
* anything else is ambiguous, and the run asks which file before it stages or reads anything. A
  tissue that no member covers is never replaced by another tissue.

This module only reads names and facets. It never touches the store or the network.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# What a request says about the assay. ``single-cell`` alone names no assay (it describes the whole
# lab), so only the explicit forms count.
_SN = re.compile(r"(?i)(?<![a-z0-9])(?:sn[- ]?rna(?:[- ]?seq)?|single[- ]nucle(?:us|i)(?: rna(?:[- ]?seq)?)?)(?![a-z0-9])")
_SC = re.compile(r"(?i)(?<![a-z0-9])(?:sc[- ]?rna(?:[- ]?seq)?|single[- ]cell rna(?:[- ]?seq)?)(?![a-z0-9])")

# Other words for a tissue that a file name abbreviates (HRCA is the Human Retina Cell Atlas, TMCB the
# trabecular meshwork and ciliary body, ...). Keyed by the lower-cased tissue facet.
TISSUE_ALIASES: dict[str, tuple[str, ...]] = {
    "hrca": ("retina", "retinal", "human retina cell atlas", "retina atlas"),
    "on": ("optic nerve",),
    "rpechoroid": ("rpe", "choroid", "rpe/choroid", "rpe-choroid", "retinal pigment epithelium"),
    "ocularsurface": ("ocular surface", "cornea", "corneal", "conjunctiva"),
    "tmcb": ("trabecular meshwork", "ciliary body"),
}


@dataclass
class Selection:
    """``chosen``: the member names the request settles (in the order the request names them);
    ``candidates``: what the user is asked to pick from when ``status`` is ``ambiguous``."""

    status: str                                 # "chosen" | "ambiguous"
    chosen: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    how: str = ""                               # why: shown on the plan card and kept in the run record
    reason: str = ""                            # why the user is asked, for the question


def _modalities(question: str) -> list[str]:
    """``["snrna"]``, ``["scrna", "snrna"]``...: the assays the request names, in order."""
    found = sorted([(m.start(), "snrna") for m in _SN.finditer(question)]
                   + [(m.start(), "scrna") for m in _SC.finditer(question)])
    out: list[str] = []
    for _, name in found:
        if name not in out:
            out.append(name)
    return out


def _mentions(question: str, word: str) -> bool:
    """Is ``word`` a whole word of ``question``? A one- or two-letter word (``ON``) must also be
    upper-case: ``on`` is an ordinary English word."""
    flags = 0 if len(word) <= 2 else re.IGNORECASE
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(word.upper() if len(word) <= 2 else word)}(?![A-Za-z0-9])",
                     question, flags) is not None


def _first_at(question: str, words: tuple[str, ...]) -> int:
    positions = [m.start() for w in words for m in [re.search(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])",
                                                             question, 0 if len(w) <= 2 else re.IGNORECASE)] if m]
    return min(positions) if positions else 10 ** 9


def _tissues(question: str, tissues: set[str]) -> list[str]:
    """The tissue facets the request names (by the facet itself or an alias), in the order named."""
    hits = []
    for tissue in tissues:
        words = (tissue, *TISSUE_ALIASES.get(tissue.lower(), ()))
        if any(_mentions(question, w) for w in words):
            hits.append((_first_at(question, words), tissue))
    return [t for _, t in sorted(hits)]


def _named(question: str, files: list[dict]) -> list[str]:
    """Members the request names by file name (with or without the extension), in the order named."""
    hits = []
    for f in files:
        name = f["name"]
        stem = os.path.basename(name).split(".")[0]
        for word in {name, os.path.basename(name), stem}:
            m = re.search(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", question, re.IGNORECASE)
            if m:
                hits.append((m.start(), name))
                break
    return [n for _, n in sorted(hits)]


def _facet(f: dict, key: str) -> str:
    return str((f.get("facets") or {}).get(key) or "").lower()


def select(question: str, files: list[dict]) -> Selection:
    """Resolve ``question`` against the member ``files`` (``{"name", "size_bytes", "facets"}``): the
    files that can be analysed, not a README. See the module docstring for the rules."""
    question = question or ""
    names = [f["name"] for f in files]
    if not files:
        return Selection("ambiguous", reason="the dataset has no file a run can analyse")
    if len(files) == 1:
        return Selection("chosen", names, names, how="the only analysable file of the dataset")
    named = _named(question, files)
    if named:
        return Selection("chosen", named, names, how="named in the request")

    mods = _modalities(question)
    tissues = _tissues(question, {str((f.get("facets") or {}).get("tissue") or "") for f in files} - {""})
    tissues_l = [t.lower() for t in tissues]
    if not (mods or tissues_l):
        return Selection("ambiguous", [], names, reason="the request does not say which file")

    pool = [f for f in files
            if (not tissues_l or _facet(f, "tissue") in tissues_l) and (not mods or _facet(f, "modality") in mods)]
    pool_names = [f["name"] for f in pool]
    if not pool:
        what = ", ".join([*(tissues or []), *mods]) or "the request"
        return Selection("ambiguous", [], names, reason=f"no file of the dataset matches {what}")

    wanted = (len(tissues_l) or 1) * (len(mods) or 1)
    pairs = {(_facet(f, "tissue"), _facet(f, "modality")) for f in pool}
    if len(pool) == len(pairs) == wanted:
        order = {t: i for i, t in enumerate(tissues_l)}
        morder = {m: i for i, m in enumerate(mods)}
        pool.sort(key=lambda f: (morder.get(_facet(f, "modality"), 0), order.get(_facet(f, "tissue"), 0)))
        how = "the assay and tissue named in the request" if tissues_l and mods else (
            "the assay named in the request" if mods else "the tissue named in the request")
        return Selection("chosen", [f["name"] for f in pool], pool_names, how=how)
    said = " and ".join([*tissues, *mods])
    return Selection("ambiguous", [], pool_names,
                     reason=f"“{said}” matches {len(pool)} files")


def describe(f: dict) -> str:
    """One line for a choice: the file, its tissue and assay, cells and size."""
    fac = f.get("facets") or {}
    bits = [str(fac[k]) for k in ("tissue", "modality") if fac.get(k)]
    if fac.get("n_obs") is not None:
        bits.append(f"{int(fac['n_obs']):,} cells")
    size = int(f.get("size_bytes") or 0)
    if size:
        bits.append(f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB")
    return f["name"] + (f" — {', '.join(bits)}" if bits else "")
