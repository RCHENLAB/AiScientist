"""Known-gene panels for disease-focused variant studies.

A panel is a plain-text file (one HGNC symbol per line, ``#`` comments + blanks ignored) shipped
next to this module. The variant line passes the loaded symbols as ``annotate_variants``' ``genes``
so the offline VEP path restricts BEFORE annotation (known-gene-first — see the IRD-parity roadmap,
``docs/ird_pipeline_parity_roadmap.md``). Loading is source-relative (``__file__``) so it works when
the app runs from the checkout, as it does in prod.
"""
from __future__ import annotations

from pathlib import Path

_PANEL_DIR = Path(__file__).resolve().parent

# name -> filename. Aliases point at the same file so callers can say "ird" or "ird_retnet".
_PANELS: dict[str, str] = {
    "ird": "ird_retnet.txt",
    "ird_retnet": "ird_retnet.txt",
    "retnet": "ird_retnet.txt",
}


def available_panels() -> list[str]:
    """The panel names that can be passed to :func:`load_gene_panel` (deduplicated by file)."""
    seen: set[str] = set()
    out: list[str] = []
    for name, fname in _PANELS.items():
        if fname not in seen:
            seen.add(fname)
            out.append(name)
    return out


def load_gene_panel(name: str) -> list[str]:
    """Return the sorted, de-duplicated gene symbols for panel ``name`` (case-insensitive).

    Raises ``KeyError`` for an unknown panel and ``FileNotFoundError`` if the asset is missing —
    both are programming/deploy errors that should surface loudly, not silently yield an empty
    (and therefore genome-wide) restriction.
    """
    key = (name or "").strip().lower()
    if key not in _PANELS:
        raise KeyError(f"unknown gene panel {name!r}; available: {sorted(set(_PANELS))}")
    path = _PANEL_DIR / _PANELS[key]
    genes: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        genes.add(line)
    return sorted(genes)
