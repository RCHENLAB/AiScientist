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

import html
import math
import re
from collections import Counter
from typing import Any

_EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_HTML_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_QUERY_MAX_WORDS = 18
# All-caps tokens that are methods/formats, not biology (kept in sync with research_lab's list).
_BRIEF_NOT_ENTITIES = {
    "WT", "KO", "DE", "QC", "PCA", "UMAP", "GO", "RNA", "DNA", "PDF", "CSV", "OK", "AND", "OR",
    "THE", "VCF", "VEP", "PASS", "GRCH37", "GRCH38", "HG19", "HG38", "BAM", "FASTQ", "H5AD", "TSV",
    "JSON", "API", "REST", "HPC", "GPU", "CPU", "LLM", "AI", "ORA", "GSEA", "FDR", "BH", "HVG",
    "MT", "SNP", "SNV", "INDEL", "CNV", "SV", "CADD", "REVEL", "SPLICEAI", "CLINVAR", "GNOMAD",
    "OMIM", "HPO", "ACMG", "DOI", "PMID", "PUBMED", "SCVI", "SCGPT", "SEURAT", "SCANPY", "DESEQ2",
    "EDGER", "LIMMA", "IRD", "WGS", "WES", "NES", "LOG2FC", "LFC",
    # all-caps INSTRUCTION words the harness or a planner writes into a brief ("Do ONLY this
    # task"); a real one of these reached Europe PMC as "DDX41 ONLY splicing retinal" -> 0 hits
    "ONLY", "NOT", "DO", "MUST", "NEVER", "ALWAYS", "ALL", "ANY", "EACH", "EVERY", "TRUE",
    "FALSE", "NONE", "NULL", "NOTE", "TBD", "IMPORTANT", "REQUIRED", "STRICTLY", "EXACT",
    "EXACTLY", "FIRST", "LAST", "BEFORE", "AFTER", "WITH", "WITHOUT", "USE", "USING", "RUN",
    "SEE", "ABOVE", "BELOW", "THIS", "THAT", "THESE", "THOSE", "FROM", "INTO", "ONTO", "PER",
}
_BRIEF_FUNCTION_WORDS = {"against", "known", "using", "within", "between", "across", "toward",
                         "through", "under", "above", "below", "while", "where", "which",
                         "their", "these", "those", "there", "other", "every", "about"}
_QUERY_MAX_CHARS = 180
_ABSTRACT_MAX_CHARS = 2000
SEARCH_DEPTH = 100
MAX_RESULTS = 25    # papers returned: what the `limit` schema promises (1-25)
_RANK_WORD = re.compile(r"[a-z][a-z0-9]*")
_RANK_STOPWORDS = set("a an and are as at be by for from in is it not of on or that the this to was were with".split())
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
    """Europe PMC embeds HTML (e.g. <i>GENE</i>) in titles and abstracts, sometimes escaped
    (``&lt;i&gt;``) — unescape it, then strip the tags, for clean output."""
    return _HTML_TAG.sub("", html.unescape(html.unescape(text or ""))).strip()


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
    # A pasted STEP BRIEF ("**Reconciliation …** — Cross-reference the bootstrap-stabilized … using
    # `literature_search` …") is not a query. Europe PMC ANDs every term, so the 10-word residue of a
    # brief returns nothing (measured: 0 hits, the Critic then scored the step 0.2). When the text
    # is brief-shaped — a bold title, backticked tool names, or very long — search on the ENTITIES
    # it names (gene/protein symbols) plus at most two adjacent content words instead.
    brief_shaped = text.startswith("**") or "`run_" in text or "`literature" in text
    if brief_shaped:
        stripped = re.sub(r"`[^`]*`|\*\*[^*]*\*\*", " ", text)
        ents: list[str] = []
        for m in re.finditer(r"\b(?:[A-Z][A-Za-z0-9-]*\d[A-Za-z0-9-]*|[A-Z]{2,}[A-Za-z0-9-]*"
                             r"|[A-Z][a-z]{2,}\d+[a-z0-9]*)\b", stripped):
            t = m.group(0).strip("-")
            if len(t) >= 3 and t.upper() not in _BRIEF_NOT_ENTITIES and t not in ents:
                ents.append(t)
        if ents:
            near: list[str] = []
            toks = _WORD.findall(stripped)
            pos = {i for i, w in enumerate(toks) if w.strip("-") in ents}
            for i, w in enumerate(toks):
                lw = w.lower().strip("-")
                if (w.islower() and len(lw) >= 5 and lw not in _QUERY_STOPWORDS
                        and lw not in _BRIEF_FUNCTION_WORDS
                        and any(abs(i - e) <= 2 for e in pos) and lw not in near):
                    near.append(lw)
            return " ".join([*ents[:3], *near[:2]])
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


def _rank_terms(text: str) -> set[str]:
    return set(_RANK_WORD.findall(_clean(text).lower())) - _RANK_STOPWORDS


def _clean_results(citations: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    """IDF-weighted query overlap in title + abstract; title matches count three times.

    Distinct terms prevent repeated words in long abstracts dominating. Exact tokens preserve
    short gene symbols (RHO, RP1) without matching incidental substrings in authors or journals.
    Ties retain the service's order; publication year and particular genes get no extra weight.
    """
    terms = _rank_terms(query)
    docs = [(c, _rank_terms(str(c.get("title") or "")),
             _rank_terms(str(c.get("abstract") or "")))
            for c in citations if not _is_generic_citation(c)]
    frequencies = Counter(term for _, title, abstract in docs for term in title | abstract)
    weights = {t: math.log(1 + (len(docs) + 1) / (frequencies[t] + 1)) for t in terms}
    scored = [(sum(weights[t] * (3 * (t in title) + (t in abstract)) for t in terms), c)
              for c, title, abstract in docs]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for score, c in scored if score > 0][:limit]


#clear query-> request Europe PMC -> parse->filter->return
def search_europepmc(query: str, limit: int = 8, timeout_s: float = 20.0, *,
                     abstracts: bool = False, pubmed_only: bool = False) -> dict[str, Any]:
    """Search Europe PMC and return real, structured citations. Never raises — returns
    ``{status: "error", error}`` on any failure so the agent loop can adapt or skip.

    ``abstracts`` adds each paper's abstract (``abstract``, plain text, at most
    ``_ABSTRACT_MAX_CHARS``), for a caller that answers from the papers rather than only citing
    them. ``pubmed_only`` keeps PubMed records (Europe PMC source ``MED``): no preprints, patents
    or theses. One request reads up to 100 core records; title + full abstract determine local
    relevance before the output limit and abstract truncation are applied."""
    query = focus_literature_query(query)
    if not query:
        return {"status": "error", "error": "empty query"}
    limit = max(1, min(int(limit or 8), MAX_RESULTS))
    sent = f"({query}) AND SRC:MED" if pubmed_only else query
    try:
        import httpx

        resp = httpx.get(
            _EUROPEPMC,
            params={"query": sent, "format": "json", "pageSize": SEARCH_DEPTH, "resultType": "core"},
            timeout=timeout_s,
            headers={"User-Agent": "AiScientist/literature_search"},
        )
        resp.raise_for_status()
        raw = resp.json().get("resultList", {}).get("result", []) or []
    except Exception as exc:  # noqa: BLE001 - network/parse failure is reported, never fatal
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "query": query}

    raw_count = len(raw[:SEARCH_DEPTH])
    results: list[dict[str, Any]] = []
    for r in raw[:SEARCH_DEPTH]:
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
        item["abstract"] = " ".join(_clean(r.get("abstractText") or "").split())
        results.append(item)
    results = _clean_results(results, query, limit)
    for item in results:
        if abstracts:
            item["abstract"] = item["abstract"][:_ABSTRACT_MAX_CHARS]
        else:
            item.pop("abstract")
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
    from ..sdk import HarnessTool, ToolContext

    def _exec(args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
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
