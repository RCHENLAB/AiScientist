"""Which file of a shared dataset version a request means (RCHENLAB/AiScientist issue 114).

A shared dataset such as HECA holds many member files (13 ``.h5ad``: HRCA, Iris, Lens, ON, ... each as
scRNA and snRNA). Choosing the biggest one for every question silently answered "this retina atlas"
with the 19 GB snRNA file. The member is resolved from what the request says instead, using the
members' facets (tissue, modality):

* a file named in the request is that file;
* a tissue (``retina``, ``iris``, ``optic nerve``) and/or an assay (``scRNA``, ``snRNA``) narrows the
  members; exactly one per requested tissue and assay is a choice, two assays of one tissue are two
  bindings (a two-atlas request). A tissue is known by its ``tissue`` facet (on HECA the maintainers
  set ``retina``, ``optic nerve``, ``RPE/choroid``, ``trabecular meshwork / ciliary body``...), each
  part of a compound facet, the file-name prefix before the assay token (``HRCA``, ``ON``, ``TMCB``)
  and the aliases of either;
* anything else is ambiguous, and the run asks which file before it stages or reads anything. A
  tissue that no member covers is never replaced by another tissue.

This module only reads names and facets. It never touches the store or the network.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .shared_datasets import infer_facets

# What a request says about the assay. ``single-cell`` alone names no assay (it describes the whole
# lab), so only the explicit forms count.
_SN = re.compile(r"(?i)(?<![a-z0-9])(?:sn[- ]?rna(?:[- ]?seq)?|single[- ]nucle(?:us|i)(?: rna(?:[- ]?seq)?)?)(?![a-z0-9])")
_SC = re.compile(r"(?i)(?<![a-z0-9])(?:sc[- ]?rna(?:[- ]?seq)?|single[- ]cell rna(?:[- ]?seq)?)(?![a-z0-9])")

# Other words for a tissue (HRCA is the Human Retina Cell Atlas, TMCB the trabecular meshwork and
# ciliary body, ...). Keyed by a tissue facet or a file-name prefix with everything but letters and
# digits removed, lower-cased (``RPE/choroid`` and ``RPEChoroid`` are both ``rpechoroid``), so the
# maintainers' facets and the prefixes of the file names reach the same words.
TISSUE_ALIASES: dict[str, tuple[str, ...]] = {
    "hrca": ("retina", "retinal", "human retina cell atlas", "retina atlas"),
    "retina": ("retinal", "hrca", "human retina cell atlas", "retina atlas"),
    "on": ("optic nerve",),
    "opticnerve": ("on",),
    "rpechoroid": ("rpe", "choroid", "rpe/choroid", "rpe-choroid", "retinal pigment epithelium"),
    "ocularsurface": ("ocular surface", "cornea", "corneal", "conjunctiva"),
    "tmcb": ("trabecular meshwork", "ciliary body"),
    "trabecularmeshworkciliarybody": ("tmcb", "trabecular meshwork", "ciliary body"),
}

# The parts of a compound facet: ``RPE/choroid``, ``trabecular meshwork / ciliary body``.
_PARTS = re.compile(r"\s*(?:/|,|&|\band\b)\s*", re.IGNORECASE)


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


def _word_re(word: str) -> "re.Pattern[str]":
    """``word`` as a whole word. A one- or two-letter word (``ON``) must also be upper-case: ``on`` is
    an ordinary English word."""
    if len(word) <= 2:
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word.upper())}(?![A-Za-z0-9])")
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", re.IGNORECASE)


def _mentions(question: str, word: str) -> bool:
    """Is ``word`` a whole word of ``question``? (See ``_word_re`` for short words.)"""
    return _word_re(word).search(question) is not None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _tissue_of(f: dict) -> str:
    """The member's tissue: its ``tissue`` facet, else the file-name prefix before the assay token."""
    return str((f.get("facets") or {}).get("tissue") or infer_facets(f["name"]).get("tissue") or "")


def _modality_of(f: dict) -> str:
    return str((f.get("facets") or {}).get("modality") or infer_facets(f["name"]).get("modality") or "").lower()


def _tissue_words(files: list[dict]) -> "dict[str, set[str]]":
    """For each tissue (lower-cased, as ``_tissue_of`` gives it) the words a request may name it by:
    the facet, each part of a compound facet, the file-name prefix of each of its files, and the
    aliases of all of these."""
    words: dict[str, set[str]] = {}
    for f in files:
        tissue = _tissue_of(f)
        if not tissue:
            continue
        names = {tissue, *(p for p in _PARTS.split(tissue) if p)}
        prefix = infer_facets(f["name"]).get("tissue")
        if prefix:
            names.add(prefix)
        for n in list(names):
            names.update(TISSUE_ALIASES.get(_norm(n), ()))
        words.setdefault(tissue.lower(), set()).update(names)
    return words


def _tissues(question: str, files: list[dict]) -> list[str]:
    """The tissues (lower-cased) the request names, in the order named. A word inside a longer word
    of another tissue does not count: "retinal pigment epithelium" is the RPE, not the retina."""
    spans = []                                  # (start, end, tissue)
    for tissue, words in _tissue_words(files).items():
        for w in words:
            spans += [(m.start(), m.end(), tissue) for m in _word_re(w).finditer(question)]
    kept = [s for s in spans if not any(o[2] != s[2] and o[0] <= s[0] and s[1] <= o[1] and (o[1] - o[0]) > (s[1] - s[0])
                                        for o in spans)]
    first: dict[str, int] = {}
    for start, _, tissue in kept:
        first[tissue] = min(start, first.get(tissue, start))
    return sorted(first, key=first.__getitem__)


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
    tissues_l = _tissues(question, files)
    shown = {_tissue_of(f).lower(): _tissue_of(f) for f in files}
    tissues = [shown.get(t, t) for t in tissues_l]
    if not (mods or tissues_l):
        return Selection("ambiguous", [], names, reason="the request does not say which file")

    pool = [f for f in files
            if (not tissues_l or _tissue_of(f).lower() in tissues_l) and (not mods or _modality_of(f) in mods)]
    pool_names = [f["name"] for f in pool]
    if not pool:
        what = ", ".join([*(tissues or []), *mods]) or "the request"
        return Selection("ambiguous", [], names, reason=f"no file of the dataset matches {what}")

    wanted = (len(tissues_l) or 1) * (len(mods) or 1)
    pairs = {(_tissue_of(f).lower(), _modality_of(f)) for f in pool}
    if len(pool) == len(pairs) == wanted:
        order = {t: i for i, t in enumerate(tissues_l)}
        morder = {m: i for i, m in enumerate(mods)}
        pool.sort(key=lambda f: (morder.get(_modality_of(f), 0), order.get(_tissue_of(f).lower(), 0)))
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
