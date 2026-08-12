"""Lightweight REAL-citation lookup via the Europe PMC REST API (free, no API key,
biomedical-focused). This is the interim, zero-heavy-deps literature backend — it returns
*real* papers (title / authors / year / journal / DOI / PMID / url) so the report can cite
verified sources instead of fabricating them. It is the precursor to the planned paper-qa
tool (which will add full-text retrieval + grounded RCS on top of search like this).

Privacy: only the model-composed keyword query (public terms — genes, pathways, disease,
cell type) leaves the host; never the dataset. The query is a short string, not raw data.
No external MODEL API is used — Europe PMC is a public data API (external retrieval is
allowed). Degrades gracefully (returns ``status="error"``) when offline.
"""

from __future__ import annotations

import re
from typing import Any

_EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_HTML_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_QUERY_MAX_WORDS = 18
_QUERY_MAX_CHARS = 180
#throw a series of garbage words
_QUERY_STOPWORDS = {
    "a", "about", "above", "after", "an", "analysis", "analyze", "and", "annotations", "any",
    "attached", "backed", "based", "biology", "brief", "cell", "celltype", "changes",
    "citation", "citations", "clustering", "compare", "conduct", "context", "contextual",
    "dataset", "datasets", "deg", "do", "doi", "each", "evidence", "existing", "expression",
    "figure", "figures", "file", "files", "final", "finish", "for", "from", "ground",
    "grounding", "h5ad", "include", "important", "invent", "key", "literature",
    "majorclass", "majorclass-specific", "marker", "markers", "must", "of", "or", "perform",
    "placeholder", "placeholders", "please", "pmid", "populate", "populated", "produce",
    "published", "qc", "real", "reference", "references", "relevant", "report", "research",
    "return", "rna-seq", "run", "sampleid", "search", "section", "shared", "short", "single",
    "single-cell", "specific", "summarize", "summary", "the", "to", "top", "upload", "uploaded",
    "use", "using", "validate", "validation", "weak", "wild-type", "with", "within", "without",
    "write", "wt",
    # genomics / VEP-pipeline method keywords — DB & tool names, genome assemblies, VCF-filter and
    # instruction words that mangled a variant-annotation question into a garbled query. None is a
    # useful biomedical search term on its own; the real query is the gene / pathway findings.
    "annotate", "assembly", "clinvar", "ensembl", "filter", "filtered", "first", "following",
    "gnomad", "grch37", "grch38", "hg19", "hg38", "pass", "provide", "silico", "then", "vcf", "vep",
    # generic research-goal / instruction verbs + bare determiners. These leak into a step label or
    # query from a THIN prompt (e.g. the preset default "complete the research") and are never a
    # useful Europe PMC search term on their own — the real query is the gene / pathway findings.
    "aim", "assess", "characterise", "characterize", "complete", "describe", "determine",
    "evaluate", "examine", "explore", "goal", "identify", "interpret", "interpretation",
    "investigate", "objective", "prioritisation", "prioritise", "prioritization", "prioritize",
    "task", "that", "these", "this", "those", "understand",
}
#abbreviation->full form
_TOKEN_NORMALIZATIONS = {
    "ko": "knockout",
    "wt": "wild type",
    "scrna": "single-cell RNA-seq",
    "scrnaseq": "single-cell RNA-seq",
    "rnaseq": "RNA-seq",
}

#some informal ones
_GENERIC_CITATION_PATTERNS = (
    r"^abstracts?\b",
    r"\babstract book\b",
    r"\bannual meeting\b",
    r"\bconference\b",
    r"\bcongress\b",
    r"\be-?posters?\b",
    r"\bmeeting abstracts?\b",
    r"^publication only$",
    r"\bsupplement\b",
)


#data clean
def _clean(text: str) -> str:
    """Europe PMC embeds HTML (e.g. <i>GENE</i>) in titles — strip tags for clean output."""
    return _HTML_TAG.sub("", text or "").strip()


def _format_citation(r: dict[str, Any]) -> str:
    """A compact, human-readable citation string the report can drop inline."""
    authors = (r.get("authors") or "").strip()
    first = authors.split(",")[0].strip() if authors else ""
    lead = f"{first} et al. " if first else ""
    year = f"({r['year']}) " if r.get("year") else ""
    title = (r.get("title") or "").rstrip(". ")
    journal = f" {r['journal']}." if r.get("journal") else ""
    tail = f" doi:{r['doi']}" if r.get("doi") else (f" PMID:{r['pmid']}" if r.get("pmid") else "")
    return f"{lead}{year}{title}.{journal}{tail}".strip()

# Convert long UI/user instructions into a short Europe PMC keyword query.
# This removes dataset/file terms, command words, and duplicate tokens.
def focus_literature_query(question: str) -> str:
    """Convert a broad UI/research prompt into a compact public literature query."""
    q = re.sub(r"\s+", " ", (question or "").strip())
    if not q:
        return ""
    words = q.split()
    has_instruction = re.search(
        r"(?i)\b(write|report|include|references?|citations?|evidence|search|return|do not|don't|run|qc|"
        r"clustering|dataset|annotate|annotation|filter|variants?|vcf|provide)\b",
        q,
    )
    if len(words) <= _QUERY_MAX_WORDS and len(q) <= _QUERY_MAX_CHARS and not has_instruction:
        return q

    text = q
    about = re.search(r"(?i)\babout\s+(.+)", text)
    if about:
        text = about.group(1)
    text = re.split(
        r"(?i)\b(?:do not|don't|include|run|perform|conduct|produce|generate|write|create)\b",
        text,
        maxsplit=1,
    )[0] or text
    text = re.sub(r"https?://\S+|`[^`]*`|[/\\][^\s]+", " ", text)
    text = re.sub(r"[_.,;:!?()\[\]{}<>|=+*&^%$#@~]", " ", text)

    picked: list[str] = []
    seen: set[str] = set()
    for raw in _WORD.findall(text):
        low = raw.lower().strip("-")
        if not low or low in _QUERY_STOPWORDS:
            continue
        if low.endswith((".h5ad", ".csv", ".tsv", ".txt", ".mtx")):
            continue
        if len(low) <= 2 and low not in _TOKEN_NORMALIZATIONS:
            continue
        token = _TOKEN_NORMALIZATIONS.get(low, raw)
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        picked.append(token)
        if len(picked) >= 10:
            break

    focused = " ".join(picked).strip()
    return focused or q[:_QUERY_MAX_CHARS].strip()

# Filter out generic meeting/poster/supplement records that are too weak for final References.
def _is_generic_citation(citation: dict[str, Any]) -> bool:
    title = str(citation.get("title") or "").strip().lower()
    if not title:
        return True
    return any(re.search(pattern, title) for pattern in _GENERIC_CITATION_PATTERNS)


#score
def _rank_key(citation: dict[str, Any], query_terms: set[str]) -> tuple[int, int]:
    title = str(citation.get("title") or "").lower()
    haystack = " ".join(str(citation.get(k) or "") for k in ("title", "journal", "authors")).lower()
    score = 0
    if citation.get("doi") or citation.get("pmid"):
        score += 2
    for term in query_terms:
        if term in title:
            score += 3
        elif term in haystack:
            score += 1
    if "review" in title:
        score += 2
    if "germline" in title:
        score += 2
    if "ddx41" in title:
        score += 4
    if _is_generic_citation(citation):
        score -= 100
    try:
        year_i = int(str(citation.get("year") or "")[:4])
    except ValueError:
        year_i = 0
    return score, year_i


def _matches_query_terms(citation: dict[str, Any], query_terms: set[str]) -> bool:
    if not query_terms:
        return True
    haystack = " ".join(str(citation.get(k) or "") for k in ("title", "journal", "authors")).lower()
    return any(term in haystack for term in query_terms)

#get the filtered results
def _clean_results(citations: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    terms = {
        t.lower()
        for t in _WORD.findall(query)
        if len(t) > 3 and t.lower() not in _QUERY_STOPWORDS
    }
    useful = [
        c for c in citations
        if not _is_generic_citation(c) and _matches_query_terms(c, terms)
    ]
    useful.sort(key=lambda c: _rank_key(c, terms), reverse=True)
    return useful[:limit]

#clear query-> request Europe PMC -> parse->filter->return
def search_europepmc(query: str, limit: int = 8, timeout_s: float = 20.0) -> dict[str, Any]:
    """Search Europe PMC and return real, structured citations. Never raises — returns
    ``{status: "error", error}`` on any failure so the agent loop can adapt or skip."""
    query = focus_literature_query(query)
    if not query:
        return {"status": "error", "error": "empty query"}
    limit = max(1, min(int(limit or 8), 25))
    try:
        import httpx

        resp = httpx.get(
            _EUROPEPMC,
            params={"query": query, "format": "json", "pageSize": limit, "resultType": "core"},
            timeout=timeout_s,
            headers={"User-Agent": "AiScientist/literature_search"},
        )
        resp.raise_for_status()
        raw = resp.json().get("resultList", {}).get("result", []) or []
    except Exception as exc:  # noqa: BLE001 - network/parse failure is reported, never fatal
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "query": query}

    raw_count = len(raw[:limit])
    results: list[dict[str, Any]] = []
    for r in raw[:limit]:
        doi = (r.get("doi") or "").strip()
        pmid = (r.get("pmid") or "").strip()
        if doi:
            url = f"https://doi.org/{doi}"
        elif pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        else:
            url = f"https://europepmc.org/article/{r.get('source', 'MED')}/{r.get('id', '')}"
        item = {
            "title": _clean(r.get("title") or ""),
            "authors": (r.get("authorString") or "").strip(),
            "year": (r.get("pubYear") or "").strip(),
            "journal": (r.get("journalTitle") or "").strip(),
            "doi": doi,
            "pmid": pmid,
            "url": url,
        }
        item["citation"] = _format_citation(item)
        results.append(item)
    results = _clean_results(results, query, limit)
    return {
        "status": "ok",
        "query": query,
        "count": len(results),
        "unfiltered_count": raw_count,
        "filtered_count": len(results),
        "results": results,
    }

#register as a tool
def make_literature_search_tool() -> Any:
    """The literature tool: search published biomedical literature for REAL citations.
    Imported lazily so ``tools.literature_search`` has no dependency on the agents pkg."""
    from ..agents.research_harness import HarnessContext, HarnessTool

    def _exec(args: dict[str, Any], _ctx: HarnessContext) -> dict[str, Any]:
        return search_europepmc(str(args.get("query", "")), int(args.get("limit", 8) or 8))

    return HarnessTool(
        "literature_search",
        "Search published biomedical literature (Europe PMC) for REAL citations to ground the "
        "report. Pass keyword queries built from your findings — genes, pathways, disease, cell "
        "type (e.g. 'DDX41 photoreceptor degeneration retina'). Returns real papers with title, "
        "authors, year, journal, DOI/PMID and a ready-to-use `citation` string. Cite ONLY papers "
        "this returns — never invent a reference. Use it before writing interpretation/discussion.",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "keyword query (genes/pathways/disease/cell type)"},
            "limit": {"type": "integer", "description": "max papers to return (1-25, default 8)"}},
         "required": ["query"]},
        _exec,
        reads_private_data=False, category="literature",
    )
