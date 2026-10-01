"""Phenotype (HPO) inference for the IRD line — upstream-agent-driven, NO human-in-the-loop.

Exomiser ranks candidate variants by how well a gene's known disease phenotypes match the PATIENT's
phenotype, supplied as HPO terms. Rather than forcing a researcher to enter HPO codes, the upstream
orchestrator infers them from the study description via :func:`infer_hpo_terms`, which matches the
described symptoms / disease names against a curated IRD phenotype table (``ird_hpo.tsv``) and picks
real HPO IDs — it never invents an ID. If nothing matches, the default root term
``HP:0000556`` (Retinal dystrophy) is returned so a run always has a phenotype and never blocks.

The inferred terms are meant to be surfaced in the run's Diagnostics for transparency.
"""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT_IRD_HPO: tuple[str, str] = ("HP:0000556", "Retinal dystrophy")
_TABLE = Path(__file__).resolve().parent / "ird_hpo.tsv"


def load_hpo_table() -> list[dict]:
    """Parse ``ird_hpo.tsv`` → ``[{id, name, keywords: [lowercased str]}]`` (comments/blanks skipped)."""
    rows: list[dict] = []
    for raw in _TABLE.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        hpo_id, name, kw = parts[0].strip(), parts[1].strip(), parts[2]
        keywords = [k.strip().lower() for k in kw.split("|") if k.strip()]
        rows.append({"id": hpo_id, "name": name, "keywords": keywords})
    return rows


def _keyword_in(keyword: str, text: str) -> bool:
    """Is ``keyword`` present in ``text`` as a WHOLE word? Anchored on alphanumeric boundaries rather
    than plain substring containment, so a short clinical alias cannot fire inside an unrelated word
    ('ird' in 'third', 'lca' in 'calcaneus', 'bbs' in 'ebbs'). Both are already lowercase."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def infer_hpo_terms(text: str, *, default: bool = True) -> list[tuple[str, str]]:
    """Infer patient HPO terms from a free-text study description (case-insensitive keyword match
    against the curated IRD table). Returns ``[(hpo_id, name), …]``, de-duplicated, order-stable.

    If nothing matches and ``default`` is True, returns ``[DEFAULT_IRD_HPO]`` — so an IRD run always
    has a phenotype for Exomiser (no forced human input). With ``default=False`` an all-miss returns
    ``[]`` (the caller decides). Only IDs from the curated table are ever returned; none are invented.
    """
    low = (text or "").lower()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for term in load_hpo_table():
        if term["id"] in seen:
            continue
        if any(_keyword_in(kw, low) for kw in term["keywords"]):
            out.append((term["id"], term["name"]))
            seen.add(term["id"])
    if not out and default:
        return [DEFAULT_IRD_HPO]
    return out
