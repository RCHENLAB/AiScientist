"""The ``extract_haploinsufficient_genes`` tool. Documentation: ``TOOL.md`` in this folder.

A corpus-wide extraction over the PaperQA corpus (the RetiGene PDFs on dfs3b): which genes do the
papers *explicitly* report as haploinsufficient in retinal disease, and where do they say so.

Why this is not ``deep_literature``: PaperQA answers one question from the top-k retrieved
passages and synthesises prose, so an exhaustive "every gene with this mechanism" request misses
genes and loses the verbatim sentences a curator needs. This tool instead walks every paper:

1. **text** — re-extract each PDF page with PyMuPDF text blocks (narrow blocks left column then
   right, full-width blocks after, running headers and footers dropped, hyphenation joined, the
   paper cut at its References heading), ~1500-character chunks, sentences within each chunk.
   Cached once in ``mechanism_cache/`` next to the papers (``scripts/build_mechanism_cache.py``).
2. **candidates** — keep a sentence when it carries mechanism wording (keyword patterns), or when
   its chunk is among the 1500 nearest, in PubMedBERT space, to one of six mechanism queries and
   the sentence has at least a soft marker (heterozygous, dominant, allele, ...). ~3% of the corpus.
3. **judge** — the session's served model reads each candidate with its neighbours and returns
   JSON: gene, mechanism class, explicit/negated/human-retinal, the phenotype as written, and ONE
   verbatim sentence. Thinking is off, output is forced to JSON, temperature 0.
4. **verify + aggregate** — a quote is kept only if it is really in the paper; the gene symbol is
   normalised through the HGNC table (approved symbols first, aliases flagged); reference-list
   look-alikes and garbled text are flagged, not silently dropped; one row per gene with up to
   three PMIDs and quotes, one row per judged sentence.

This is the pipeline that produced the first delivery (Oct 2026: 1,739 papers, 426,706 sentences,
14,535 candidates, 45 genes), moved into the platform unchanged in its rules. The model and the
embedder are injectable (``judge_fn`` / ``embed_fn``) so the whole pipeline runs in the tests
without HPC3, a GPU or network.
"""

from __future__ import annotations

import collections
import csv
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .._lib.corpus import (  # noqa: F401 - re-exported: scripts/build_mechanism_cache.py imports them from here
    CorpusPaths, chunk_vectors, l2 as _l2, load_chunks, read_manifest, sentence_transformer_embed)
from .._lib.corpus import corpus_paths as _corpus_paths
from ..sdk import HarnessTool

_TOOL = "extract_haploinsufficient_genes"
_OUT_SUBDIR = "haploinsufficiency"

# ---------------------------------------------------------------------------------------------
# The definition (Jin Li, 2026-10-05) and the model's instructions — verbatim from the delivery run
# ---------------------------------------------------------------------------------------------

JUDGE_SYSTEM = """You are a careful curator of inherited retinal disease (IRD) genetics. You judge ONLY from the text given. Never use prior knowledge about any gene.

DEFINITION. "Haploinsufficiency" means: a heterozygous loss-of-function allele (nonsense, frameshift, splice, whole-gene deletion, or a transcript degraded by nonsense-mediated decay) causes the disease because one functional copy is insufficient, i.e. a dominant loss-of-function / reduced-dosage mechanism. It is NOT dominant-negative, NOT gain-of-function, and NOT recessive (biallelic) loss of function.

STRICT RULES.
(1) Use "haploinsufficiency" ONLY when the text states BOTH: (a) the disease occurs with a heterozygous / monoallelic / dominant loss-of-function or null allele, AND (b) a dosage mechanism (haploinsufficiency, gene dosage, one functional copy insufficient, reduced protein level from the single remaining allele, NMD of the mutant allele in a dominant context).
(2) NMD, truncation or loss of function described in a recessive / biallelic / compound-heterozygous / homozygous context is "recessive_lof", never haploinsufficiency.
(3) "Dominant mutation causes X", "dominant-acting", "autosomal dominant inheritance" with no stated mechanism is "not_mechanism".
(4) If the sentence says the mechanism is unknown, debated, or contrasts haploinsufficiency with dominant-negative without deciding, use "other" with explicit=false.
(5) The gene symbol must appear in TARGET or CONTEXT; otherwise gene=null.

TASK. Read TARGET (with CONTEXT before/after). For each gene whose disease MECHANISM is stated in the text, output one item. A sentence that only says a gene is mutated in / associated with / causes a disease, without saying HOW (dosage, one copy, dominant-negative...), is NOT a mechanism statement: use "not_mechanism".

Return ONLY JSON: {"items":[{"gene": HGNC symbol as written in the text (null if the text says only 'the gene' and no symbol appears in TARGET or CONTEXT),
 "mechanism_class": one of "haploinsufficiency" | "dominant_negative" | "gain_of_function" | "recessive_lof" | "other" | "not_mechanism",
 "explicit": true if the text asserts the mechanism for this gene; false if hedged (may, might, suggests, possibly, could, consistent with, likely),
 "negated": true if the text says this mechanism is NOT / unlikely the cause for this gene,
 "human_retinal": true ONLY if the phenotype named for this gene is a human retinal or ocular disease (retinitis pigmentosa, cone-rod dystrophy, LCA, macular dystrophy, Stargardt, achromatopsia, CSNB, optic atrophy, aniridia, etc.); false if the phenotype is non-ocular (cardiomyopathy, epilepsy, cancer, syndromic non-eye features...) or the evidence is only from animal models or cell lines,
 "phenotype": disease name as written in the text or null,
 "quote": ONE complete sentence copied VERBATIM, character for character, from TARGET (or from CONTEXT only if that is where the mechanism is stated). Never paraphrase, never shorten, never merge sentences.}]}
If the text is a reference-list entry or a cited paper title rather than a statement by the authors, set mechanism_class to "not_mechanism".
If no gene mechanism is stated, return {"items":[]}."""

DEFINITION = ("A heterozygous loss-of-function allele (nonsense, frameshift, splice, whole-gene deletion, or a "
              "transcript degraded by nonsense-mediated decay) causes the disease because one functional copy is "
              "insufficient: a dominant loss-of-function / reduced-dosage mechanism. Not dominant-negative, not "
              "gain-of-function, not recessive (biallelic) loss of function.")

MECHANISM_CLASSES = ("haploinsufficiency", "dominant_negative", "gain_of_function",
                     "recessive_lof", "other", "not_mechanism")

# ---------------------------------------------------------------------------------------------
# Candidate selection (filter_v2.py)
# ---------------------------------------------------------------------------------------------

KW: dict[str, re.Pattern[str]] = {k: re.compile(v, re.I) for k, v in {
    "haploinsuff": r"haplo-?\s?insuffici\w*",
    "dosage": r"\b(gene |allele |protein )?dosage\b|dosage[- ]sensitiv\w*",
    "nmd": r"nonsense[- ]mediated|\bNMD\b",
    "het_lof": r"heterozyg\w*[^.]{0,80}\b(null|loss[- ]of[- ]function|LoF|truncat\w*|nonsense|frameshift|"
               r"premature (stop|termination)|stop[- ]gain|whole[- ]gene deletion|deletion of the (entire|whole) gene)\b|"
               r"\b(null|truncat\w*|nonsense|frameshift|loss[- ]of[- ]function|LoF)\b[^.]{0,80}heterozyg\w*",
    "one_copy": r"\b(one|single|a single|only one) (functional|normal|wild[- ]type|intact|remaining) (copy|allele)\b|"
                r"\bloss of (one|a single) (allele|copy)\b|"
                r"\b(half|50 ?%) (of )?(the )?(normal|wild[- ]type) (level|amount|dose|expression|protein)",
    "dom_lof": r"dominant[^.]{0,60}loss[- ]of[- ]function|loss[- ]of[- ]function[^.]{0,60}dominant",
    "dn_gof": r"dominant[- ]negative|gain[- ]of[- ]function|\bGoF\b|\bDN\b (effect|mechanism|allele)",
}.items()}
SOFT = re.compile(r"heterozyg|dominant|allele|mutant|transcript|haplo|dosage|\bcopy\b|copies|LoF|loss of|null|"
                  r"truncat|nonsense|frameshift|deletion|insufficien", re.I)
SEMANTIC_QUERIES = (
    "heterozygous loss-of-function variants cause the retinal disease through haploinsufficiency",
    "a single functional copy of the gene is insufficient, so heterozygous null alleles cause dominant disease",
    "the mutant transcript is degraded by nonsense-mediated decay, leaving reduced gene dosage",
    "dominant retinal dystrophy caused by reduced gene dosage rather than a dominant-negative effect",
    "whole-gene deletion in heterozygous state causes retinitis pigmentosa",
    "the disease mechanism is haploinsufficiency as opposed to gain of function",
)
_SEMANTIC_TOPN = 1500
# Per-candidate flags (pipeline_v2.sbatch): a journal citation in the sentence; extraction damage.
CIT = re.compile(r"(\b(19|20)\d{2}\s*[;:]\s*\d+\s*[:(]\d|\bdoi\s*:|\b(19|20)\d{2}\)\s*\d+[:(])")
GARBLED_MARK = re.compile(r"How to cite|Downloaded from|All rights reserved")

# ---------------------------------------------------------------------------------------------
# Aggregation (aggregate_final.py)
# ---------------------------------------------------------------------------------------------

AUTH = re.compile(r"^[A-Z][A-Za-z'-]+ [A-Z]{1,3}(,| and| &)|et al\.?,? \(?\d{4}\)?|et al\.?,? [A-Z][^.]{20,}, [A-Z][a-z]*\.?$")
CITN = re.compile(r"\b(19|20)\d{2}\s*[;:]\s*\d+|\bdoi\s*:|\(\d{4}\)\s*[A-Z]|^[A-Z][A-Za-z'-]+ [A-Z]{1,3}[,.]")
MECH = re.compile(r"haplo-?\s?insuffici\w*|gene dosage|dosage[- ]sensitiv\w*|dosage|nonsense[- ]mediated (mRNA )?decay|"
                  r"\bNMD\b|heterozygous (null|loss[- ]of[- ]function|truncating|nonsense|frameshift)[^,.;]{0,40}|"
                  r"loss of (one|a single) (allele|copy)|(one|single) (functional|normal|wild[- ]type) (copy|allele)[^,.;]{0,40}|"
                  r"reduced (level|amount|dosage) of [^,.;]{0,30}", re.I)
DOM = re.compile(r"heterozyg|monoallelic|dominant|haplo-?\s?insuffici|dosage|"
                 r"(one|single|a single) (functional|normal|wild[- ]type|intact|remaining) (copy|allele)|\bcarriers?\b", re.I)


@dataclass
class Sentence:
    sid: str          # "<pmid>:<chunk>:<sent>"
    pmid: str
    chunk: int
    sent: int
    page: int
    text: str
    prev: str = ""    # neighbours within the same chunk
    next: str = ""


@dataclass
class Judgment:
    sid: str
    items: list[dict[str, Any]] | None   # None = the model call failed (retried on the next run)
    error: str = ""


# ---------------------------------------------------------------------------------------------
# 1. Text: PDF -> blocks -> chunks -> sentences (reparse_pdfs.py)
# ---------------------------------------------------------------------------------------------

REFHEAD = re.compile(r"^\s*(references|reference list|bibliography|literature cited)\s*$", re.I)
ABBR = re.compile(r"\b(et al|Fig|Figs|Dr|vs|e\.g|i\.e|cf|approx|No|Ref|Refs|Suppl)\.$", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\(\[])")
_CHUNK_CHARS = 1500


def dehyph(t: str) -> str:
    t = t.replace("­", "")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)            # line-end hyphenation
    t = re.sub(r"[ \t]*\n[ \t]*", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def split_sents(t: str) -> list[str]:
    parts: list[str] = []
    buf = ""
    for piece in _SENT_SPLIT.split(t):
        buf = (buf + " " + piece).strip() if buf else piece
        if ABBR.search(buf):
            continue
        parts.append(buf)
        buf = ""
    if buf:
        parts.append(buf)
    return [p for p in parts if len(p) > 25]


def page_blocks(page: Any) -> list[str]:
    """A page's text blocks in reading order: image blocks, the header/footer strips (7% of the
    height) and fragments under 15 characters dropped; narrow blocks left column then right, each
    top to bottom; full-width blocks after them, top to bottom."""
    w, h = page.rect.width, page.rect.height
    out = []
    for b in page.get_text("blocks"):
        x0, y0, x1, y1, txt = b[0], b[1], b[2], b[3], b[4]
        if len(b) > 6 and b[6] != 0:
            continue
        if y1 < 0.07 * h or y0 > 0.93 * h:
            continue
        if len(txt.strip()) < 15:
            continue
        col = 0 if (x0 + x1) / 2 < w * 0.5 or (x1 - x0) > 0.6 * w else 1
        wide = (x1 - x0) > 0.6 * w
        out.append((y0 if wide else 0, col if not wide else -1, y0, txt))
    out.sort(key=lambda t: (t[0] if t[1] == -1 else 0, t[1], t[2]))
    return [t[3] for t in out]


def paper_chunks(pdf_path: Path) -> list[tuple[int, str]]:
    """``(page, text)`` chunks of about 1500 characters, built block by block; the paper is cut at
    a References heading found after 40% of its pages."""
    try:
        import pymupdf as fitz  # noqa: PLC0415
    except ImportError:  # pragma: no cover - the old module name
        import fitz  # type: ignore  # noqa: PLC0415
    chunks: list[tuple[int, str]] = []
    with fitz.open(str(pdf_path)) as doc:
        npg = len(doc)
        for pno, page in enumerate(doc):
            buf = ""
            stop = False
            for txt in page_blocks(page):
                if pno >= npg * 0.4 and REFHEAD.match(txt.strip().split("\n")[0]):
                    stop = True
                    break
                t = dehyph(txt)
                if not t:
                    continue
                buf = (buf + " " + t) if buf else t
                if len(buf) >= _CHUNK_CHARS:
                    chunks.append((pno + 1, buf))
                    buf = ""
            if buf:
                chunks.append((pno + 1, buf))
            if stop:
                break
    return chunks


def chunk_sentences(pmid: str, ci: int, page: int, text: str) -> list[Sentence]:
    sents = split_sents(text)
    return [Sentence(sid=f"{pmid}:{ci}:{si}", pmid=pmid, chunk=ci, sent=si, page=page, text=s,
                     prev=sents[si - 1] if si else "", next=sents[si + 1] if si + 1 < len(sents) else "")
            for si, s in enumerate(sents)]


def corpus_files(papers: Path, manifest: list[dict[str, str]]) -> list[tuple[str, Path]]:
    """``(pmid, pdf)`` for every paper: the manifest's order and PMIDs when there is one, else
    every PDF under ``papers`` filed under the digits in its name."""
    if manifest:
        out = []
        for r in manifest:
            loc = str(r.get("file_location") or "")
            if not loc:
                continue
            pdf = Path(loc) if Path(loc).is_absolute() else papers / loc
            pmid = str(r.get("doc_id") or "").strip() or pdf.stem
            out.append((pmid, pdf))
        return out
    out = []
    for pdf in sorted(papers.rglob("*.pdf")):
        m = re.search(r"(\d{6,9})", pdf.stem)
        out.append((m.group(1) if m else pdf.stem, pdf))
    return out


def build_text_cache(papers: Path, manifest_path: Path | None, out_dir: Path,
                     pmids: Iterable[str] | None = None, log: Callable[[str], None] = lambda s: None,
                     ) -> tuple[Path, Path]:
    """Extract the corpus into ``out_dir/chunks.jsonl`` + ``out_dir/sentences.jsonl``. Per paper
    and idempotent: a paper already in the files is skipped (resume after a timeout)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sent_path, chunk_path = out_dir / "sentences.jsonl", out_dir / "chunks.jsonl"
    done: set[str] = set()
    if chunk_path.exists():
        with chunk_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["pmid"])
                except (ValueError, KeyError):
                    continue
    want = {str(p) for p in pmids} if pmids else None
    n_new = n_fail = 0
    with sent_path.open("a", encoding="utf-8") as sf, chunk_path.open("a", encoding="utf-8") as cf:
        for pmid, pdf in corpus_files(papers, read_manifest(manifest_path)):
            if pmid in done or (want is not None and pmid not in want):
                continue
            try:
                chunks = paper_chunks(pdf)
            except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the corpus
                log(f"{pdf.name}: {type(exc).__name__}: {str(exc)[:80]}")
                n_fail += 1
                continue
            for ci, (page, text) in enumerate(chunks):
                cf.write(json.dumps({"pmid": pmid, "chunk": ci, "page": page, "text": text}) + "\n")
                for s in chunk_sentences(pmid, ci, page, text):
                    sf.write(json.dumps(s.__dict__) + "\n")
            done.add(pmid)
            n_new += 1
            if n_new % 200 == 0:
                log(f"extracted {n_new} papers")
    log(f"text cache: {len(done)} papers ({n_new} new, {n_fail} failed)")
    return sent_path, chunk_path


def load_sentences(sent_path: Path, pmids: set[str] | None = None) -> list[Sentence]:
    out: list[Sentence] = []
    with sent_path.open(encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            if pmids is None or d["pmid"] in pmids:
                out.append(Sentence(**d))
    return out


# ---------------------------------------------------------------------------------------------
# 2. Candidates
# ---------------------------------------------------------------------------------------------

def keyword_hits(text: str) -> list[str]:
    return [k for k, rx in KW.items() if rx.search(text)]


def is_garbled(text: str) -> bool:
    return bool(GARBLED_MARK.search(text)) or len(re.findall(r"[a-z]- [a-z]", text)) >= 2


def select_candidates(sentences: list[Sentence], chunks: list[dict[str, Any]], vecs: Any | None,
                      embed_fn: Callable[[list[str]], Any] | None, topn: int = _SEMANTIC_TOPN,
                      ) -> tuple[list[Sentence], dict[str, dict[str, Any]]]:
    """The sentences the model will read, with their flags: keyword tags, semantic hit, cite_like,
    garbled. Keyword hits, plus every sentence with a soft marker inside a chunk among the
    ``topn`` nearest to any mechanism query."""
    import numpy as np  # noqa: PLC0415
    flags: dict[str, dict[str, Any]] = {}
    for s in sentences:
        tags = keyword_hits(s.text)
        if tags:
            flags[s.sid] = {"kw": tags, "sem": False}
    if vecs is not None and embed_fn is not None and len(chunks):
        q = _l2(embed_fn(list(SEMANTIC_QUERIES)))
        sims = np.asarray(vecs) @ q.T                      # chunks x queries
        hot: set[tuple[str, int]] = set()
        for j in range(sims.shape[1]):
            for i in np.argsort(-sims[:, j])[:topn]:
                hot.add((chunks[int(i)]["pmid"], int(chunks[int(i)]["chunk"])))
        for s in sentences:
            if (s.pmid, s.chunk) in hot and SOFT.search(s.text):
                flags.setdefault(s.sid, {"kw": [], "sem": False})["sem"] = True
    by_sid = {s.sid: s for s in sentences}
    cands = [by_sid[sid] for sid in flags]
    for s in cands:
        flags[s.sid]["cite_like"] = bool(CIT.search(s.text))
        flags[s.sid]["garbled"] = is_garbled(s.text)
    return cands, flags


# ---------------------------------------------------------------------------------------------
# 3. Judge: one bounded JSON completion per candidate (judge.py)
# ---------------------------------------------------------------------------------------------

def judge_prompt(s: Sentence) -> list[dict[str, str]]:
    user = (f"CONTEXT BEFORE: {s.prev or '(none)'}\nTARGET: {s.text}\nCONTEXT AFTER: {s.next or '(none)'}")
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def parse_items(text: str) -> list[dict[str, Any]] | None:
    """The model's ``items``, or None when the reply is not the JSON asked for."""
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("items", [])
    if not isinstance(items, list):
        return None
    return [it for it in items if isinstance(it, dict)]


def openai_judge_fn(base_url: str, model: str, timeout: float = 180.0, max_tokens: int = 700,
                    ) -> Callable[[list[dict[str, str]]], str]:
    """``messages -> text`` against an OpenAI-compatible ``/v1`` (the session's vLLM Qwen):
    thinking OFF, JSON object output, temperature 0."""
    url = base_url.rstrip("/") + "/chat/completions"
    key = os.environ.get("AISCIENTIST_LLM_API_KEY", "sk-no-key-required")

    def call(messages: list[dict[str, str]]) -> str:
        body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data["choices"][0]["message"].get("content") or "")

    return call


# After this many candidates IN A ROW fail to reach the model, the endpoint is taken as gone (the
# serve job was preempted or hit its time limit) and the pass stops at once, instead of retrying
# every remaining sentence against a dead port — 14,535 refused connections cost three hours once.
_DEAD_AFTER = 20


def _connection_error(exc: BaseException) -> bool:
    """A transport failure (nothing listening, host down, reset), as opposed to an HTTP error the
    server answered with or a reply that was not the JSON asked for."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, OSError))


def judge_candidates(cands: list[Sentence], judge_fn: Callable[[list[dict[str, str]]], str],
                     out_path: Path, concurrency: int = 16, log: Callable[[str], None] = lambda s: None,
                     should_stop: Callable[[], bool] | None = None, attempts: int = 3,
                     dead_after: int | None = None) -> tuple[dict[str, Judgment], str]:
    """Judge every candidate not already judged in ``out_path`` (a line whose items are null is a
    failed call and is retried on the next run). Returns ``(judgments, unreachable)``: the second
    is empty normally, else the error that made the pass give the endpoint up for dead."""
    dead_after = dead_after or _DEAD_AFTER
    done: dict[str, Judgment] = {}
    if out_path.exists():
        with out_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("items") is not None:
                    done[d["sid"]] = Judgment(d["sid"], d["items"])
    todo = [s for s in cands if s.sid not in done]
    log(f"judging {len(todo)} candidates ({len(done)} already done)")
    lock = threading.Lock()
    t0 = time.time()
    n = 0
    dead = threading.Event()
    dead_reason = ""
    streak = 0                      # consecutive transport failures, reset by any answer

    def one(s: Sentence) -> Judgment:
        nonlocal streak, dead_reason
        err = ""
        for attempt in range(attempts):
            if dead.is_set():
                return Judgment(s.sid, None, "model unreachable")
            if should_stop and should_stop():
                return Judgment(s.sid, None, "stopped")
            try:
                items = parse_items(judge_fn(judge_prompt(s)))
                with lock:
                    streak = 0
                if items is not None:
                    return Judgment(s.sid, items)
                err = "not the JSON asked for"
            # IndexError / TypeError / AttributeError: a 200 whose body is not a completion (no
            # choices, a null message). One odd reply fails its sentence, not the whole pass.
            except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError,
                    AttributeError) as exc:
                err = f"{type(exc).__name__}: {str(exc)[:200]}"
                if _connection_error(exc):
                    with lock:
                        streak += 1
                        if streak >= dead_after and not dead.is_set():
                            dead_reason = err
                            dead.set()
                    continue        # a refused connection is not a transient the next second cures
            time.sleep(2 * (attempt + 1))
        return Judgment(s.sid, None, err)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(one, s) for s in todo]
        for fut in as_completed(futures):
            j = fut.result()
            with lock:
                # A sentence that was never really judged (the endpoint had died) is not written:
                # a null-items line would be retried anyway, and the file stays a record of answers.
                if not (j.items is None and j.error in ("model unreachable", "stopped")):
                    fh.write(json.dumps({"sid": j.sid, "items": j.items, "error": j.error}) + "\n")
                    fh.flush()
                done[j.sid] = j
                n += 1
                if n % 500 == 0:
                    log(f"judged {n}/{len(todo)} in {time.time() - t0:.0f}s")
    if dead.is_set():
        log(f"the model stopped answering ({dead_reason}); {sum(1 for j in done.values() if j.items is not None)} "
            f"of {len(cands)} judged so far")
    return done, dead_reason if dead.is_set() else ""


# ---------------------------------------------------------------------------------------------
# 4. Verify, normalise, aggregate (aggregate_final.py)
# ---------------------------------------------------------------------------------------------

def nz(s: str | None) -> str:
    return re.sub(r"[\s­-]+", "", (s or "").lower())


class Hgnc:
    """Symbol normalisation only, never evidence: approved symbols first; previous symbols and
    aliases second; an alias that maps to several approved symbols is reported as ambiguous."""

    def __init__(self, path: Path | None):
        self.hg: dict[str, str] = {}
        self.alias: dict[str, set[str]] = {}
        self.loaded = False
        if not path or not path.is_file():
            return
        with path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh, delimiter="\t"):
                sym = str(r.get("symbol") or "").strip()
                if not sym:
                    continue
                self.hg[sym.upper()] = sym
                for col in ("alias_symbol", "prev_symbol"):
                    for a in str(r.get(col) or "").split("|"):
                        a = a.strip().strip('"')
                        if a:
                            self.alias.setdefault(a.upper(), set()).add(sym)
        self.loaded = True

    def norm(self, g: Any) -> tuple[str | None, str]:
        """``(symbol, status)``; status ok | alias | ambiguous:A|B | unknown | missing | unchecked."""
        if not g:
            return None, "missing"
        g = re.sub(r"[^A-Za-z0-9-]", "", str(g)).upper()
        if not g:
            return None, "missing"
        if not self.loaded:
            return g, "unchecked"
        if g in self.hg:
            return self.hg[g], "ok"
        if g in self.alias and len(self.alias[g]) == 1:
            return next(iter(self.alias[g])), "alias"
        if g in self.alias:
            return g, "ambiguous:" + "|".join(sorted(self.alias[g]))
        return g, "unknown"


def make_is_cite(titles: Iterable[str]) -> Callable[[str, str, str], bool]:
    """A sentence is a reference-list entry when it contains (or is) a corpus paper's title, reads
    like an author list, or sits between journal citations without first-person wording."""
    tl = [t for t in (nz(t) for t in titles) if len(t) > 30]

    def is_cite(q: str, prev: str = "", nxt: str = "") -> bool:
        n = nz(q)
        return ((len(n) > 30 and any(n in t or t in n for t in tl)) or bool(AUTH.search(q))
                or bool(CITN.search(prev) and CITN.search(nxt))
                or (bool(CITN.search(prev) or CITN.search(nxt))
                    and not re.search(r"\b(we|our|these|this|here)\b", q, re.I)))

    return is_cite


HIT_COLUMNS = ("pmid", "page", "gene", "gene_as_written", "gene_status", "gene_in_quote", "gene_in_context",
               "mechanism_class", "explicit", "negated", "human_retinal", "phenotype", "mechanism_statement",
               "quote", "quote_verified", "cite_like", "dominance_marker", "garbled", "sentence_id", "eligible")
GENE_COLUMNS = ("gene", "phenotype", "mechanism_statement", "n_papers", "n_hits", "hedged_hits",
                "conflicting_mechanism", "flag_only_cited_titles", "flag_no_dominance_marker",
                "flag_gene_not_in_quote", "flag_gene_alias_or_unknown", "gene_status", "gene_as_written",
                "flag_garbled", "pmid_1", "quote_1", "pmid_2", "quote_2", "pmid_3", "quote_3")


def eligible(h: dict[str, Any]) -> bool:
    """genes.csv's entry condition = the hard requirements of the brief; everything else is a flag."""
    return bool(h["mechanism_class"] == "haploinsufficiency" and h["explicit"] and not h["negated"]
                and h["human_retinal"] and h["quote_verified"] and h["gene"] and h["gene_status"] != "missing")


def _word(gw: str, text: str) -> bool:
    return bool(gw) and re.search(r"(?<![A-Za-z0-9])" + re.escape(gw) + r"(?![A-Za-z0-9])", text, re.I) is not None


def aggregate(judgments: dict[str, Judgment], by_sid: dict[str, Sentence], flags: dict[str, dict[str, Any]],
              hgnc: Hgnc, titles: Iterable[str], max_quotes: int = 3,
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(gene rows, hit rows)`` from the raw judgments."""
    is_cite = make_is_cite(titles)
    hits: list[dict[str, Any]] = []
    for sid, j in judgments.items():
        s = by_sid.get(sid)
        if s is None or not j.items:
            continue
        fl = flags.get(sid, {})
        ctx = s.text + " " + s.prev + " " + s.next
        for it in j.items:
            q = str(it.get("quote") or "")
            gene, gstat = hgnc.norm(it.get("gene"))
            gw = str(it.get("gene") or "").strip()
            m = MECH.search(q) or MECH.search(s.text)
            h = {"pmid": s.pmid, "page": s.page, "gene": gene, "gene_as_written": gw, "gene_status": gstat,
                 "gene_in_quote": _word(gw, q), "gene_in_context": _word(gw, ctx),
                 "mechanism_class": it.get("mechanism_class"), "explicit": bool(it.get("explicit")),
                 "negated": bool(it.get("negated")), "human_retinal": bool(it.get("human_retinal")),
                 "phenotype": it.get("phenotype"), "mechanism_statement": m.group(0).strip() if m else "",
                 "quote": q, "quote_verified": bool(q) and nz(q) in nz(ctx),
                 "cite_like": bool(fl.get("cite_like")) or is_cite(q, s.prev, s.next) or is_cite(s.text, s.prev, s.next),
                 "dominance_marker": bool(DOM.search(q + " " + ctx)), "garbled": bool(fl.get("garbled")),
                 "sentence_id": sid}
            h["eligible"] = eligible(h)
            hits.append(h)

    by_gene: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for h in hits:
        if h["eligible"]:
            by_gene[h["gene"]].append(h)
    other: dict[str, set[str]] = collections.defaultdict(set)
    hedged: collections.Counter[str] = collections.Counter()
    for h in hits:
        if h["gene"] and h["mechanism_class"] in ("dominant_negative", "gain_of_function") and h["explicit"] \
                and not h["negated"]:
            other[h["gene"]].add(h["mechanism_class"])
        if h["gene"] and h["mechanism_class"] == "haploinsufficiency" and not h["explicit"] and h["human_retinal"]:
            hedged[h["gene"]] += 1
    out: list[dict[str, Any]] = []
    for g, hs in sorted(by_gene.items()):
        # Quotes: body sentences before reference entries, clean before garbled, the gene in the
        # quote, the word "haploinsufficiency" itself, then the longer sentence.
        hs = sorted(hs, key=lambda h: (h["cite_like"], h["garbled"], not h["gene_in_quote"],
                                       "haploinsuffic" not in h["quote"].lower(), -len(h["quote"])))
        picked: list[dict[str, Any]] = []
        seen: set[str] = set()
        for h in hs:
            if h["pmid"] in seen:
                continue
            picked.append(h)
            seen.add(h["pmid"])
            if len(picked) == max_quotes:
                break
        phen = collections.Counter(h["phenotype"] for h in hs if h["phenotype"]).most_common(1)
        row: dict[str, Any] = {
            "gene": g, "phenotype": phen[0][0] if phen else "",
            "mechanism_statement": picked[0]["mechanism_statement"] or "haploinsufficiency",
            "n_papers": len({h["pmid"] for h in hs}), "n_hits": len(hs), "hedged_hits": hedged.get(g, 0),
            "conflicting_mechanism": "|".join(sorted(other.get(g, []))),
            "flag_only_cited_titles": all(h["cite_like"] for h in hs),
            "flag_no_dominance_marker": not any(h["dominance_marker"] for h in picked),
            "flag_gene_not_in_quote": not any(h["gene_in_quote"] for h in picked),
            "flag_gene_alias_or_unknown": hs[0]["gene_status"] not in ("ok", "unchecked"),
            "gene_status": hs[0]["gene_status"], "gene_as_written": hs[0]["gene_as_written"],
            "flag_garbled": any(h["garbled"] for h in picked),
        }
        for i in range(max_quotes):
            row[f"pmid_{i + 1}"] = picked[i]["pmid"] if i < len(picked) else ""
            row[f"quote_{i + 1}"] = picked[i]["quote"] if i < len(picked) else ""
        out.append(row)
    return out, hits


def write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


# ---------------------------------------------------------------------------------------------
# Deployment: where the corpus, cache, HGNC table and model are
# ---------------------------------------------------------------------------------------------

def local_endpoint(ctx: Any) -> tuple[str, str] | None:
    """``(model, api_base)`` for the session's served model, the way deep_literature finds it."""
    model = getattr(ctx, "model", None)
    if not model:
        return None
    base = getattr(ctx, "llm_base_url", None) or os.environ.get("AISCIENTIST_PAPERQA_LLM_BASE_URL")
    if base:
        return str(model), str(base).rstrip("/")
    port = getattr(ctx, "tunnel_port", None)
    if not port:
        return None
    return str(model), f"http://127.0.0.1:{port}/v1"


# ---------------------------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------------------------

PARAMS: dict[str, tuple[Any, str]] = {
    "max_quotes_per_gene": (3, "how many papers (PMID + verbatim quote) genes.csv lists per gene"),
    "concurrency": (16, "parallel requests to the served model while judging sentences"),
    "max_candidates": (0, "judge only the first N candidate sentences (0 = all); for a smoke test"),
}


def _schema() -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name, (default, meaning) in PARAMS.items():
        props[name] = {"type": "integer", "default": default, "description": meaning}
    props["pmids"] = {"type": "array", "items": {"type": "string"},
                      "description": "restrict the scan to these PMIDs (empty = the whole corpus)"}
    return {"type": "object", "properties": props}


def _p(name: str, args: dict[str, Any]) -> Any:
    v = args.get(name)
    return PARAMS[name][0] if v is None else v


def run_extract(args: dict[str, Any], ctx: Any, *, judge_fn: Callable[[list[dict[str, str]]], str] | None = None,
                embed_fn: Callable[[list[str]], Any] | None = None, log: Callable[[str], None] | None = None,
                ) -> dict[str, Any]:
    """The whole pipeline for one run. ``judge_fn`` / ``embed_fn`` default to the session's served
    model and the corpus' PubMedBERT; the tests pass fakes."""
    log = log or (lambda s: print(f"[{_TOOL}] {s}", flush=True))
    ws = getattr(ctx, "workspace", None)
    if not ws:
        return {"status": "error", "step": "literature", "error": f"{_TOOL} needs a run workspace"}
    ws = Path(ws)
    paths = _corpus_paths(ctx, work_subdir=_OUT_SUBDIR)
    if paths is None:
        return {"status": "not_enabled", "step": "literature",
                "note": (f"{_TOOL} reads the PaperQA corpus on HPC3 (AISCIENTIST_PAPERQA_PAPERS), which is "
                         "not available where this step ran. It runs as an HPC3 literature job.")}
    warnings = list(paths.notes)
    if judge_fn is None:
        ep = local_endpoint(ctx)
        if ep is None:
            return {"status": "not_enabled", "step": "literature",
                    "note": "no served model on this session (model / llm_base_url unset); the sentences "
                            "cannot be judged. Start the session's model and rerun."}
        model, base = ep
        judge_fn = openai_judge_fn(base, model)
        log(f"model {model} at {base}")

    work = ws / "work" / _OUT_SUBDIR
    art = ws / "artifacts" / _OUT_SUBDIR
    work.mkdir(parents=True, exist_ok=True)
    genes_csv, hits_csv = art / "genes.csv", art / "hits.csv"
    pmids = {str(p) for p in (args.get("pmids") or []) if str(p).strip()} or None

    # 1. text
    t0 = time.time()
    sent_path, chunk_path = paths.cache / "sentences.jsonl", paths.cache / "chunks.jsonl"
    if not chunk_path.is_file():
        if not paths.cache_writable:
            return {"status": "error", "step": "literature",
                    "error": f"no corpus cache at {paths.cache} and it is not writable"}
        build_text_cache(paths.papers, paths.manifest, paths.cache, pmids, log)
    sentences = load_sentences(sent_path, pmids)
    chunks = load_chunks(chunk_path)
    if pmids:
        chunks = [c for c in chunks if c["pmid"] in pmids]
    n_papers = len({s.pmid for s in sentences})
    log(f"{n_papers} papers, {len(sentences)} sentences ({time.time() - t0:.0f}s)")
    if not sentences:
        return {"status": "failed", "step": "literature", "error": "no text extracted from the corpus",
                "warnings": warnings}

    # 2. candidates
    manifest = read_manifest(paths.manifest)
    titles = [str(r.get("title") or "") for r in manifest]
    embed = embed_fn if embed_fn is not None else sentence_transformer_embed(paths.hf_cache)
    vecs = None
    if embed is not None and not pmids:
        vecs = chunk_vectors(chunks, paths.cache, embed, paths.cache_writable, log)
    elif embed is not None:
        vecs = _l2(embed([c["text"] for c in chunks])) if chunks else None
    else:
        warnings.append("sentence-transformers / the PubMedBERT model are not available here: candidates "
                        "were chosen by keyword only (the semantic pass was skipped).")
    cands, flags = select_candidates(sentences, chunks, vecs, embed)
    max_c = int(_p("max_candidates", args) or 0)
    if max_c > 0:
        cands = cands[:max_c]
    log(f"{len(cands)} candidate sentences in {len({s.pmid for s in cands})} papers")
    with (work / "candidates.jsonl").open("w", encoding="utf-8") as fh:
        for s in cands:
            fh.write(json.dumps({**s.__dict__, **flags.get(s.sid, {})}) + "\n")

    # 3. judge
    stop = getattr(ctx, "should_cancel", None)
    judgments, unreachable = judge_candidates(cands, judge_fn, work / "judgments.jsonl",
                                              int(_p("concurrency", args)), log,
                                              should_stop=stop if callable(stop) else None)
    n_ok = sum(1 for sid, j in judgments.items() if j.items is not None and sid in flags)
    if unreachable:
        # The served model went away (its Slurm job was preempted or expired). Nothing to aggregate
        # that a rerun would not improve: say so, keep the judgments made, and let the caller bring
        # the model back and call again — the next run picks up where this one stopped.
        return {"status": "failed", "step": "literature", "model_unreachable": True,
                "error": (f"The served model stopped answering ({unreachable}); {n_ok} of {len(cands)} "
                          "candidate sentences are judged. Rerun once the model is back — finished "
                          "judgments are kept and only the rest are judged."),
                "n_papers": n_papers, "n_sentences": len(sentences), "n_candidates": len(cands),
                "n_judged": n_ok, "warnings": warnings}
    failed = len(cands) - n_ok
    if failed:
        warnings.append(f"{failed} of {len(cands)} sentences could not be judged (model errors); rerun the "
                        "tool to retry them — finished judgments are kept.")
    if cands and n_ok == 0:
        return {"status": "failed", "step": "literature", "model_unreachable": True,
                "error": "None of the candidate sentences could be judged (every model call failed). "
                         "Check the served model and rerun.", "n_candidates": len(cands), "n_judged": 0,
                "warnings": warnings}

    # 4. aggregate
    by_sid = {s.sid: s for s in cands}
    genes, hits = aggregate({k: v for k, v in judgments.items() if k in by_sid}, by_sid, flags,
                            Hgnc(paths.hgnc), titles, int(_p("max_quotes_per_gene", args)))
    write_csv(genes_csv, genes, GENE_COLUMNS)
    write_csv(hits_csv, hits, HIT_COLUMNS)
    n_elig = sum(1 for h in hits if h["eligible"])
    log(f"{n_elig} eligible hits -> {len(genes)} genes ({time.time() - t0:.0f}s total)")
    flag_keys = [k for k in GENE_COLUMNS if k.startswith("flag_")]
    return {
        "status": "ok", "step": "literature",
        "n_papers": n_papers, "n_sentences": len(sentences), "n_candidates": len(cands),
        "n_judged": len(cands) - failed, "n_hits_eligible": n_elig, "n_genes": len(genes),
        "genes": [{"gene": g["gene"], "n_papers": g["n_papers"], "phenotype": (g["phenotype"] or "")[:80],
                   "flags": [k[5:] for k in flag_keys if g[k]]} for g in genes],
        "genes_flagged_for_review": [g["gene"] for g in genes if any(g[k] for k in flag_keys)],
        "files": {"genes_csv": str(genes_csv.relative_to(ws)), "hits_csv": str(hits_csv.relative_to(ws))},
        "definition": DEFINITION,
        "warnings": warnings,
        "raw_data_to_llm": False,
    }


def make_tool() -> HarnessTool:
    """The ``extract_haploinsufficient_genes`` record for the catalog (see ``TOOL.md``)."""
    return HarnessTool(
        _TOOL,
        "Scan EVERY paper of the indexed literature corpus for genes the papers explicitly report as "
        "HAPLOINSUFFICIENT in retinal disease (a heterozygous loss-of-function allele causes disease "
        "because one copy is not enough; not dominant-negative, not gain-of-function, not recessive). "
        "Writes genes.csv (one row per gene: HGNC symbol, phenotype and mechanism in the paper's words, "
        "up to 3 PMIDs with verbatim quotes, review flags) and hits.csv (every judged sentence). Use it "
        "when the user wants the complete list with evidence as a table; for one focused question use "
        "deep_literature. Slow (about an hour): call it once per run.",
        _schema(),
        run_extract,
        reads_private_data=False, category="literature", requires=("paper-qa",),
    )
