"""Final-report reference formatting.

This module deliberately does **not** retrieve literature. Literature retrieval belongs to
``tools.literature_search`` and should happen as an explicit, accepted lab step. The final report
stage only formats and inserts citations produced by that step. If no accepted citation exists,
the manuscript says so honestly instead of performing a hidden fallback search.
"""

from __future__ import annotations

import re
from typing import Any

# The exact placeholder the manuscript writer leaves in the reserved slot (kept in sync with the
# gateway's ``_REFERENCES_PLACEHOLDER``). We replace this, or the whole ``## References`` body,
# with accepted citations from the run.
_PLACEHOLDER = "*Citations to be inserted by the literature module (PaperQA).*"

# Replace from the '## References' heading up to the next top-level/section heading or EOF.
_REFERENCES_BLOCK = re.compile(
    r"(?mis)^#{2}\s+references\b.*?(?=^\#{1,2}\s+\S|\Z)"
)
_STRAY_REFERENCES_BLOCK = re.compile(
    r"(?mis)^(?:#{1}|#{3,6})\s+references\b.*?(?=^\#{1,6}\s+\S|\Z)"
)


# Build the standard empty result when no accepted literature citations exist.
def empty_references(reason: str, *, query: str = "") -> dict[str, Any]:
    """Return the standard empty-reference result shape.

    This is used when the lab run did not produce any accepted ``literature_search`` citations.
    It does not trigger a new search.
    """
    return {
        "status": "empty",
        "tier": "none",
        "reason": reason,
        "query": query,
        "answer": "",
        "unfiltered_count": 0,
        "filtered_count": 0,
        "citations": [],
    }


# Inline-HTML tags Europe PMC embeds in titles (e.g. "<i>BRCA2</i>"), plus their HTML-escaped form.
_HTML_TAG_RE = re.compile(r"(?is)</?\s*[a-z][a-z0-9]*\s*/?>")


def _plain_text(value: str) -> str:
    """Strip inline HTML markup so a citation renders as CLEAN text, never literal '<i>' or the
    HTML-escaped '&lt;i&gt;' in the rendered PDF/DOCX. Unescapes entities first (so '&lt;i&gt;'
    becomes '<i>'), then removes the tags, then collapses the leftover whitespace."""
    import html

    value = _HTML_TAG_RE.sub("", html.unescape(value or ""))
    return re.sub(r"\s{2,}", " ", value).strip()


# Check whether a citation has a DOI or PMID that makes it traceable.
def _citation_has_identifier(citation: dict[str, Any]) -> bool:
    return bool(str(citation.get("doi") or "").strip() or str(citation.get("pmid") or "").strip())


# Normalize one citation record and fill missing URL/citation text when possible.
def _normalise_citation(citation: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(citation, dict) or not _citation_has_identifier(citation):
        return None
    doi = str(citation.get("doi") or "").strip()
    pmid = str(citation.get("pmid") or "").strip()
    url = str(citation.get("url") or "").strip()
    if not url:
        url = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    item = {
        "title": _plain_text(str(citation.get("title") or "")),
        "authors": _plain_text(str(citation.get("authors") or "")),
        "year": str(citation.get("year") or "").strip(),
        "journal": _plain_text(str(citation.get("journal") or "")),
        "doi": doi,
        "pmid": pmid,
        "url": url,
        "citation": _plain_text(str(citation.get("citation") or "")),
    }
    if not item["citation"]:
        title = item["title"].rstrip(". ")
        authors = item["authors"]
        first = authors.split(",")[0].strip() if authors else ""
        lead = f"{first} et al. " if first else ""
        year = f"({item['year']}) " if item["year"] else ""
        journal = f" {item['journal']}." if item["journal"] else ""
        tail = f" doi:{doi}" if doi else f" PMID:{pmid}"
        item["citation"] = f"{lead}{year}{title}.{journal}{tail}".strip()
    return item


# Convert accepted literature_search citations into the standard reference result shape.
def references_from_citations(
    citations: list[dict[str, Any]],
    *,
    query: str = "",
    reason: str = "reused accepted literature_search tool results",
    tier: str = "lab_literature_search",
) -> dict[str, Any]:
    """Create the standard reference result from citations already retrieved in this run."""
    normalised: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for citation in citations:
        item = _normalise_citation(citation)
        if item is None:
            continue
        key = (item["doi"].lower(), item["pmid"], item["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        normalised.append(item)
    if not normalised:
        return empty_references("no accepted literature_search citations from this run", query=query)
    return {
        "status": "ok",
        "tier": tier,
        "reason": reason,
        "query": query,
        "answer": "",
        "unfiltered_count": len(citations),
        "filtered_count": len(normalised),
        "citations": normalised,
    }


# DOI / PMID embedded inside a free-text citation string (PaperQA's formatted_citation), used to
# add a clickable link when the corpus citation carries one. Absence is fine — corpus provenance
# does not depend on an identifier (see references_from_corpus_citations).
_INLINE_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_INLINE_PMID_RE = re.compile(r"\bPMID[:\s]*(\d{4,12})\b", re.I)


# Convert accepted deep_literature (PaperQA/indexed corpus) citation STRINGS into the reference shape.
def references_from_corpus_citations(
    citations: list[str],
    *,
    query: str = "",
    answer: str = "",
    reason: str = "reused accepted deep_literature (indexed corpus) citations",
    tier: str = "lab_deep_literature",
) -> dict[str, Any]:
    """Create the standard reference result from free-text corpus citation strings.

    The corpus counterpart of :func:`references_from_citations`. deep_literature answers against the
    lab's OWN curated PubMedBERT corpus, so every citation string names a paper physically in the
    index — provenance is guaranteed by the corpus itself, and a DOI/PMID is therefore NOT required
    to admit a citation (unlike online Europe PMC, where the identifier guards against hallucinated
    preprints). A DOI/PMID is still EXTRACTED from the string when present, purely to attach a
    clickable link. This never fabricates: it only formats strings the tool actually returned."""
    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in citations:
        text = _plain_text(str(raw or ""))
        if not text:
            continue
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        doi_m = _INLINE_DOI_RE.search(text)
        pmid_m = _INLINE_PMID_RE.search(text)
        doi = doi_m.group(0).rstrip(".").strip() if doi_m else ""
        pmid = pmid_m.group(1) if pmid_m else ""
        url = (f"https://doi.org/{doi}" if doi
               else (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""))
        normalised.append({
            "title": "", "authors": "", "year": "", "journal": "",
            "doi": doi, "pmid": pmid, "url": url, "citation": text,
        })
    if not normalised:
        return empty_references("no accepted deep_literature citations from this run", query=query)
    return {
        "status": "ok",
        "tier": tier,
        "reason": reason,
        "query": query,
        "answer": answer or "",
        "unfiltered_count": len(citations),
        "filtered_count": len(normalised),
        "citations": normalised,
    }


# Render accepted citations, or an honest empty note, as a Markdown References section.
def format_references_section(result: dict[str, Any]) -> str:
    """Render the full ``## References`` section markdown.

    Real, numbered citations when present; an honest one-line note when this run produced no
    accepted literature citations. This function never fabricates a reference and never searches.
    """
    citations = result.get("citations") or []
    if not citations:
        return ("## References\n\n"
                "*No accepted literature-search citations were produced in this run.*\n")
    lines = ["## References", ""]
    for i, c in enumerate(citations, 1):
        cite = str(c.get("citation") or "").strip()
        url = str(c.get("url") or "").strip()
        lines.append(f"{i}. {cite}" + (f" {url}" if url and url not in cite else ""))
    return "\n".join(lines) + "\n"


# Inline numeric citation markers the report writer emits in prose (e.g. "[3]", "[1, 2]", "[4-7]").
# These numbers are model-invented narrative artifacts with NO stable link to any specific
# reference: the final numbered ``## References`` list is rebuilt independently from the run's
# accepted citations, so "[3]" in the body does not correspond to reference 3. Rather than render
# a false cross-reference, we strip the markers and keep the (real, corpus-backed) References list.
_INTEXT_CITATION_MARKER = re.compile(
    r"[ \t]*\[\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+(?:\s*[-–]\s*\d+)?)*\]"
)


def strip_intext_citation_markers(manuscript_md: str) -> str:
    """Remove model-invented inline citation markers ("[3]", "[1, 2]", "[4-7]") from a report body.

    The final References list is rebuilt independently from the run's accepted citations, so these
    inline numbers point to nothing reliable (see :data:`_INTEXT_CITATION_MARKER`). We strip them
    rather than print a citation number that disagrees with the reference list. The numbered
    ``## References`` list itself uses the "1." form (not "[1]") and Markdown links carry letters,
    so neither is affected. The marker match absorbs the space preceding it, so removing a
    citation leaves clean spacing without a global whitespace rewrite.
    """
    return _INTEXT_CITATION_MARKER.sub("", manuscript_md)


# Replace the manuscript's reserved References slot with the accepted citations.
def insert_references(manuscript_md: str, result: dict[str, Any]) -> str:
    """Substitute the run's accepted citations into the manuscript's ``## References`` slot.

    When the run produced NO accepted citations, the MANUSCRIPT is left clean: the reserved
    placeholder and any empty ``## References`` heading are removed rather than printing a
    "no citations were produced in this run" note into the final paper. That absence is a run
    degradation and belongs in the technical report's Diagnostics, not the manuscript
    (silent-degradation design). Dropping the section here also stops a References block the model
    may have hand-written from surviving when there are no *accepted* citations to back it — this
    module never fabricates a reference."""
    manuscript_md = _STRAY_REFERENCES_BLOCK.sub("", manuscript_md)
    citations = result.get("citations") or []
    if not citations:
        cleaned = manuscript_md.replace(_PLACEHOLDER, "")
        cleaned = _REFERENCES_BLOCK.sub("", cleaned)      # drop the now-empty References section
        return cleaned.rstrip() + "\n"

    section = format_references_section(result).rstrip() + "\n"
    if _PLACEHOLDER in manuscript_md:
        body = section.split("\n", 2)[2] if section.count("\n") >= 2 else ""
        return manuscript_md.replace(_PLACEHOLDER, body.strip() or _PLACEHOLDER, 1)

    if _REFERENCES_BLOCK.search(manuscript_md):
        return _REFERENCES_BLOCK.sub(section + "\n", manuscript_md, count=1)

    return manuscript_md.rstrip() + "\n\n" + section


# Explain missing references in the technical report without running a hidden fallback search.
def degradation_note(result: dict[str, Any]) -> str | None:
    """Technical-report note for the reference insertion path.

    Clean accepted citations need no note. Empty results are documented internally so the user can
    see that no hidden fallback search ran and no citation was fabricated.
    """
    if result.get("status") == "ok" and result.get("citations"):
        return None
    reason = str(result.get("reason") or "").strip() or "no accepted literature_search citations"
    return (
        "**Literature references were not inserted from a hidden fallback search.** "
        f"{reason}. The manuscript's References section is an honest 'none produced in this run' "
        "note unless an accepted `literature_search` step provides DOI/PMID-backed citations; "
        "no citation was fabricated."
    )
