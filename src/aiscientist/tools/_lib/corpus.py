"""The literature corpus on disk, shared by the tools that read it.

The RetiGene corpus lives on dfs3b next to its PDFs: ``mechanism_cache/chunks.jsonl`` (clean text
chunks), ``mechanism_cache/chunk_vecs.npy`` (their L2-normalised PubMedBERT vectors), the manifest
CSV and the offline model cache. ``extract_haploinsufficient_genes`` and ``search_corpus`` both read
them; this module is the one place that knows the layout, and the one retrieval both use: exact
words (BM25 over the folded text, ``TextIndex``) next to the embedding, merged by reciprocal rank
(``hybrid_rank``) for a top-k search, or every passage holding a rare enough term (``TextIndex.matching``)
for a scan that must read all of them. Standard library at import; numpy and sentence-transformers are
imported when a function needs them (they exist inside ``paperqa.sif``).
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
import unicodedata
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

CACHE_DIRNAME = "mechanism_cache"
DEFAULT_EMBEDDING = os.environ.get("AISCIENTIST_PAPERQA_EMBEDDING", "st-NeuML/pubmedbert-base-embeddings")


@dataclass
class CorpusPaths:
    papers: Path
    manifest: Path | None
    cache: Path
    cache_writable: bool
    hgnc: Path | None
    hf_cache: Path | None
    notes: list[str] = field(default_factory=list)


def read_manifest(path: Path | None) -> list[dict[str, str]]:
    """The corpus manifest rows (``file_location``, ``title``, ``doc_id`` = PMID, ``citation``, ...)."""
    if not path or not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def manifest_citations(rows: list[dict[str, str]]) -> dict[str, str]:
    """PMID -> citation string, from manifest rows. A row without a ``citation`` column falls back to
    its title, so a passage is never returned without something to cite."""
    out: dict[str, str] = {}
    for row in rows:
        pmid = str(row.get("doc_id") or "").strip()
        if not pmid and row.get("docname"):
            pmid = str(row["docname"]).removeprefix("PMID").strip()
        if not pmid and row.get("file_location"):
            pmid = Path(str(row["file_location"])).stem
        if pmid:
            out[pmid] = (row.get("citation") or row.get("title") or "").strip()
    return out


def load_chunks(chunk_path: Path) -> list[dict[str, Any]]:
    with chunk_path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def l2(v: Any) -> Any:
    import numpy as np  # noqa: PLC0415
    v = np.asarray(v, dtype="float32")
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return v / n


def chunk_vectors(chunks: list[dict[str, Any]], cache_dir: Path, embed_fn: Callable[[list[str]], Any],
                  writable: bool, log: Callable[[str], None]) -> Any:
    """PubMedBERT vectors for every chunk (cached as ``chunk_vecs.npy`` beside the text cache)."""
    import numpy as np  # noqa: PLC0415
    path = cache_dir / "chunk_vecs.npy"
    if path.exists():
        vecs = np.load(path, mmap_mode="r")
        if vecs.shape[0] == len(chunks):
            return vecs
        log(f"chunk_vecs.npy has {vecs.shape[0]} rows for {len(chunks)} chunks; re-embedding")
    t0 = time.time()
    parts = []
    for i in range(0, len(chunks), 256):
        parts.append(l2(embed_fn([c["text"] for c in chunks[i:i + 256]])))
        if (i // 256) % 20 == 0:
            log(f"embedded {min(i + 256, len(chunks))}/{len(chunks)} chunks")
    vecs = np.concatenate(parts) if parts else np.zeros((0, 1), dtype="float32")
    log(f"embedding done in {time.time() - t0:.0f}s")
    if writable:
        try:
            np.save(path, vecs)
        except OSError as exc:
            log(f"could not cache vectors: {exc}")
    return vecs


def corpus_paths(ctx: Any, *, work_subdir: str = "") -> CorpusPaths | None:
    """Where the corpus is, from ``AISCIENTIST_PAPERQA_*``. ``work_subdir`` names the folder under the
    run's ``work/`` to build a text cache in when the shared one is missing (a tool that cannot build
    one leaves it empty and gets the shared path back, unreadable)."""
    papers = os.environ.get("AISCIENTIST_PAPERQA_PAPERS", "")
    if not papers or not Path(papers).is_dir():
        return None
    papers_p = Path(papers)
    root = papers_p.parent
    manifest = os.environ.get("AISCIENTIST_PAPERQA_MANIFEST", "")
    manifest_p = Path(manifest) if manifest and Path(manifest).is_file() else None
    notes: list[str] = []
    shared = Path(os.environ.get("AISCIENTIST_MECHANISM_CACHE", "") or root / CACHE_DIRNAME)
    ws = getattr(ctx, "workspace", None)
    if (shared / "chunks.jsonl").is_file():
        cache, writable = shared, os.access(shared, os.W_OK)
    elif ws and work_subdir:
        cache, writable = Path(ws) / "work" / work_subdir / CACHE_DIRNAME, True
        notes.append(f"No corpus cache at {shared}; extracting the PDFs into the run's work/ "
                     "(build it once with scripts/build_mechanism_cache.py to make this fast).")
    else:
        cache, writable = shared, os.access(root, os.W_OK)
    hgnc = os.environ.get("AISCIENTIST_HGNC_TABLE", "") or str(root / "hgnc_complete_set.txt")
    hgnc_p = Path(hgnc) if Path(hgnc).is_file() else None
    if hgnc_p is None:
        notes.append("No HGNC table (hgnc_complete_set.txt next to the papers, or AISCIENTIST_HGNC_TABLE): "
                     "gene symbols are reported as written and gene_status is 'unchecked'.")
    hf = root / "hf_cache"
    return CorpusPaths(papers_p, manifest_p, cache, writable, hgnc_p, hf if hf.is_dir() else None, notes)


def sentence_transformer_embed(hf_cache: Path | None) -> Callable[[list[str]], Any] | None:
    """The corpus embedding model (PubMedBERT, offline from the corpus' hf_cache), or None."""
    try:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    except ImportError:
        return None
    name = DEFAULT_EMBEDDING.split("st-", 1)[-1]
    if hf_cache:
        os.environ.setdefault("HF_HOME", str(hf_cache))
        os.environ.setdefault("HF_HUB_CACHE", str(hf_cache / "hub"))
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        model = SentenceTransformer(name)
    except Exception:  # noqa: BLE001 - offline cache missing: no semantic search
        return None
    return lambda texts: model.encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True)


# A bare gene symbol: 2-8 characters, capitals and digits, at least one capital, optionally a
# hyphen part (HLA-DRB1). Used to boost a chunk that contains the symbol as a word.
_GENE_RX = re.compile(r"\b[A-Z][A-Z0-9]{1,7}(?:-[A-Z0-9]{1,4})?\b")
_NOT_GENES = frozenset({"DNA", "RNA", "THE", "AND", "FOR", "WITH", "WHAT", "WHICH", "ARE", "WAS", "NOT",
                        "PCR", "MRNA", "SNP", "CNV", "OR", "IN", "OF", "TO", "IS", "AS", "AT", "BY"})


def gene_symbols(text: str) -> list[str]:
    """Tokens of ``text`` that look like gene symbols (RPGR, PRPF31), in order, without repeats."""
    seen: dict[str, None] = {}
    for tok in _GENE_RX.findall(text):
        if tok not in _NOT_GENES and any(c.isalpha() for c in tok) and tok not in seen:
            seen[tok] = None
    return list(seen)


# ---------------------------------------------------------------------------------------------
# Hybrid retrieval: exact words next to the embedding
#
# A passage's vector is its topic: one sentence that states the fact asked about barely moves it,
# so a top-k embedding search misses passages that say it in passing (2026-10-20: the scan's
# requests reached 50 of the 337 passages that say "haploinsufficiency"; "Is X haploinsufficient?"
# found evidence for 13 of 24 genes). The exact words find them. Every match runs on folded text:
# PDF ligatures ("haploinsufﬁciency", 62% of that word in the corpus) read as letters, soft hyphens
# dropped, Unicode dashes as "-".
# ---------------------------------------------------------------------------------------------

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")


def fold(text: str | None) -> str:
    """NFKC (ligatures as letters), soft hyphens out, Unicode dashes as ``-``."""
    return unicodedata.normalize("NFKC", text or "").replace("­", "").translate(_DASHES)


# Words a question is made of that say nothing about its topic.
STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each either few for from further had has
have having he her here hers how i if in into is it its itself just know known me might more most much
must my no nor not now of off on once only or other our out over own per please same she should so some
such tell than that the their them then there these they this those through to too under until up us
very was we were what when where whether which while who whom whose why will with within would you your
describe explain list give show find any role reported report reports paper papers study studies
use used using uses mean means meaning follow followed help typical typically usually common commonly main
""".split())

# A term is used to FILTER (a scan reads every passage holding it; a top-k search restricts to
# them) only when it is this rare: a word in 1 passage of 50 or more says nothing in particular.
FILTER_MAX_SHARE = 0.02
RRF_K = 60


@dataclass
class Term:
    """One exact word or phrase, as matched in the corpus. ``kind``: ``gene`` (a gene symbol, matched
    case-sensitively as a whole word), ``word`` (a word or phrase, any case, word endings and
    hyphen/space variants allowed) or ``pattern`` (a regular expression given by a tool, any case)."""
    text: str
    kind: str = "word"
    pattern: str = ""
    source: str = ""            # "query" (from the question), "model" (written for it) or the tool's own
    n_passages: int = 0
    use: str = ""               # "filter", "rank" or "absent", set by TextIndex.check


def _word_rx(w: str) -> str:
    """One word of a term: short words exactly (plus a plural), long ones by their stem, so
    "haploinsufficiency" also matches "haploinsufficient" and "dominant" "dominance"."""
    end = "(?![a-z0-9])"
    if w.isdigit() or len(w) <= 3:
        return re.escape(w) + ("" if w.isdigit() else "s?") + end
    if len(w) <= 5:
        return (re.escape(w[:-1]) + "(?:y|ies)" if w.endswith("y") else re.escape(w) + "(?:s|es)?") + end
    return re.escape(w[:max(5, len(w) - 3)]) + "[a-z0-9]*"


def term_regex(term: Term) -> tuple[re.Pattern[str], str, bool]:
    """The compiled pattern of ``term``, the text view it runs on (``case``, ``low`` or ``join``) and
    whether a match must start a word. The left word boundary is checked on each match rather than
    written into the pattern: a pattern that starts with its literal is about 20 times faster."""
    if term.kind == "pattern":
        return re.compile(term.pattern, re.I), "low", False
    t = fold(term.text).strip()
    if term.kind == "gene":
        return re.compile(re.escape(t) + r"(?![A-Za-z0-9])"), "case", True
    words = re.findall(r"[a-z0-9]+", t.lower())
    if not words:
        return re.compile(r"(?!x)x"), "low", False
    if len(words) == 1:
        # One word also matches its hyphenated spelling ("haplo-insufficiency"): the ``join`` view
        # has the hyphens between letters taken out.
        return re.compile(_word_rx(words[0])), "join", True
    return re.compile(r"[\s\-/]*".join(_word_rx(w) for w in words)), "low", True


# Upper-case abbreviations of this field that are not genes in its papers, though some are HGNC
# symbols (ERG: electroretinogram; RPE: retinal pigment epithelium; OCT: optical coherence tomography).
NOT_GENES_HERE = frozenset("""
ERG EOG RPE OCT FAF AMD LCA RP CSNB CRD ADRP ARRP XLRP BCVA VA IOP ONL OPL INL IPL GCL RNFL ELM EZ IZ
ISCEV ETDRS ROP FEVR CNV SNV WES WGS NGS PCR AAV CRISPR MRI CT ICG FA ERM CME MD BEST STGD PDE ISS
OMIM HGMD ACMG LOF GOF NMD DN AD AR XL WT KO
""".split())


def is_gene(token: str, known: frozenset[str] | None = None) -> bool:
    """A token is taken as a gene symbol when it is written like one, is not one of this field's
    abbreviations, and (with an HGNC table) is an approved, previous or alias symbol."""
    return (gene_symbols(token) == [token] and token not in NOT_GENES_HERE
            and (known is None or token in known))


_HGNC_SYMBOLS: dict[str, frozenset[str]] = {}


def known_gene_symbols(path: Path | None) -> frozenset[str] | None:
    """Every approved, previous and alias symbol in the HGNC table, as written; None without one."""
    if not path or not Path(path).is_file():
        return None
    key = str(path)
    if key not in _HGNC_SYMBOLS:
        out: set[str] = set()
        with Path(path).open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                for col in ("symbol", "alias_symbol", "prev_symbol"):
                    out.update(a.strip().strip('"') for a in str(r.get(col) or "").split("|") if a.strip())
        _HGNC_SYMBOLS[key] = frozenset(out)
    return _HGNC_SYMBOLS[key]


def query_terms(query: str, known: frozenset[str] | None = None) -> list[Term]:
    """The terms a question gives by itself: its gene symbols, and its other words of three letters
    or more that are not stop words (an abbreviation that is not a gene here counts as a word)."""
    genes = [g for g in gene_symbols(query) if is_gene(g, known)]
    out = [Term(g, "gene", source="query") for g in genes]
    gl = {g.lower() for g in genes}
    seen: set[str] = set()
    for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", fold(query).lower()):
        if len(w) >= 3 and w not in STOPWORDS and w not in gl and w not in seen and not w.isdigit():
            seen.add(w)
            out.append(Term(w, "word", source="query"))
    return out


def parse_terms(raw: Any, source: str = "model", known: frozenset[str] | None = None) -> list[Term]:
    """Terms given as a list of strings (or one comma-separated string): a gene symbol becomes a
    ``gene`` term, anything else a ``word`` term. Duplicates and empty items are dropped."""
    if isinstance(raw, str):
        raw = re.split(r"[;,\n]", raw)
    out: list[Term] = []
    seen: set[str] = set()
    for x in raw or []:
        t = fold(str(x)).strip().strip("\"'")
        if not t or len(t) > 80 or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(Term(t, "gene" if is_gene(t, known) else "word", source=source))
    return out


class TextIndex:
    """The corpus' chunk texts, folded, joined into three searchable views, with each chunk's
    length in words: enough for exact matching and BM25 without a stored index (building it takes
    a few seconds for the 39,563 RetiGene chunks)."""

    def __init__(self, texts: Iterable[str]):
        folded = [fold(t) for t in texts]
        low = [t.lower() for t in folded]
        joined = [re.sub(r"(?<=[a-z])-(?=[a-z])", "", t) for t in low]
        self.n = len(folded)
        self._views: dict[str, tuple[str, list[int]]] = {}
        for name, docs in (("case", folded), ("low", low), ("join", joined)):
            starts, pos = [], 0
            for d in docs:
                starts.append(pos)
                pos += len(d) + 1
            self._views[name] = ("\n".join(docs), starts)
        self.doc_len = [max(1, len(t.split())) for t in folded]
        self.avg_len = (sum(self.doc_len) / self.n) if self.n else 1.0
        self._counts: dict[tuple[str, str, str], dict[int, int]] = {}

    @classmethod
    def of_chunks(cls, chunks: list[dict[str, Any]]) -> "TextIndex":
        return cls(c.get("text", "") for c in chunks)

    def counts(self, term: Term) -> dict[int, int]:
        """Chunk index -> how often ``term`` occurs in it (chunks without it left out)."""
        key = (term.kind, term.text, term.pattern)
        if key not in self._counts:
            rx, view, word_start = term_regex(term)
            text, starts = self._views[view]
            out: Counter[int] = Counter()
            for m in rx.finditer(text):
                at = m.start()
                if word_start and at and text[at - 1].isalnum():
                    continue
                out[bisect_right(starts, at) - 1] += 1
            self._counts[key] = dict(out)
        return self._counts[key]

    def check(self, terms: list[Term], filter_max_share: float = FILTER_MAX_SHARE) -> list[Term]:
        """Each term's passage count and use, from the corpus itself: ``absent`` (no passage has it),
        ``filter`` (rare enough to select passages by) or ``rank`` (too common: it only adds to the
        BM25 score). Returns the same terms, filled in."""
        cap = max(1, int(filter_max_share * self.n))
        for t in terms:
            t.n_passages = len(self.counts(t))
            t.use = "absent" if not t.n_passages else ("filter" if t.n_passages <= cap else "rank")
        return terms

    def matching(self, terms: Iterable[Term]) -> set[int]:
        """Every chunk that holds at least one of ``terms``."""
        out: set[int] = set()
        for t in terms:
            out.update(self.counts(t))
        return out

    def bm25(self, terms: list[Term], k1: float = 1.2, b: float = 0.75,
             weights: dict[str, float] | None = None) -> dict[int, float]:
        """Okapi BM25 of each chunk for ``terms`` (chunks scoring 0 left out). A rare word counts far
        more than a common one, and a long chunk does not win by length alone. ``weights`` (by term
        text) scales a term's part; a term weighted 0 is left out."""
        import math  # noqa: PLC0415
        scores: dict[int, float] = {}
        for t in terms:
            w = 1.0 if weights is None else weights.get(t.text, 1.0)
            tf = self.counts(t) if w > 0 else {}
            if not tf:
                continue
            idf = w * math.log(1 + (self.n - len(tf) + 0.5) / (len(tf) + 0.5))
            for d, f in tf.items():
                norm = f + k1 * (1 - b + b * self.doc_len[d] / self.avg_len)
                scores[d] = scores.get(d, 0.0) + idf * f * (k1 + 1) / norm
        return scores


def nearest(q_vec: Any, vecs: Any, chunks: list[dict[str, Any]], k: int,
            candidates: Iterable[int] | None = None, max_per_paper: int = 3) -> list[int]:
    """The ``k`` chunks nearest one request (cosine: both sides are L2-normalised), at most
    ``max_per_paper`` from one paper. ``candidates`` limits the search to those chunk indices."""
    import numpy as np  # noqa: PLC0415
    if candidates is None:
        idx = None
        scores = vecs @ q_vec
    else:
        idx = np.asarray(sorted(candidates), dtype=int)
        if not len(idx):
            return []
        scores = vecs[idx] @ q_vec
    out: list[int] = []
    per_paper: dict[str, int] = {}
    for o in np.argsort(-scores, kind="stable"):        # ties keep the corpus order
        i = int(o) if idx is None else int(idx[o])
        pmid = str(chunks[i]["pmid"])
        if per_paper.get(pmid, 0) >= max_per_paper:
            continue
        per_paper[pmid] = per_paper.get(pmid, 0) + 1
        out.append(i)
        if len(out) >= k:
            break
    return out


def _fuse(cos: Any, index: TextIndex, term_groups: list[list[Term]], pool: set[int] | None,
          depth: int) -> dict[int, float]:
    """Reciprocal-rank fusion of the cosine ranking and one BM25 ranking per term group, all taken
    within ``pool`` (all chunks when None), each cut at ``depth``."""
    import numpy as np  # noqa: PLC0415
    if pool is not None:
        cand = np.asarray(sorted(pool), dtype=int)
        if not len(cand):
            return {}
        dense_order = cand[np.argsort(-cos[cand], kind="stable")][:depth]
    else:
        dense_order = np.argsort(-cos, kind="stable")[:depth]
    fused: dict[int, float] = {}
    for rank, d in enumerate(dense_order):
        fused[int(d)] = fused.get(int(d), 0.0) + 1.0 / (RRF_K + rank + 1)
    for group in term_groups:
        lex = index.bm25(group)
        if pool is not None:
            lex = {d: v for d, v in lex.items() if d in pool}
        for rank, d in enumerate(sorted(lex, key=lambda d: (-lex[d], d))[:depth]):
            fused[d] = fused.get(d, 0.0) + 1.0 / (RRF_K + rank + 1)
    return fused


# The merged ranking gives this many chunks for each one the embedding's own ranking gives.
DENSE_EVERY = 3


def interleave(first: list[int], second: list[int], ratio: int = 1) -> list[int]:
    """``ratio`` chunks of ``first``, then one of ``second``, in turn, each chunk once: the best of
    ``second`` stays near the top whatever ``first`` thinks of it."""
    out: list[int] = []
    seen: set[int] = set()
    a, b = iter(first), iter(second)
    live = True
    while live:
        live = False
        for src, n in ((a, ratio), (b, 1)):
            for _ in range(n):
                for d in src:
                    if d not in seen:
                        seen.add(d)
                        out.append(d)
                        live = True
                        break
    return out


def hybrid_rank(q_vec: Any, vecs: Any, index: TextIndex, terms: list[Term], *, depth: int = 200,
                allowed: set[int] | None = None) -> list[tuple[int, float, float]]:
    """One query's chunks, best first, as ``(chunk index, score, cosine)``.

    Rankings, each cut at ``depth``: the embedding (cosine), BM25 over the question's own words, and
    BM25 over the terms from elsewhere (the model's), each kept apart so the model's topic words
    cannot drown the question's own (terms checked first with ``TextIndex.check``; ``absent`` ones
    are ignored). They are merged by reciprocal rank (sum of 1 / (60 + rank)), so a chunk near the
    top of any list comes up, and one high on several comes first. The merged list then gives three
    chunks for each one the embedding's own ranking gives (``DENSE_EVERY``), so the embedding's best
    three always stay in a top 12: the words rescue a passage the embedding ranks low, and a passage
    it ranks first is not pushed out by words that merely occur in other passages (2026-10-21, 32
    graded questions: plain fusion did worse than the embedding alone on 3, losing up to 4 of 6
    answering passages; this on 2, losing at most 2).

    When the query itself names genes that the corpus has, the chunks that name one of them are ranked
    among themselves and come first (a question about RPGR is answered from passages about RPGR);
    the other chunks follow, so a gene few passages name still gets a full answer list. ``allowed``
    limits everything to those chunk indices. The score is 1 / (60 + position)."""
    import numpy as np  # noqa: PLC0415
    live = [t for t in terms if t.use != "absent"]
    # Only a gene the question itself names decides which chunks come first: a gene the model adds
    # to its terms (ZNHIT3 for a question about PEHO syndrome) would put its passages ahead of the answer.
    genes = [t for t in live if t.kind == "gene" and t.source == "query"]
    own = [t for t in live if t.source == "query"]
    other = [t for t in live if t.source != "query"]
    groups = [g for g in (own, other) if g]
    cos = np.asarray(vecs) @ np.asarray(q_vec, dtype="float32")
    scope = set(allowed) if allowed is not None else None
    named = index.matching(genes) if genes else set()
    if scope is not None:
        named &= scope

    def tier(pool: set[int] | None) -> list[int]:
        fused = _fuse(cos, index, groups, pool, depth)
        merged = sorted(fused, key=lambda d: (-fused[d], d))
        if pool is not None:
            cand = np.asarray(sorted(pool), dtype=int)
            dense = cand[np.argsort(-cos[cand], kind="stable")][:depth].tolist() if len(cand) else []
        else:
            dense = np.argsort(-cos, kind="stable")[:depth].tolist()
        return interleave(merged, [int(d) for d in dense], DENSE_EVERY)

    order: list[int] = []
    if named:
        order += tier(named)
        order += tier((scope if scope is not None else set(range(index.n))) - named)
    else:
        order += tier(scope)
    return [(d, 1.0 / (RRF_K + r + 1), float(cos[d])) for r, d in enumerate(order)]


def term_report(terms: list[Term]) -> list[dict[str, Any]]:
    """The terms as the tool's result shows them, so the model can see what each one did."""
    return [{"term": t.text, "kind": t.kind, "source": t.source, "n_passages": t.n_passages, "use": t.use}
            for t in terms]
