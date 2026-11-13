"""Platform-owned literature planning contract, independent of scientific tool runtimes.

These are the existing UI query and curation-spec rules. Search/ranking, PDF
processing, embeddings and extraction remain in the literature service. The
platform validates a draft before scheduling it; the service validates it again.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any

_HTML_TAG = re.compile(r"<[^>]+>")


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


_QUERY_MAX_WORDS = 18


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


_TOKEN_NORMALIZATIONS = {
    "ko": "knockout",
    "wt": "wild type",
    "scrna": "single-cell RNA-seq",
    "scrnaseq": "single-cell RNA-seq",
    "rnaseq": "RNA-seq",
}


def _clean(text: str) -> str:
    """Europe PMC embeds HTML (e.g. <i>GENE</i>) in titles and abstracts, sometimes escaped
    (``&lt;i&gt;``) — unescape it, then strip the tags, for clean output."""
    return _HTML_TAG.sub("", html.unescape(html.unescape(text or ""))).strip()


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


SPEC_SYSTEM = """You are planning a whole-corpus curation over the lab's RetiGene corpus: about 1,700 full-text research papers on inherited retinal disease, split into about 40,000 passages.

How the curation runs once you have written the spec:
1. Retrieval. Each of your search requests is embedded (PubMedBERT) and takes its 50 nearest passages. Every passage that contains one of your exact terms is added too, but only for a term that occurs in at most 1% of the passages (a common term would add thousands).
2. Reading. A language model reads each retrieved passage once, with your definition and rules, and for every gene a sentence talks about it assigns one of your classes, says whether the statement is explicit or hedged, negated or not, and within your scope or not, and copies the sentence verbatim.
3. Second round. For each gene read as your target class, one more request is written and searched among the passages that name that gene, and those passages are read as well.
4. The table. A gene is listed when at least one sentence states your target class for it explicitly, not negated, within scope, with the quote found in the paper.

Write the spec as ONE JSON object with exactly these fields:
- "target": a short name for what is collected (a few words).
- "definition": 2-4 sentences that say exactly what a sentence must state for a gene to count, precise enough that two curators reading the same sentence would agree.
- "counts_when": 2-6 rules: what a sentence must contain to count.
- "does_not_count": 3-8 rules: near misses that look similar but must not count.
- "classes": 4-7 snake_case labels the reader assigns to each gene and sentence: the target class first, then the near-miss classes, then "other" and "not_relevant".
- "scope": one sentence: which diseases or phenotypes, and which kinds of evidence, count.
- "requests": exactly 16 search requests. Each is one full sentence written the way a paper would state a qualifying finding, each from a different angle (the wording authors use for it, the kinds of variants or alleles involved, the kinds of evidence, the phenotypes). Name no specific gene: the genes must come from the papers.
- "terms": at most 12 exact words or short phrases (1-4 words) that a qualifying sentence would contain, with their usual variants (noun and adjective forms, hyphenated and spaced spellings). Leave out words that also fill thousands of passages that do not qualify, such as gene and disease names or variant types on their own.

Return ONLY the JSON object."""


SPEC_FIELDS = ("target", "definition", "counts_when", "does_not_count", "classes", "scope", "requests", "terms")


_SPEC_LIST_FIELDS = ("counts_when", "does_not_count", "classes", "requests", "terms")


def parse_spec(spec: Any) -> dict[str, Any]:
    """A drafted spec (a dict, or a model reply holding one) checked and normalised. Raises
    ValueError naming what is wrong, so a draft that cannot drive a scan is never run."""
    data = spec if isinstance(spec, dict) else _json_object(str(spec or ""))
    if not isinstance(data, dict):
        raise ValueError("the spec is not a JSON object")
    missing = [f for f in SPEC_FIELDS if f not in data]
    if missing:
        raise ValueError(f"the spec has no {', '.join(missing)}")
    out: dict[str, Any] = {f: data[f] for f in SPEC_FIELDS}
    for f in ("target", "definition", "scope"):
        if not isinstance(out[f], str) or not out[f].strip():
            raise ValueError(f"the spec's {f} is empty")
        out[f] = " ".join(out[f].split())
    for f in _SPEC_LIST_FIELDS:
        if not isinstance(out[f], list) or not all(isinstance(x, str) for x in out[f]):
            raise ValueError(f"the spec's {f} is not a list of strings")
        out[f] = [" ".join(x.split()) for x in out[f] if x.strip()]
    classes: list[str] = []
    for c in out["classes"]:
        c = re.sub(r"[^a-z0-9_]+", "_", c.lower()).strip("_")
        if c and c not in classes:
            classes.append(c)
    for c in ("other", "not_relevant"):
        if c not in classes:
            classes.append(c)
    if len(classes) < 3:
        raise ValueError("the spec names no target class")
    out["classes"] = classes
    out["requests"] = [r for r in out["requests"] if 4 <= len(r.split()) and len(r) <= 400][:16]
    out["terms"] = [t for t in out["terms"] if 0 < len(t.split()) <= 4][:12]
    if len(out["requests"]) < 4:
        raise ValueError(f"the spec has {len(out['requests'])} usable search requests (at least 4)")
    if not out["counts_when"] or not out["does_not_count"]:
        raise ValueError("the spec needs rules for what counts and what does not")
    return out


def spec_folder(spec: dict[str, Any]) -> str:
    """Where a spec scan writes under ``work/`` and ``artifacts/``: one folder per spec."""
    return "curation_" + spec_digest(spec)[:10]


def spec_digest(spec: dict[str, Any]) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(json.dumps({f: spec[f] for f in SPEC_FIELDS}, sort_keys=True).encode("utf-8")).hexdigest()


def _json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in a model reply: the reply itself, else the outermost ``{...}`` in it (a
    model that writes a line before its JSON is still read)."""
    text = re.sub(r"^```(json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    for cand in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None

QUERY_STOPWORDS = _QUERY_STOPWORDS
