"""The ``extract_haploinsufficient_genes`` tool. Documentation: ``TOOL.md`` in this folder.

A corpus-wide extraction over the PaperQA corpus (the RetiGene PDFs on dfs3b): which genes do the
papers *explicitly* report as haploinsufficient in retinal disease, and where do they say so.

It is retrieval-augmented, in the same steps as ``search_corpus`` answering a question:

1. **requests** — the model writes search requests from the definition: full sentences, each from
   another angle (the mechanism's wording, the allele types, the evidence, the phenotypes), with no
   gene named, so the genes come from the papers and not from the model.
2. **retrieve** — PubMedBERT embeds each request and takes its nearest chunks of the corpus text
   cache (``mechanism_cache/``, built once by ``scripts/build_mechanism_cache.py``), a few per paper
   at most.
3. **locate** — each retrieved chunk is read back as its paper, page and numbered sentences (the
   text is re-extracted with PyMuPDF text blocks, column-aware, cut at the References heading).
4. **read** — the model reads each chunk once and returns JSON: per gene whose disease mechanism a
   sentence states, the mechanism class, explicit/negated/human-retinal, the phenotype as written,
   and ONE verbatim sentence with its number. Thinking off, JSON output, temperature 0.
5. **verify + aggregate** — a quote is kept only if it is a sentence of the paper; the gene symbol
   is normalised through the HGNC table (approved symbols first, aliases flagged); reference-list
   look-alikes and garbled text are flagged, not silently dropped; one row per gene with up to
   three PMIDs and quotes, one row per judged sentence.

Steps 1-4 run twice. Round one searches the whole corpus with the general requests, and also
reads every chunk whose text states a dosage mechanism in words (``DOSAGE_WORDING``: the word
haploinsufficiency, gene dosage, one functional copy, ...): the embedding of a 1,500-character
chunk is dominated by its topic, so the requests alone found 50 of the corpus' 337 chunks that say
"haploinsufficiency". Round two takes every gene round one read as haploinsufficient, has the model
write one request about that gene, and searches only the chunks that name the gene, so the other
papers stating that gene's mechanism are read as well.

The model and the embedder are injectable (``judge_fn`` / ``embed_fn``) so the whole pipeline runs
in the tests without HPC3, a GPU or network.
"""

from __future__ import annotations

import collections
import csv
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .._lib.corpus import (  # noqa: F401 - re-exported: scripts/build_mechanism_cache.py imports them from here
    CorpusPaths, chunk_vectors, l2 as _l2, load_chunks, read_manifest, sentence_transformer_embed)
from .._lib.corpus import FILTER_MAX_SHARE, Term, TextIndex, fold, nearest, parse_terms, term_report
from .._lib.corpus import corpus_paths as _corpus_paths
from ..sdk import HarnessTool

_TOOL = "extract_haploinsufficient_genes"
_OUT_SUBDIR = "haploinsufficiency"

# ---------------------------------------------------------------------------------------------
# The definition (Jin Li, 2026-10-05) and the model's instructions
# ---------------------------------------------------------------------------------------------

DEFINITION = ("A heterozygous loss-of-function allele (nonsense, frameshift, splice, whole-gene deletion, or a "
              "transcript degraded by nonsense-mediated decay) causes the disease because one functional copy is "
              "insufficient: a dominant loss-of-function / reduced-dosage mechanism. Not dominant-negative, not "
              "gain-of-function, not recessive (biallelic) loss of function.")

MECHANISM_CLASSES = ("haploinsufficiency", "dominant_negative", "gain_of_function",
                     "recessive_lof", "other", "not_mechanism")

# Step 1, round one. "{N}" is the number of requests asked for.
REQUESTS_SYSTEM = """You are writing search requests for an embedding index (PubMedBERT) of full-text papers on inherited retinal disease. The goal is to find EVERY passage in which the authors state that a gene causes a retinal or ocular disease through haploinsufficiency.

DEFINITION. "Haploinsufficiency" means: a heterozygous loss-of-function allele (nonsense, frameshift, splice, whole-gene deletion, or a transcript degraded by nonsense-mediated decay) causes the disease because one functional copy is insufficient, i.e. a dominant loss-of-function / reduced-dosage mechanism. It is NOT dominant-negative, NOT gain-of-function, and NOT recessive (biallelic) loss of function.

Write {N} DISTINCT requests. Each is ONE full sentence of the kind a passage of such a paper could contain (a statement, not a list of keywords), and together they cover different angles:
- the words authors use for the mechanism: haploinsufficiency, reduced gene dosage, one functional copy is not enough, half the normal amount of protein;
- the allele types: heterozygous nonsense, frameshift, splice-site or null alleles; heterozygous whole-gene or exon deletions and copy-number losses; a mutant transcript removed by nonsense-mediated decay in a dominant disease;
- the evidence: patient cells or tissue with about half the normal transcript or protein; heterozygous knockout animals that reproduce the dominant human disease; incomplete penetrance or variable expressivity explained by dosage;
- the contrast authors draw with a dominant-negative or gain-of-function effect;
- the phenotypes: retinitis pigmentosa, cone and cone-rod dystrophy, macular dystrophy, optic atrophy, aniridia and other eye-development disorders, vitreoretinopathy, syndromes with eye involvement.
Do NOT name any specific gene: the genes must come from the papers, not from you.

Return ONLY JSON: {"requests": ["...", "..."]}"""

# Step 1, round two: one request per gene round one read as haploinsufficient.
GENE_REQUESTS_SYSTEM = """You are writing search requests for an embedding index of full-text papers on inherited retinal disease. A first search found passages that state or suggest haploinsufficiency for the genes listed below, each with the phenotype the passages gave. For EACH gene write ONE full sentence that would find the other passages about that gene's disease mechanism: name the gene, its phenotype, and heterozygous loss of function / haploinsufficiency. Use only the gene symbols and phenotypes given; add no other gene.

Return ONLY JSON: {"requests": {"GENE": "sentence", "...": "..."}}"""

# Step 1, round one, exact words: the model writes the words a qualifying passage would contain;
# the corpus counts each, and every passage holding a rare enough one is read too.
TERMS_SYSTEM = """You are choosing exact search words for a full-text index of papers on inherited retinal disease. The goal is to find EVERY passage in which the authors state that a gene causes a retinal or ocular disease through haploinsufficiency.

DEFINITION. "Haploinsufficiency" means: a heterozygous loss-of-function allele causes the disease because one functional copy is insufficient, i.e. a dominant loss-of-function / reduced-dosage mechanism. It is NOT dominant-negative, NOT gain-of-function, and NOT recessive loss of function.

Write the words and short phrases (1-4 words) that such a passage would use to STATE THIS MECHANISM, with their usual variants (noun and adjective forms, hyphenated spellings, synonyms). Leave out words that also fill thousands of passages that do not state it: gene and disease names, variant types on their own (nonsense, frameshift, deletion, heterozygous), and the names of other mechanisms. Each word is counted in the corpus; one found in more than 1 passage of 100 is not used.

Return ONLY JSON: {"terms": ["...", "..."]} with at most {N} terms."""
_MAX_TERMS = 12


def terms_messages() -> list[dict[str, str]]:
    return [{"role": "system", "content": TERMS_SYSTEM.replace("{N}", str(_MAX_TERMS))},
            {"role": "user", "content": "Write the search words."}]


def parse_model_terms(text: str) -> list[str] | None:
    """The model's search words: strings of at most 4 words, at most ``_MAX_TERMS``. None when the
    reply is not the JSON asked for."""
    data = _json_object(text)
    raw = data.get("terms") if data else None
    if not isinstance(raw, list):
        return None
    out = [" ".join(str(r).split()) for r in raw if isinstance(r, str) and 0 < len(str(r).split()) <= 4]
    return out[:_MAX_TERMS]


# Used for a gene the model's reply left out (counted in the result's warnings).
GENE_REQUEST_TEMPLATE = ("Heterozygous loss of function of {gene} causes {phenotype} through haploinsufficiency, "
                         "because one functional copy of {gene} is not enough.")

# Step 4: the model reads one located chunk.
JUDGE_SYSTEM = """You are a careful curator of inherited retinal disease (IRD) genetics. You judge ONLY from the text given. Never use prior knowledge about any gene.

DEFINITION. "Haploinsufficiency" means: a heterozygous loss-of-function allele (nonsense, frameshift, splice, whole-gene deletion, or a transcript degraded by nonsense-mediated decay) causes the disease because one functional copy is insufficient, i.e. a dominant loss-of-function / reduced-dosage mechanism. It is NOT dominant-negative, NOT gain-of-function, and NOT recessive (biallelic) loss of function.

STRICT RULES.
(1) Use "haploinsufficiency" ONLY when the text states BOTH: (a) the disease occurs with a heterozygous / monoallelic / dominant loss-of-function or null allele, AND (b) a dosage mechanism (haploinsufficiency, gene dosage, one functional copy insufficient, reduced protein level from the single remaining allele, NMD of the mutant allele in a dominant context).
(2) NMD, truncation or loss of function described in a recessive / biallelic / compound-heterozygous / homozygous context is "recessive_lof", never haploinsufficiency.
(3) "Dominant mutation causes X", "dominant-acting", "autosomal dominant inheritance" with no stated mechanism is "not_mechanism".
(4) If the sentence says the mechanism is unknown, debated, or contrasts haploinsufficiency with dominant-negative without deciding, use "other" with explicit=false.
(5) The gene symbol must appear in the PASSAGE; otherwise gene=null.

TASK. The PASSAGE is a run of numbered sentences from one paper. Give one item for each gene and sentence where the sentence names the gene together with a heterozygous / dominant / monoallelic allele, gene dosage, nonsense-mediated decay, or a dominant-negative / gain-of-function effect, and classify the mechanism it states. A sentence that only says a gene is mutated in / associated with / causes a disease, without any of that, gets no item.

Return ONLY JSON: {"items":[{"gene": HGNC symbol as written in the text (null if the text says only 'the gene' and no symbol appears in the PASSAGE),
 "mechanism_class": one of "haploinsufficiency" | "dominant_negative" | "gain_of_function" | "recessive_lof" | "other" | "not_mechanism",
 "explicit": true if the text asserts the mechanism for this gene; false if hedged (may, might, suggests, possibly, could, consistent with, likely),
 "negated": true if the text says this mechanism is NOT / unlikely the cause for this gene,
 "human_retinal": true ONLY if the phenotype named for this gene is a human retinal or ocular disease (retinitis pigmentosa, cone-rod dystrophy, LCA, macular dystrophy, Stargardt, achromatopsia, CSNB, optic atrophy, aniridia, etc.); false if the phenotype is non-ocular (cardiomyopathy, epilepsy, cancer, syndromic non-eye features...) or the evidence is only from animal models or cell lines,
 "phenotype": disease name as written in the text or null,
 "sentence": the number of the sentence the quote is,
 "quote": that ONE complete sentence copied VERBATIM, character for character, from the PASSAGE. Never paraphrase, never shorten, never merge sentences.}]}
If a sentence is a reference-list entry or a cited paper title rather than a statement by the authors, set mechanism_class to "not_mechanism".
If no gene mechanism is stated, return {"items":[]}."""

# Per-candidate flags: a journal citation in the sentence; extraction damage.
CIT = re.compile(r"(\b(19|20)\d{2}\s*[;:]\s*\d+\s*[:(]\d|\bdoi\s*:|\b(19|20)\d{2}\)\s*\d+[:(])")
GARBLED_MARK = re.compile(r"How to cite|Downloaded from|All rights reserved")


# ---------------------------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------------------------

AUTH = re.compile(r"^[A-Z][A-Za-z'-]+ [A-Z]{1,3}(,| and| &)|et al\.?,? \(?\d{4}\)?|et al\.?,? [A-Z][^.]{20,}, [A-Z][a-z]*\.?$")
CITN = re.compile(r"\b(19|20)\d{2}\s*[;:]\s*\d+|\bdoi\s*:|\(\d{4}\)\s*[A-Z]|^[A-Z][A-Za-z'-]+ [A-Z]{1,3}[,.]")
MECH = re.compile(r"haplo-?\s?insuffici\w*|gene dosage|dosage[- ]sensitiv\w*|dosage|nonsense[- ]mediated (mRNA )?decay|"
                  r"\bNMD\b|heterozygous (null|loss[- ]of[- ]function|truncating|nonsense|frameshift)[^,.;]{0,40}|"
                  r"loss of (one|a single) (allele|copy)|(one|single) (functional|normal|wild[- ]type) (copy|allele)[^,.;]{0,40}|"
                  r"reduced (level|amount|dosage) of [^,.;]{0,30}", re.I)
DOM = re.compile(r"heterozyg|monoallelic|dominant|haplo-?\s?insuffici|dosage|"
                 r"(one|single|a single) (functional|normal|wild[- ]type|intact|remaining) (copy|allele)|\bcarriers?\b", re.I)
# Round one also reads every chunk whose (ligature-folded) text says this: a dosage mechanism in words.
DOSAGE_WORDING = re.compile(
    r"haplo-?\s?insuffici\w*|gene[- ]dosage|dosage[- ]sensitiv\w*|dosage effect|reduced (?:gene )?dosage|"
    r"half[- ]dosage|(?:one|single|a single|the remaining) (?:functional|normal|wild[- ]type|intact|working) "
    r"(?:copy|allele)|loss of (?:one|a single) (?:copy|allele)|half (?:the |of the )?(?:normal )?(?:amount|level|dose)",
    re.I)
KEYWORD_REQUEST = ("every passage that states a dosage mechanism in words: haploinsufficiency, gene dosage, "
                   "dosage sensitivity, one functional copy or allele, loss of one copy, half the normal amount")


# ---------------------------------------------------------------------------------------------
# A scan for any property: the task spec
# ---------------------------------------------------------------------------------------------
#
# Everything above that names haploinsufficiency is the built-in rules, used when a call passes no
# ``spec``. A spec is the same rules for another property, written by a model from the user's
# one-line question (``SPEC_SYSTEM``, checked by ``parse_spec``): its definition and rules become
# the reading prompt, its requests and exact terms drive round one, and its terms are read in full
# (``SPEC_TERM_CAP``), as the built-in rules read every chunk their dosage wording matches.
# Retrieval, the pilot, the quote check, HGNC and the tables are the same code for every spec.

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
# A spec's own terms are read in full: they play the part of the built-in dosage wording, whose
# chunks are all read. The built-in cap (max_term_passages, 300, rarest first) is for the model's
# extra words; on a spec it dropped "haploinsufficiency" itself (332 of the 39,563 RetiGene
# chunks, rarest words first), and with it most of the genes the corpus states explicitly.
SPEC_TERM_CAP = 2000
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


def _bullets(xs: Iterable[str]) -> str:
    return "\n".join(f"- {x}" for x in xs)


def spec_judge_system(spec: dict[str, Any]) -> str:
    """The reading prompt for a spec: the built-in prompt's task and output, the spec's definition."""
    classes = " | ".join(f'"{c}"' for c in spec["classes"])
    return f"""You are a careful curator of inherited retinal disease (IRD) genetics. You judge ONLY from the text given. Never use prior knowledge about any gene.

TARGET: {spec["target"]}
DEFINITION. {spec["definition"]}
IT COUNTS WHEN:
{_bullets(spec["counts_when"])}
IT DOES NOT COUNT:
{_bullets(spec["does_not_count"])}
SCOPE. {spec["scope"]}

TASK. The PASSAGE is a run of numbered sentences from one paper. Give one item for each gene and sentence where the sentence says something about that gene that bears on the definition, including the near misses, and classify it with one of the classes. A sentence that only says a gene is mutated in, associated with or causes a disease, with nothing that bears on the definition, gets no item. The gene symbol must appear in the PASSAGE; otherwise gene=null. If a sentence is a reference-list entry or a cited paper title rather than a statement by the authors, its class is "not_relevant".

Return ONLY JSON: {{"items":[{{"gene": HGNC symbol as written in the text (null if no symbol appears in the PASSAGE),
 "class": one of {classes},
 "explicit": true if the text asserts it for this gene; false if hedged (may, might, suggests, possibly, could, consistent with, likely),
 "negated": true if the text says it is NOT so, or unlikely, for this gene,
 "in_scope": true ONLY if the phenotype and the evidence fit the SCOPE above,
 "phenotype": disease name as written in the text or null,
 "sentence": the number of the sentence the quote is,
 "quote": that ONE complete sentence copied VERBATIM, character for character, from the PASSAGE. Never paraphrase, never shorten, never merge sentences.}}]}}
If nothing in the passage bears on the definition, return {{"items":[]}}."""


def spec_gene_requests_system(spec: dict[str, Any]) -> str:
    return ("You are writing search requests for an embedding index of full-text papers on inherited retinal "
            "disease. A first search found passages that state or suggest the following for the genes listed "
            f"below, each with the phenotype the passages gave: {spec['target']} ({spec['definition']}) For "
            "EACH gene write ONE full sentence that would find the other passages that state it for that gene: "
            "name the gene, its phenotype, and the wording such a statement uses. Use only the gene symbols and "
            "phenotypes given; add no other gene.\n\n"
            'Return ONLY JSON: {"requests": {"GENE": "sentence", "...": "..."}}')


def spec_gene_request_template(spec: dict[str, Any]) -> str:
    """Round two's fallback request for a spec (``str.format`` with ``gene`` and ``phenotype``)."""
    target = spec["target"].replace("{", "(").replace("}", ")")
    return "{gene} in {phenotype}: a statement of " + target + " for {gene}."


def spec_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """A spec reading's items in the scan's own fields: ``class`` is ``mechanism_class`` and
    ``in_scope`` is ``human_retinal`` (hits.csv keeps its columns for every spec)."""
    if items is None:
        return None
    out = []
    for it in items:
        it = dict(it)
        it["mechanism_class"] = str(it.get("class") or it.get("mechanism_class") or "").strip().lower() or "other"
        if "human_retinal" not in it:
            it["human_retinal"] = bool(it.get("in_scope"))
        out.append(it)
    return out


def spec_folder(spec: dict[str, Any]) -> str:
    """Where a spec scan writes under ``work/`` and ``artifacts/``: one folder per spec."""
    return "curation_" + spec_digest(spec)[:10]


def spec_digest(spec: dict[str, Any]) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(json.dumps({f: spec[f] for f in SPEC_FIELDS}, sort_keys=True).encode("utf-8")).hexdigest()


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
    raw: str = ""                        # the model's whole reply, kept to parse again offline


@dataclass
class Passage:
    """A retrieved chunk, located: its paper, page and sentences, and the requests that found it."""
    sid: str                  # "<pmid>:<chunk>"
    pmid: str
    chunk: int
    page: int
    sentences: list[Sentence]
    found_by: list[str] = field(default_factory=list)   # "R3" = round-one request 3, "G:PRPF31"


# ---------------------------------------------------------------------------------------------
# 1. Text: PDF -> blocks -> chunks -> sentences
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
# 2. Requests and retrieval
# ---------------------------------------------------------------------------------------------

# A request takes at most this many chunks from one paper, so one long review cannot fill its k.
_MAX_PER_PAPER = 3
# Genes per round-two request-writing call.
_GENE_BATCH = 25


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


def request_messages(n: int) -> list[dict[str, str]]:
    return [{"role": "system", "content": REQUESTS_SYSTEM.replace("{N}", str(n))},
            {"role": "user", "content": f"Write the {n} search requests."}]


def parse_requests(text: str, n: int) -> list[str] | None:
    """The model's round-one requests: distinct full sentences, at most ``n``. None when the reply
    is not the JSON asked for or holds fewer than three usable requests."""
    data = _json_object(text)
    raw = data.get("requests") if data else None
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        if not isinstance(r, str):
            continue
        r = " ".join(r.split())
        if len(r.split()) < 4 or len(r) > 400 or r.lower() in seen:
            continue
        seen.add(r.lower())
        out.append(r)
    return out[:n] if len(out) >= min(3, n) else None


def gene_request_messages(genes: dict[str, list[str]], system: str | None = None) -> list[dict[str, str]]:
    lines = [f"{g} — {'; '.join(ph[:3]) if ph else 'phenotype not given'}" for g, ph in genes.items()]
    return [{"role": "system", "content": system or GENE_REQUESTS_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def parse_gene_requests(text: str, genes: Iterable[str]) -> dict[str, str] | None:
    """``gene -> request`` for the genes asked about; None when the reply holds none of them."""
    data = _json_object(text)
    raw = data.get("requests") if data else None
    if not isinstance(raw, dict):
        return None
    want = {g.upper(): g for g in genes}
    out: dict[str, str] = {}
    for k, v in raw.items():
        g = want.get(str(k).strip().upper())
        if g and isinstance(v, str) and len(v.split()) >= 4:
            out[g] = " ".join(v.split())[:400]
    return out or None


def nearest_chunks(q_vec: Any, vecs: Any, chunks: list[dict[str, Any]], k: int,
                   candidates: Iterable[int] | None = None, max_per_paper: int = _MAX_PER_PAPER) -> list[int]:
    """The ``k`` chunks nearest one request, at most ``max_per_paper`` from one paper (``_lib.corpus.nearest``)."""
    return nearest(q_vec, vecs, chunks, k, candidates, max_per_paper)


DOSAGE_TERM = Term("dosage wording", "pattern", DOSAGE_WORDING.pattern, source="tool")


def keyword_chunks(chunks: list[dict[str, Any]], index: TextIndex | None = None) -> list[int]:
    """The chunks whose text states a dosage mechanism in words (``DOSAGE_WORDING``, ligatures folded),
    in corpus order. Jin's brief asks for genes the papers report EXPLICITLY, and a request's nearest
    chunks miss most of them: a chunk's vector is its topic, and one word of 250 barely moves it."""
    index = index if index is not None else TextIndex.of_chunks(chunks)
    return sorted(index.counts(DOSAGE_TERM))


def model_term_chunks(raw_terms: list[str], index: TextIndex, cap: int,
                      already: Iterable[int] = ()) -> tuple[list[Term], set[int]]:
    """The model's words checked against the corpus, and the chunks the usable ones hold. A word is
    used when it is in the corpus, names no gene and is in at most 1 chunk of 100 (``FILTER_MAX_SHARE``
    / 2: a scan reads every chunk it is in); rarest first, while the chunks it adds beyond
    ``already`` (those read anyway) stay within ``cap``."""
    terms = index.check(parse_terms(raw_terms, "model"), FILTER_MAX_SHARE / 2)
    seen = set(already)
    chosen: set[int] = set()
    added = 0
    for t in sorted(terms, key=lambda t: t.n_passages):
        if t.kind == "gene" and t.use == "filter":
            t.use = "rank"                     # a gene name selects no mechanism
        if t.use != "filter":
            continue
        hold = set(index.counts(t))
        more = hold - chosen - seen
        if added + len(more) > cap:
            t.use = "over_cap"
            continue
        chosen |= hold
        added += len(more)
    return terms, chosen


def chunks_naming(terms: dict[str, set[str]], chunks: list[dict[str, Any]],
                  index: TextIndex | None = None) -> dict[str, set[int]]:
    """For each gene, the chunks that contain one of its symbols (``terms``) as a word. Case-sensitive:
    gene symbols are written in capitals, and a lower-case word is not the gene."""
    index = index if index is not None else TextIndex.of_chunks(chunks)
    return {g: index.matching(Term(t, "gene") for t in ts if t) for g, ts in terms.items()}


# ---------------------------------------------------------------------------------------------
# 3. Locate and read: one bounded JSON completion per retrieved chunk
# ---------------------------------------------------------------------------------------------

def locate(found: dict[int, list[str]], chunks: list[dict[str, Any]],
           sents_by_chunk: dict[tuple[str, int], list[Sentence]]) -> list[Passage]:
    """The retrieved chunks as passages (paper, page, numbered sentences), in retrieval order. A
    chunk with no sentence long enough to state anything is left out."""
    out: list[Passage] = []
    for i, labels in found.items():
        c = chunks[i]
        pmid, ci = str(c["pmid"]), int(c["chunk"])
        sents = sents_by_chunk.get((pmid, ci), [])
        if sents:
            out.append(Passage(f"{pmid}:{ci}", pmid, ci, int(c.get("page") or sents[0].page), sents, list(labels)))
    return out


def passage_prompt(p: Passage, system: str | None = None) -> list[dict[str, str]]:
    lines = [f"PAPER: PMID {p.pmid}, page {p.page}", "PASSAGE:"]
    lines += [f"[{k + 1}] {s.text}" for k, s in enumerate(p.sentences)]
    return [{"role": "system", "content": system or JUDGE_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]


def parse_items(text: str) -> list[dict[str, Any]] | None:
    """The model's ``items``, or None when the reply is not the JSON asked for."""
    data = _json_object(text)
    if data is None:
        return None
    items = data.get("items", [])
    if not isinstance(items, list):
        return None
    return [it for it in items if isinstance(it, dict)]


def openai_judge_fn(base_url: str, model: str, timeout: float = 180.0, max_tokens: int = 2000,
                    ) -> Callable[[list[dict[str, str]]], str]:
    """``messages -> text`` against an OpenAI-compatible ``/v1`` (the scan job's vLLM):
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


# ---------------------------------------------------------------------------------------------
# A model the gateway calls for the job (the user's own API key): the relay
# ---------------------------------------------------------------------------------------------
#
# A key never leaves the Eye Server, so a scan on an API model cannot call it from HPC3. The job
# writes each prompt as a file under ``<relay>/req/``; the gateway reads it, calls the model with
# the user's key (recording the call and its cost against the user's budget) and writes the reply
# to ``<relay>/resp/``. Both sides write a temporary name and rename it into place.

RELAY_REQ, RELAY_RESP = "req", "resp"
_RELAY_POLL_S = 0.5          # how often the job looks for a reply


class ScanStopped(Exception):
    """The gateway declined to call the model, and not because of a fault: the spending cap was
    reached or the run was stopped. The pass ends at once; every passage read so far is kept."""


@dataclass
class Meter:
    """The model calls the gateway made for this scan and what they cost (as the gateway priced
    them). Empty on a model the job serves itself: that has no per-call price."""
    calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: dict[str, Any] | None, cost_usd: Any) -> None:
        u = usage or {}
        with self._lock:
            self.calls += 1
            self.input_tokens += int(u.get("prompt_tokens") or 0)
            self.output_tokens += int(u.get("completion_tokens") or 0)
            try:
                self.cost_usd += float(cost_usd or 0)
            except (TypeError, ValueError):
                pass

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {"calls": self.calls, "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens, "cost_usd": round(self.cost_usd, 6)}


def relay_judge_fn(relay_dir: Path, meter: Meter, *, max_tokens: int, timeout: float = 900.0,
                   poll: float | None = None) -> Callable[[list[dict[str, str]]], str]:
    """``messages -> text`` through the gateway (see the section comment). A reply that carries an
    error raises what the passage loop expects: ``ScanStopped`` ends the pass, a transport error
    counts toward the dead-endpoint streak, any other error is retried like an unparsable reply.
    No reply within ``timeout`` (the gateway went away) is a transport error."""
    req, resp = Path(relay_dir) / RELAY_REQ, Path(relay_dir) / RELAY_RESP
    req.mkdir(parents=True, exist_ok=True)
    resp.mkdir(parents=True, exist_ok=True)
    poll = _RELAY_POLL_S if poll is None else poll

    def call(messages: list[dict[str, str]]) -> str:
        rid = uuid.uuid4().hex
        tmp = req / f".{rid}.tmp"
        tmp.write_text(json.dumps({"id": rid, "messages": messages, "max_tokens": max_tokens}),
                       encoding="utf-8")
        os.replace(tmp, req / f"{rid}.json")
        answer = resp / f"{rid}.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if answer.is_file():
                try:
                    data = json.loads(answer.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(poll)          # seen before the rename landed everywhere: read again
                    continue
                answer.unlink(missing_ok=True)
                if data.get("usage") or data.get("cost_usd"):
                    meter.add(data.get("usage"), data.get("cost_usd"))
                err = str(data.get("error") or "")
                if not err:
                    return str(data.get("text") or "")
                kind = str(data.get("kind") or "")
                if kind == "stop":
                    raise ScanStopped(err)
                if kind == "transport":
                    raise urllib.error.URLError(err)
                raise ValueError(err)
            time.sleep(poll)
        # Withdraw the prompt, so a gateway that comes back late does not pay for an unread answer.
        (req / f"{rid}.json").unlink(missing_ok=True)
        raise urllib.error.URLError(f"no answer from the gateway within {timeout:.0f} s")

    return call


# After this many passages IN A ROW fail to reach the model, the endpoint is taken as gone (the
# job's model died) and the pass stops at once, instead of retrying every remaining passage
# against a dead port.
_DEAD_AFTER = 20
# How a pass that the gateway stopped (``ScanStopped``) reports itself, as opposed to a model that died.
STOPPED = "stopped: "
# Everything one model call can raise that fails that call, not the pass. IndexError / TypeError /
# AttributeError: a 200 whose body is not a completion (no choices, a null message).
_CALL_ERRORS = (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError)


def _connection_error(exc: BaseException) -> bool:
    """A transport failure (nothing listening, host down, reset), as opposed to an HTTP error the
    server answered with or a reply that was not the JSON asked for."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, OSError))


def _ask(fn: Callable[[list[dict[str, str]]], str], messages: list[dict[str, str]],
         parse: Callable[[str], Any], attempts: int = 3) -> tuple[Any, str, bool]:
    """One request-writing call: ``(parsed, error, unreachable)``. ``unreachable`` is true when every
    attempt failed in transport (the model is gone, not confused)."""
    err, transport = "", True
    for attempt in range(attempts):
        try:
            got = parse(fn(messages))
            if got is not None:
                return got, "", False
            err, transport = "not the JSON asked for", False
        except ScanStopped as exc:
            return None, f"{STOPPED}{exc}", True
        except _CALL_ERRORS as exc:
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            transport = transport and _connection_error(exc)
        time.sleep(2 * (attempt + 1))
    return None, err, transport


def judge_passages(passages: list[Passage], judge_fn: Callable[[list[dict[str, str]]], str],
                   out_path: Path, concurrency: int = 16, log: Callable[[str], None] = lambda s: None,
                   should_stop: Callable[[], bool] | None = None, attempts: int = 3,
                   dead_after: int | None = None, system: str | None = None,
                   parse: Callable[[str], list[dict[str, Any]] | None] | None = None,
                   ) -> tuple[dict[str, Judgment], str]:
    """Read every passage not already read in ``out_path``. Each line keeps the model's whole reply
    (``raw``): a line whose reply did not parse is parsed again first, so a parser fix costs no
    model call, and only a line with no usable reply is sent again. Returns ``(judgments,
    unreachable)``: the second is empty normally, else the error that made the pass give the
    endpoint up for dead. ``system`` and ``parse`` are a spec's reading prompt and item reader
    (default: the built-in rules')."""
    dead_after = dead_after or _DEAD_AFTER
    if parse is None:
        def parse(text: str) -> list[dict[str, Any]] | None:
            return parse_items(text)
    done: dict[str, Judgment] = {}
    if out_path.exists():
        with out_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                items = d.get("items")
                if items is None and d.get("raw"):
                    items = parse(str(d["raw"]))
                if items is not None:
                    done[d["sid"]] = Judgment(d["sid"], items, raw=str(d.get("raw") or ""))
    todo = [p for p in passages if p.sid not in done]
    log(f"reading {len(todo)} passages ({len(passages) - len(todo)} already read)")
    lock = threading.Lock()
    t0 = time.time()
    n = 0
    dead = threading.Event()
    dead_reason = ""
    streak = 0                      # consecutive transport failures, reset by any answer

    def one(p: Passage) -> Judgment:
        nonlocal streak, dead_reason
        err, raw = "", ""
        for attempt in range(attempts):
            if dead.is_set():
                return Judgment(p.sid, None, "model unreachable")
            if should_stop and should_stop():
                return Judgment(p.sid, None, "stopped")
            try:
                raw = judge_fn(passage_prompt(p, system))
                items = parse(raw)
                with lock:
                    streak = 0
                if items is not None:
                    return Judgment(p.sid, items, raw=raw)
                err = "not the JSON asked for"
            except ScanStopped as exc:
                with lock:
                    if not dead.is_set():
                        dead_reason = f"{STOPPED}{exc}"
                        dead.set()
                return Judgment(p.sid, None, "stopped")
            except _CALL_ERRORS as exc:
                err = f"{type(exc).__name__}: {str(exc)[:200]}"
                if _connection_error(exc):
                    with lock:
                        streak += 1
                        if streak >= dead_after and not dead.is_set():
                            dead_reason = err
                            dead.set()
                    continue        # a refused connection is not a transient the next second cures
            time.sleep(2 * (attempt + 1))
        return Judgment(p.sid, None, err, raw=raw)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(one, p) for p in todo]
        for fut in as_completed(futures):
            j = fut.result()
            with lock:
                # A passage that was never really read (the endpoint had died) is not written:
                # the file stays a record of the model's answers.
                if not (j.items is None and j.error in ("model unreachable", "stopped")):
                    fh.write(json.dumps({"sid": j.sid, "items": j.items, "error": j.error, "raw": j.raw}) + "\n")
                    fh.flush()
                done[j.sid] = j
                n += 1
                if n % 200 == 0:
                    log(f"read {n}/{len(todo)} in {time.time() - t0:.0f}s")
    if dead.is_set():
        log(f"the model stopped answering ({dead_reason}); "
            f"{sum(1 for p in passages if done.get(p.sid) and done[p.sid].items is not None)} "
            f"of {len(passages)} passages read so far")
    return done, dead_reason if dead.is_set() else ""


def _int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def locate_items(p: Passage, items: list[dict[str, Any]]) -> list[tuple[Sentence, dict[str, Any]]]:
    """Each item with the sentence of the passage its quote is: the sentence that holds the quote
    (or is held by it), else the sentence number the model gave, else a blank stand-in. The quote is
    verified afterwards against the sentence and its neighbours, so a wrong pick fails it."""
    out: list[tuple[Sentence, dict[str, Any]]] = []
    for it in items:
        q = nz(str(it.get("quote") or ""))
        hit = None
        if q:
            hit = next((s for s in p.sentences if q in nz(s.text)), None) \
                or next((s for s in p.sentences if len(nz(s.text)) > 20 and nz(s.text) in q), None)
        if hit is None:
            k = _int(it.get("sentence"))
            if k is not None and 1 <= k <= len(p.sentences):
                hit = p.sentences[k - 1]
        if hit is None:
            hit = Sentence(sid=f"{p.sid}:-", pmid=p.pmid, chunk=p.chunk, sent=-1, page=p.page, text="")
        out.append((hit, it))
    return out


def sentence_judgments(passages: list[Passage], judgments: dict[str, Judgment],
                       ) -> tuple[dict[str, Judgment], dict[str, Sentence], dict[str, dict[str, Any]]]:
    """The passages' items regrouped per sentence, as ``aggregate`` reads them: ``(judgments by
    sentence id, sentences by id, per-sentence flags)``."""
    out: dict[str, Judgment] = {}
    by_sid: dict[str, Sentence] = {}
    flags: dict[str, dict[str, Any]] = {}
    for p in passages:
        j = judgments.get(p.sid)
        if j is None or not j.items:
            continue
        for s, it in locate_items(p, j.items):
            by_sid[s.sid] = s
            flags.setdefault(s.sid, {"cite_like": bool(CIT.search(s.text)), "garbled": is_garbled(s.text)})
            out.setdefault(s.sid, Judgment(s.sid, [])).items.append(it)  # type: ignore[union-attr]
    return out, by_sid, flags


# A word that can be a gene symbol as written: 2-15 letters, digits or hyphens, one capital at least
# ("PRPF31", "Prpf31"), so a lower-case word the model put in the gene field never becomes a search term.
_SYMBOL_LIKE = re.compile(r"(?=[A-Za-z0-9-]*[A-Z])[A-Za-z0-9-]{2,15}")


def round_two_genes(passages: list[Passage], judgments: dict[str, Judgment], hgnc: "Hgnc",
                    target: str = "haploinsufficiency") -> dict[str, dict[str, Any]]:
    """The genes round one read as haploinsufficient (not negated; hedged and non-retinal included,
    since another paper may state them plainly) whose symbol HGNC knows, or every symbol-like one
    without an HGNC table: ``symbol -> {"terms": symbols to search for, "phenotypes": as written}``,
    in the order first read."""
    out: dict[str, dict[str, Any]] = {}
    for p in passages:
        j = judgments.get(p.sid)
        for it in (j.items if j and j.items else []):
            if it.get("mechanism_class") != target or it.get("negated"):
                continue
            written = re.sub(r"[^A-Za-z0-9-]", "", str(it.get("gene") or ""))
            sym, status = hgnc.norm(it.get("gene"))
            if not sym or status in ("missing", "unknown") or not _SYMBOL_LIKE.fullmatch(written):
                continue
            g = out.setdefault(sym, {"terms": {sym}, "phenotypes": []})
            g["terms"].add(written)
            ph = " ".join(str(it.get("phenotype") or "").split())
            if ph and ph not in g["phenotypes"]:
                g["phenotypes"].append(ph)
    return out


def is_garbled(text: str) -> bool:
    return bool(GARBLED_MARK.search(text)) or len(re.findall(r"[a-z]- [a-z]", text)) >= 2


# ---------------------------------------------------------------------------------------------
# 4. Verify, normalise, aggregate
# ---------------------------------------------------------------------------------------------

# Whitespace, hyphens and dashes of every kind, and control characters: a PDF can extract "60–80%"
# as "60\x0180%", and the model types the dash back in.
_NZ_DROP = re.compile("[\\s­\\-‐-―−\x00-\x1f\x7f]+")


def nz(s: str | None) -> str:
    """Text for comparing quotes: ligatures folded, lower case, no whitespace, dashes or control
    characters."""
    return _NZ_DROP.sub("", fold(s).lower())


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
               "quote", "quote_verified", "cite_like", "dominance_marker", "garbled", "sentence_id", "found_by",
               "eligible")
GENE_COLUMNS = ("gene", "phenotype", "mechanism_statement", "n_papers", "n_hits", "hedged_hits",
                "conflicting_mechanism", "flag_only_cited_titles", "flag_no_dominance_marker",
                "flag_gene_not_in_quote", "flag_gene_alias_or_unknown", "gene_status", "gene_as_written",
                "flag_garbled", "pmid_1", "quote_1", "pmid_2", "quote_2", "pmid_3", "quote_3")


def eligible(h: dict[str, Any], target: str = "haploinsufficiency") -> bool:
    """genes.csv's entry condition = the hard requirements of the brief; everything else is a flag.
    ``target``: a spec's target class (``human_retinal`` then holds the spec's in-scope)."""
    return bool(h["mechanism_class"] == target and h["explicit"] and not h["negated"]
                and h["human_retinal"] and h["quote_verified"] and h["gene"] and h["gene_status"] != "missing")


def _word(gw: str, text: str) -> bool:
    return bool(gw) and re.search(r"(?<![A-Za-z0-9])" + re.escape(gw) + r"(?![A-Za-z0-9])", text, re.I) is not None


def aggregate(judgments: dict[str, Judgment], by_sid: dict[str, Sentence], flags: dict[str, dict[str, Any]],
              hgnc: Hgnc, titles: Iterable[str], max_quotes: int = 3, *, spec: dict[str, Any] | None = None,
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(gene rows, hit rows)`` from the raw judgments. With a ``spec``: its target class is listed,
    its near-miss classes are the conflicting ones, the statement is the spec term the quote holds,
    and there is no dominance flag (that one belongs to haploinsufficiency)."""
    target = spec["classes"][0] if spec else "haploinsufficiency"
    near = tuple(c for c in spec["classes"][1:] if c not in ("other", "not_relevant")) if spec \
        else ("dominant_negative", "gain_of_function")
    statement = MECH if spec is None else (
        re.compile("|".join(re.escape(t) for t in sorted(spec["terms"], key=len, reverse=True)), re.I)
        if spec["terms"] else None)
    prefer = "haploinsuffic" if spec is None else (spec["terms"][0].lower() if spec["terms"] else "")
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
            m = (statement.search(fold(q)) or statement.search(fold(s.text))) if statement else None
            h = {"pmid": s.pmid, "page": s.page, "gene": gene, "gene_as_written": gw, "gene_status": gstat,
                 "gene_in_quote": _word(gw, q), "gene_in_context": _word(gw, ctx),
                 "mechanism_class": it.get("mechanism_class"), "explicit": bool(it.get("explicit")),
                 "negated": bool(it.get("negated")), "human_retinal": bool(it.get("human_retinal")),
                 "phenotype": it.get("phenotype"), "mechanism_statement": m.group(0).strip() if m else "",
                 "quote": q, "quote_verified": bool(q) and nz(q) in nz(ctx),
                 "cite_like": bool(fl.get("cite_like")) or is_cite(q, s.prev, s.next) or is_cite(s.text, s.prev, s.next),
                 "dominance_marker": bool(DOM.search(fold(q + " " + ctx))) if spec is None else True,
                 "garbled": bool(fl.get("garbled")), "sentence_id": sid}
            h["eligible"] = eligible(h, target)
            hits.append(h)

    by_gene: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for h in hits:
        if h["eligible"]:
            by_gene[h["gene"]].append(h)
    other: dict[str, set[str]] = collections.defaultdict(set)
    hedged: collections.Counter[str] = collections.Counter()
    for h in hits:
        if h["gene"] and h["mechanism_class"] in near and h["explicit"] and not h["negated"]:
            other[h["gene"]].add(h["mechanism_class"])
        if h["gene"] and h["mechanism_class"] == target and not h["explicit"] and h["human_retinal"]:
            hedged[h["gene"]] += 1
    out: list[dict[str, Any]] = []
    for g, hs in sorted(by_gene.items()):
        # Quotes: body sentences before reference entries, clean before garbled, the gene in the
        # quote, the target's own word ("haploinsufficiency", or a spec's first term), then the
        # longer sentence.
        hs = sorted(hs, key=lambda h: (h["cite_like"], h["garbled"], not h["gene_in_quote"],
                                       prefer not in fold(h["quote"]).lower(), -len(h["quote"])))
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
            "mechanism_statement": picked[0]["mechanism_statement"] or (spec["target"] if spec else "haploinsufficiency"),
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
    """``(model, api_base)`` of the model to judge with: the args' endpoint, else
    ``AISCIENTIST_PAPERQA_LLM_BASE_URL`` (set inside the scan's own GPU job), else a tunnel port."""
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
    "n_requests": (16, "how many search requests the model writes for the first round"),
    "k_per_request": (50, "passages (chunks) each first-round request retrieves"),
    "keyword_round": (1, "1 = round one also reads every passage that states a dosage mechanism in words "
                         "(haploinsufficiency, gene dosage, one functional copy, ...); 0 = the requests alone"),
    "term_round": (1, "1 = the model also writes the exact words a qualifying passage would use; each is "
                      "counted in the corpus, and every passage holding one that is in at most 1% of the "
                      "passages is read too; 0 = the built-in dosage wording only"),
    "max_term_passages": (300, "at most this many passages the model's words add (rarest words first)"),
    "k_per_gene": (15, "passages each second-round gene request retrieves, among the chunks that name the gene"),
    "max_quotes_per_gene": (3, "how many papers (PMID + verbatim quote) genes.csv lists per gene"),
    "concurrency": (16, "parallel requests to the model while it reads the passages"),
    "max_passages": (0, "read only the first N retrieved passages (0 = all); for a smoke test"),
    "pilot_fraction": (0.01, "share of round one's passages read first as a pilot (at least 5, at most 25): the "
                             "scan stops before the rest when half of them fail, or when the cost they project "
                             "is above max_cost_usd (0 = no pilot)"),
    "max_cost_usd": (0.0, "stop before the full pass when the pilot projects a model cost above this, in USD "
                          "(0 = no cap; only a model the gateway calls for the scan has a price)"),
    "check_unread": (None, "1 = after round two, read the chunks that hold a usable term (the spec's terms or "
                           "the model's words) but were not read, up to max_unread_passages; 0 = only count "
                           "them; unset = on for a spec, off for the built-in rules"),
    "max_unread_passages": (600, "at most this many unread chunks the check reads"),
}
# Pilot size bounds: 1% of 857 passages is 9; a small scan still gets 5, a huge one at most 25.
_PILOT_MIN, _PILOT_MAX = 5, 25
REQUEST_COLUMNS = ("id", "round", "gene", "request", "n_passages")


def _schema() -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name, (default, meaning) in PARAMS.items():
        props[name] = {"type": "number" if isinstance(default, float) else "integer", "description": meaning}
        if default is not None:
            props[name]["default"] = default
    props["pmids"] = {"type": "array", "items": {"type": "string"},
                      "description": "restrict the scan to these PMIDs (empty = the whole corpus)"}
    props["spec"] = {
        "type": "object",
        "description": ("a task spec for another property than haploinsufficiency, drafted from the user's question "
                        "(fields: " + ", ".join(SPEC_FIELDS) + "; see TOOL.md). Its definition and rules are the "
                        "reading prompt, its requests and terms drive round one and its terms are read in full. "
                        "Omit it for the built-in haploinsufficiency rules"),
        "properties": {f: ({"type": "array", "items": {"type": "string"}} if f in _SPEC_LIST_FIELDS
                           else {"type": "string"}) for f in SPEC_FIELDS},
        "required": list(SPEC_FIELDS)}
    return {"type": "object", "properties": props}


def _p(name: str, args: dict[str, Any]) -> Any:
    v = args.get(name)
    return PARAMS[name][0] if v is None else v


def pilot_sample(passages: list[Passage], fraction: float) -> list[Passage]:
    """About ``fraction`` of the passages (``_PILOT_MIN`` to ``_PILOT_MAX``), spread evenly over the
    list, so the pilot sees passages of every request and of the keyword round."""
    if not passages or fraction <= 0:
        return []
    n = min(len(passages), max(_PILOT_MIN, min(_PILOT_MAX, math.ceil(len(passages) * fraction))))
    step = len(passages) / n
    return [passages[int(i * step)] for i in range(n)]


def _save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run_extract(args: dict[str, Any], ctx: Any, *, judge_fn: Callable[[list[dict[str, str]]], str] | None = None,
                embed_fn: Callable[[list[str]], Any] | None = None, log: Callable[[str], None] | None = None,
                ) -> dict[str, Any]:
    """The whole pipeline for one run. ``judge_fn`` / ``embed_fn`` default to the scan job's model
    and the corpus' PubMedBERT; the tests pass fakes. A rerun in the same workspace resumes: the
    requests and every passage already read are kept in ``work/haploinsufficiency/``."""
    import numpy as np  # noqa: PLC0415
    log = log or (lambda s: print(f"[{_TOOL}] {s}", flush=True))
    ws = getattr(ctx, "workspace", None)
    if not ws:
        return {"status": "error", "step": "literature", "error": f"{_TOOL} needs a run workspace"}
    ws = Path(ws)
    # A spec (another property, drafted from the user's question) or the built-in rules.
    spec: dict[str, Any] | None = None
    if args.get("spec"):
        try:
            spec = parse_spec(args["spec"])
        except ValueError as exc:
            return {"status": "error", "step": "literature", "error": f"The task spec cannot drive a scan: {exc}"}
    out_subdir = spec_folder(spec) if spec else _OUT_SUBDIR
    target = spec["classes"][0] if spec else "haploinsufficiency"
    read_system = spec_judge_system(spec) if spec else None
    read_parse = (lambda text: spec_items(parse_items(text))) if spec else None
    paths = _corpus_paths(ctx, work_subdir=_OUT_SUBDIR)
    if paths is None:
        return {"status": "not_enabled", "step": "literature",
                "note": (f"{_TOOL} reads the PaperQA corpus on HPC3 (AISCIENTIST_PAPERQA_PAPERS), which is "
                         "not available where this step ran. It runs as an HPC3 literature job.")}
    warnings = list(paths.notes)
    write_fn = judge_fn
    meter = Meter()
    relay = getattr(ctx, "llm_relay_dir", None)
    if judge_fn is None and relay:
        # The user's API model, called by the gateway with the user's key (see relay_judge_fn). It
        # may think before it answers, so the reply gets room beyond the JSON.
        judge_fn = write_fn = relay_judge_fn(Path(relay), meter, max_tokens=8000)
        log(f"model {getattr(ctx, 'model', None) or '?'} through the gateway (relay {relay})")
    elif judge_fn is None:
        ep = local_endpoint(ctx)
        if ep is None:
            return {"status": "not_enabled", "step": "literature",
                    "note": "no model to read with (model / llm_base_url unset); the corpus cannot be read."}
        model, base = ep
        judge_fn = openai_judge_fn(base, model)
        write_fn = openai_judge_fn(base, model, max_tokens=2500)
        log(f"model {model} at {base}")

    work = ws / "work" / out_subdir
    art = ws / "artifacts" / out_subdir
    work.mkdir(parents=True, exist_ok=True)
    genes_csv, hits_csv, requests_csv = art / "genes.csv", art / "hits.csv", art / "requests.csv"
    pmids = {str(p) for p in (args.get("pmids") or []) if str(p).strip()} or None
    n_req = max(1, int(_p("n_requests", args)))
    k1, k2 = max(1, int(_p("k_per_request", args))), max(0, int(_p("k_per_gene", args)))
    max_p = int(_p("max_passages", args) or 0)
    concurrency = int(_p("concurrency", args))
    stop = getattr(ctx, "should_cancel", None)
    should_stop = stop if callable(stop) else None

    # The corpus: text cache, sentences per chunk, chunk vectors.
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
    log(f"{n_papers} papers, {len(chunks)} chunks, {len(sentences)} sentences ({time.time() - t0:.0f}s)")
    if not sentences or not chunks:
        return {"status": "failed", "step": "literature", "error": "no text extracted from the corpus",
                "warnings": warnings}
    sents_by_chunk: dict[tuple[str, int], list[Sentence]] = collections.defaultdict(list)
    for s in sentences:
        sents_by_chunk[(s.pmid, s.chunk)].append(s)
    for v in sents_by_chunk.values():
        v.sort(key=lambda s: s.sent)
    embed = embed_fn if embed_fn is not None else sentence_transformer_embed(paths.hf_cache)
    if embed is None:
        return {"status": "failed", "step": "literature",
                "error": ("The scan finds its passages with PubMedBERT (sentence-transformers and the embedding "
                          "model in the corpus' hf_cache), which is not available where this step ran."),
                "warnings": warnings}
    raw_vecs = (chunk_vectors(chunks, paths.cache, embed, paths.cache_writable, log) if not pmids
                else _l2(embed([c["text"] for c in chunks])))
    vecs = np.asarray(raw_vecs, dtype="float32")
    index = TextIndex.of_chunks(chunks)
    hgnc = Hgnc(paths.hgnc)
    manifest = read_manifest(paths.manifest)
    titles = [str(r.get("title") or "") for r in manifest]
    store_path = work / "requests.json"
    stored: dict[str, Any] = {}
    if store_path.is_file():
        try:
            stored = json.loads(store_path.read_text(encoding="utf-8"))
        except ValueError:
            stored = {}
    judgments_path = work / "judgments.jsonl"

    def counts(passages: list[Passage], judgments: dict[str, Judgment]) -> dict[str, Any]:
        n_ok = sum(1 for p in passages if judgments.get(p.sid) and judgments[p.sid].items is not None)
        return {"n_papers": n_papers, "n_sentences": len(sentences), "n_passages": len(passages), "n_judged": n_ok}

    def gone(reason: str, passages: list[Passage], judgments: dict[str, Judgment]) -> dict[str, Any]:
        # The job's model went away (out of memory, a lost node). Keep what was read and say so:
        # the gateway submits the job again, and the rerun picks up where this one stopped.
        c = counts(passages, judgments)
        if reason.startswith(STOPPED):
            # The gateway declined (the spending cap, a stop): not a fault, and not to be retried.
            return {"status": "failed", "step": "literature", "stopped": True,
                    "error": (f"The scan was stopped: {reason[len(STOPPED):]}. {c['n_judged']} of "
                              f"{len(passages)} retrieved passages are read and kept; a rerun reads only "
                              "the rest."),
                    **c, **({"api_usage": meter.public()} if meter.calls else {}), "warnings": warnings}
        return {"status": "failed", "step": "literature", "model_unreachable": True,
                "error": (f"The model stopped answering ({reason}); {c['n_judged']} of {len(passages)} "
                          "retrieved passages are read. Rerun once the model is back: the requests and "
                          "every passage read are kept, and only the rest are read."),
                **c, **({"api_usage": meter.public()} if meter.calls else {}), "warnings": warnings}

    # Round one: the model writes the requests; each takes its nearest chunks of the whole corpus.
    requests = stored.get("round1")
    if spec:
        requests = list(spec["requests"])
        if stored.get("round1") != requests:
            stored["round1"] = requests
            _save_json(store_path, stored)
    if not (isinstance(requests, list) and requests):
        requests, err, unreachable = _ask(write_fn, request_messages(n_req), lambda t: parse_requests(t, n_req))
        if requests is None:
            if unreachable:
                return gone(err, [], {})
            return {"status": "failed", "step": "literature",
                    "error": f"The model did not write usable search requests ({err}).", "warnings": warnings}
        stored["round1"] = requests
        _save_json(store_path, stored)
    log(f"round one: {len(requests)} requests")
    found: dict[int, list[str]] = {}
    hits_per_request: dict[str, int] = {}
    for r, qv in enumerate(_l2(embed(list(requests)))):
        got = nearest_chunks(qv, vecs, chunks, k1)
        hits_per_request[f"R{r + 1}"] = len(got)
        for i in got:
            found.setdefault(i, []).append(f"R{r + 1}")
    keyword_only = 0
    if spec is None and int(_p("keyword_round", args)):
        worded = keyword_chunks(chunks, index)
        hits_per_request["K"] = len(worded)
        for i in worded:
            keyword_only += i not in found
            found.setdefault(i, []).append("K")
        log(f"round one: {len(worded)} passages state a dosage mechanism in words, {keyword_only} of them "
            "found by no request")
    # The model's own words, checked against the corpus: a safety net for wordings the built-in list
    # does not have. Only the passages nothing else found are added.
    term_rows: list[Term] = []
    term_only = 0
    term_cap = args.get("max_term_passages")
    term_cap = max(0, int(term_cap if term_cap is not None
                          else SPEC_TERM_CAP if spec else PARAMS["max_term_passages"][0]))
    if int(_p("term_round", args)):
        raw_terms = list(spec["terms"]) if spec else stored.get("terms")
        if not isinstance(raw_terms, list):
            raw_terms, err, unreachable = _ask(write_fn, terms_messages(), parse_model_terms)
            if raw_terms is None:
                if unreachable:
                    return gone(err, [], {})
                warnings.append(f"The model did not write usable search words ({err}); the built-in dosage "
                                "wording was used alone.")
                raw_terms = []
            stored["terms"] = raw_terms
            _save_json(store_path, stored)
        term_rows, by_terms = model_term_chunks(raw_terms, index, term_cap, already=found)
        hits_per_request["T"] = len(by_terms)
        for i in sorted(by_terms):
            term_only += i not in found
            found.setdefault(i, []).append("T")
        log(f"round one: the {'spec' if spec else 'model'}'s {len(raw_terms)} words, "
            f"{sum(t.use == 'filter' for t in term_rows)} used, add {term_only} passages")
    passages = locate(found, chunks, sents_by_chunk)
    if max_p > 0:
        passages = passages[:max_p]
    log(f"round one: {len(passages)} passages in {len({p.pmid for p in passages})} papers")

    # The pilot: about 1% of the passages first. A model that cannot do the job (an endpoint that
    # answers with errors, a reply the parser cannot read) is found after a few calls, not after
    # the whole corpus has been paid for; on a priced model its cost projects the scan's.
    pilot = pilot_sample(passages, float(_p("pilot_fraction", args) or 0))
    pilot_info: dict[str, Any] | None = None
    if pilot:
        before = meter.public()
        judgments, unreachable = judge_passages(pilot, judge_fn, judgments_path, concurrency, log, should_stop,
                                            system=read_system, parse=read_parse)
        if unreachable:
            return gone(unreachable, passages, judgments)
        bad = [p for p in pilot if not (judgments.get(p.sid) and judgments[p.sid].items is not None)]
        after = meter.public()
        calls, spent = after["calls"] - before["calls"], after["cost_usd"] - before["cost_usd"]
        pilot_info = {"passages": len(pilot), "read": len(pilot) - len(bad), "calls": calls,
                      "cost_usd": round(spent, 6)}
        if calls and spent > 0:
            # Round two reads about as many passages again (857 then 766 in the 2026-10-20 scan).
            pilot_info["estimated_total_usd"] = round(spent / calls * 2 * len(passages), 4)
        log(f"pilot: {pilot_info['read']} of {len(pilot)} passages read"
            + (f", ${spent:.4f}; projected for the scan ${pilot_info['estimated_total_usd']:.2f}"
               if "estimated_total_usd" in pilot_info else ""))
        if 2 * len(bad) >= len(pilot):
            first = next((judgments[p.sid].error for p in bad if judgments.get(p.sid) and judgments[p.sid].error),
                         "no reply")
            return {"status": "failed", "step": "literature", "pilot_failed": True,
                    "error": (f"The pilot failed: {len(bad)} of its {len(pilot)} passages (about 1% of the "
                              f"{len(passages)} retrieved) could not be read ({first}). The rest was not read, "
                              "so nothing more was spent; fix the model or its endpoint and rerun."),
                    **counts(passages, judgments), "pilot": pilot_info,
                    **({"api_usage": meter.public()} if meter.calls else {}), "warnings": warnings}
        cap = float(_p("max_cost_usd", args) or 0)
        if cap > 0 and pilot_info.get("estimated_total_usd", 0) > cap:
            return {"status": "failed", "step": "literature", "stopped": True, "over_budget": True,
                    "error": (f"The pilot projects about ${pilot_info['estimated_total_usd']:.2f} for the scan, "
                              f"above the ${cap:.2f} it may spend. Only the pilot's {len(pilot)} passages were "
                              "read; raise the budget or the cap and rerun (they are kept)."),
                    **counts(passages, judgments), "pilot": pilot_info,
                    "api_usage": meter.public(), "warnings": warnings}

    judgments, unreachable = judge_passages(passages, judge_fn, judgments_path, concurrency, log, should_stop,
                                            system=read_system, parse=read_parse)
    if unreachable:
        return gone(unreachable, passages, judgments)

    # Round two: one request per gene round one read as haploinsufficient, searched only among the
    # chunks that name the gene; the passages not read yet are read.
    genes2 = round_two_genes(passages, judgments, hgnc, target) if k2 else {}
    gene_requests: dict[str, str] = dict(stored.get("round2") or {})
    todo = [g for g in genes2 if g not in gene_requests]
    templated = 0
    for b in range(0, len(todo), _GENE_BATCH):
        batch = {g: genes2[g]["phenotypes"] for g in todo[b:b + _GENE_BATCH]}
        got, err, unreachable = _ask(write_fn, gene_request_messages(
                                         batch, spec_gene_requests_system(spec) if spec else None),
                                     lambda t, gs=list(batch): parse_gene_requests(t, gs))
        if got is None and unreachable:
            stored["round2"] = gene_requests
            _save_json(store_path, stored)
            return gone(err, passages, judgments)
        for g, ph in batch.items():
            if got and g in got:
                gene_requests[g] = got[g]
            else:
                template = spec_gene_request_template(spec) if spec else GENE_REQUEST_TEMPLATE
                gene_requests[g] = template.format(gene=g, phenotype=ph[0] if ph else "retinal disease")
                templated += 1
    if todo:
        stored["round2"] = gene_requests
        _save_json(store_path, stored)
    if templated:
        warnings.append(f"The model's reply left out {templated} of {len(todo)} genes for round two; their "
                        "requests were written from a fixed template.")
    seen_chunks = {(p.pmid, p.chunk) for p in passages}
    found2: dict[int, list[str]] = {}
    round_one = {p.sid: p for p in passages}
    if genes2:
        naming = chunks_naming({g: genes2[g]["terms"] for g in genes2}, chunks, index)
        for g, qv in zip(genes2, _l2(embed([gene_requests[g] for g in genes2]))):
            got = nearest_chunks(qv, vecs, chunks, k2, candidates=naming.get(g, set()))
            hits_per_request[f"G:{g}"] = len(got)
            for i in got:
                key = (str(chunks[i]["pmid"]), int(chunks[i]["chunk"]))
                if key in seen_chunks:
                    p1 = round_one.get(f"{key[0]}:{key[1]}")
                    if p1 is not None and f"G:{g}" not in p1.found_by:
                        p1.found_by.append(f"G:{g}")
                else:
                    found2.setdefault(i, []).append(f"G:{g}")
    passages2 = locate(found2, chunks, sents_by_chunk)
    if max_p > 0:
        passages2 = passages2[:max(0, max_p - len(passages))]
    if passages2:
        log(f"round two: {len(genes2)} gene requests, {len(passages2)} more passages")
        judgments, unreachable = judge_passages(passages2, judge_fn, judgments_path, concurrency, log, should_stop,
                                            system=read_system, parse=read_parse)
        if unreachable:
            return gone(unreachable, passages + passages2, judgments)
    passages_all = passages + passages2

    # The check: chunks that hold a usable term (one in at most 1% of the chunks: a spec's terms,
    # the model's words) but that no round read, because a cap or the requests left them out.
    # Counted always; read (up to max_unread_passages) when check_unread is on, which it is for a
    # spec: its terms are what defines the property, and a chunk that says one is a candidate.
    check = _p("check_unread", args)
    check = (spec is not None) if check is None else bool(int(check))
    read_keys = {(p.pmid, p.chunk) for p in passages_all}
    core: set[int] = set()
    for t in term_rows:
        if t.use in ("filter", "over_cap"):
            core |= set(index.counts(t))
    unread = sorted(i for i in core if (str(chunks[i]["pmid"]), int(chunks[i]["chunk"])) not in read_keys)
    passages3: list[Passage] = []
    if check and unread and max_p <= 0:
        passages3 = locate({i: ["U"] for i in unread[:max(0, int(_p("max_unread_passages", args)))]},
                           chunks, sents_by_chunk)
        if passages3:
            log(f"check: {len(unread)} chunks hold a usable term but were not read; reading {len(passages3)}")
            hits_per_request["U"] = len(passages3)
            judgments, unreachable = judge_passages(passages3, judge_fn, judgments_path, concurrency, log,
                                                    should_stop, system=read_system, parse=read_parse)
            if unreachable:
                return gone(unreachable, passages_all + passages3, judgments)
    passages_all = passages_all + passages3
    unread_left = len(unread) - len(passages3)
    if unread_left > 0:
        warnings.append(f"{unread_left} chunks hold a usable term but were not read"
                        + ("" if check else " (check_unread is off)") + ".")

    # Verify and aggregate.
    c = counts(passages_all, judgments)
    failed = len(passages_all) - c["n_judged"]
    if failed:
        warnings.append(f"{failed} of {len(passages_all)} passages could not be read (model errors); rerun the "
                        "tool to retry them: finished passages are kept.")
    if passages_all and c["n_judged"] == 0:
        return {"status": "failed", "step": "literature", "model_unreachable": True,
                "error": "None of the retrieved passages could be read (every model call failed). "
                         "Check the model and rerun.", **c, "warnings": warnings}
    by_sent, by_sid, flags = sentence_judgments(passages_all, judgments)
    genes, hits = aggregate(by_sent, by_sid, flags, hgnc, titles, int(_p("max_quotes_per_gene", args)), spec=spec)
    found_by = {p.sid: "|".join(p.found_by) for p in passages_all}
    for h in hits:
        h["found_by"] = found_by.get(str(h["sentence_id"]).rsplit(":", 1)[0], "")
    hits.sort(key=lambda h: (str(h["pmid"]), str(h["sentence_id"])))
    write_csv(genes_csv, genes, GENE_COLUMNS)
    write_csv(hits_csv, hits, HIT_COLUMNS)
    req_rows = [{"id": f"R{i + 1}", "round": 1, "gene": "", "request": q,
                 "n_passages": hits_per_request.get(f"R{i + 1}", 0)} for i, q in enumerate(requests)]
    if "K" in hits_per_request:
        req_rows.append({"id": "K", "round": 1, "gene": "", "request": KEYWORD_REQUEST,
                         "n_passages": hits_per_request["K"]})
    if "T" in hits_per_request:
        req_rows.append({"id": "T", "round": 1, "gene": "", "n_passages": hits_per_request["T"],
                         "request": ("the spec's terms: " if spec else "the model's words: ") + "; ".join(
                             f"{t.text} ({t.n_passages}, {t.use})" for t in term_rows)})
    if "U" in hits_per_request:
        req_rows.append({"id": "U", "round": 3, "gene": "", "n_passages": hits_per_request["U"],
                         "request": "the check: chunks holding a usable term that no round had read"})
    req_rows += [{"id": f"G:{g}", "round": 2, "gene": g, "request": gene_requests[g],
                  "n_passages": hits_per_request.get(f"G:{g}", 0)} for g in genes2]
    write_csv(requests_csv, req_rows, REQUEST_COLUMNS)
    n_elig = sum(1 for h in hits if h["eligible"])
    log(f"{c['n_judged']} passages read -> {n_elig} eligible hits -> {len(genes)} genes "
        f"({time.time() - t0:.0f}s total)")
    flag_keys = [k for k in GENE_COLUMNS if k.startswith("flag_")]
    return {
        "status": "ok", "step": "literature", **c,
        "n_requests": len(requests), "n_gene_requests": len(genes2),
        "n_keyword_passages": hits_per_request.get("K", 0), "n_keyword_only_passages": keyword_only,
        "n_term_passages": hits_per_request.get("T", 0), "n_term_only_passages": term_only,
        "terms": term_report(term_rows),
        "n_hits_eligible": n_elig, "n_genes": len(genes),
        "genes": [{"gene": g["gene"], "n_papers": g["n_papers"], "phenotype": (g["phenotype"] or "")[:80],
                   "flags": [k[5:] for k in flag_keys if g[k]]} for g in genes],
        "genes_flagged_for_review": [g["gene"] for g in genes if any(g[k] for k in flag_keys)],
        "requests": list(requests),
        "files": {"genes_csv": str(genes_csv.relative_to(ws)), "hits_csv": str(hits_csv.relative_to(ws)),
                  "requests_csv": str(requests_csv.relative_to(ws))},
        "definition": spec["definition"] if spec else DEFINITION,
        **({"spec": {"target": spec["target"], "classes": spec["classes"], "scope": spec["scope"],
                     "terms": spec["terms"], "folder": out_subdir, "rules": rules_digest(spec)}} if spec else {}),
        "unread_check": {"on": check, "found": len(unread), "read": len(passages3), "left": unread_left},
        "pilot": pilot_info,
        **({"api_usage": meter.public()} if meter.calls else {}),
        "warnings": warnings,
        "raw_data_to_llm": False,
    }


# ---------------------------------------------------------------------------------------------
# The rules as one fingerprint
# ---------------------------------------------------------------------------------------------

# Bump when a code change alters which passages are retrieved, how a reply is read, or how
# judgments become rows (``nearest_chunks``, ``chunks_naming``, ``round_two_genes``, ``parse_items``,
# ``locate_items``, ``eligible``, ``aggregate``). The constants below are fingerprinted as they
# are, so editing them needs no bump.
RULES_REVISION = 4


def rules_digest(spec: dict[str, Any] | None = None) -> str:
    """A fingerprint of everything that decides the scan's tables besides the corpus and the model:
    the definition, the model's instructions, the retrieval settings and the table columns. A stored
    scan whose digest differs was made under other rules (gateway/corpus_scan.py does not reuse it).
    With a ``spec`` (checked by ``parse_spec``) the spec and the prompts built from it are added, so
    each spec has its own digest and the built-in rules' digest is unchanged."""
    import hashlib  # noqa: PLC0415

    sample = Passage("p:0", "p", 0, 1, [Sentence(sid="p:0:0", pmid="p", chunk=0, sent=0, page=1, text="TEXT")])
    parts = [
        str(RULES_REVISION), JUDGE_SYSTEM, REQUESTS_SYSTEM, GENE_REQUESTS_SYSTEM, GENE_REQUEST_TEMPLATE,
        TERMS_SYSTEM, f"filter_max_share={FILTER_MAX_SHARE}",
        DEFINITION, ",".join(MECHANISM_CLASSES),
        passage_prompt(sample)[1]["content"], request_messages(1)[1]["content"],
        *(rx.pattern for rx in (CIT, GARBLED_MARK, AUTH, CITN, MECH, DOM, DOSAGE_WORDING)), KEYWORD_REQUEST,
        f"max_per_paper={_MAX_PER_PAPER}", f"gene_batch={_GENE_BATCH}",
        *(f"{k}={PARAMS[k][0]}" for k in ("n_requests", "k_per_request", "keyword_round", "term_round",
                                          "max_term_passages", "k_per_gene", "max_quotes_per_gene")),
        ",".join(HIT_COLUMNS), ",".join(GENE_COLUMNS), ",".join(REQUEST_COLUMNS),
    ]
    if spec is not None:
        parts += ["spec", json.dumps({f: spec[f] for f in SPEC_FIELDS}, sort_keys=True), spec_judge_system(spec),
                  spec_gene_requests_system(spec), spec_gene_request_template(spec), f"spec_term_cap={SPEC_TERM_CAP}",
                  f"max_unread_passages={PARAMS['max_unread_passages'][0]}"]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def make_tool() -> HarnessTool:
    """The ``extract_haploinsufficient_genes`` record for the catalog (see ``TOOL.md``)."""
    return HarnessTool(
        _TOOL,
        "Search the indexed literature corpus for genes the papers explicitly report as "
        "HAPLOINSUFFICIENT in retinal disease (a heterozygous loss-of-function allele causes disease "
        "because one copy is not enough; not dominant-negative, not gain-of-function, not recessive). "
        "The model writes search requests, PubMedBERT retrieves the passages, the model reads each "
        "passage, and every quote is checked against the paper. Writes genes.csv (one row per gene: "
        "HGNC symbol, phenotype and mechanism in the paper's words, up to 3 PMIDs with verbatim "
        "quotes, review flags), hits.csv (every judged sentence) and requests.csv (the search "
        "requests). Use it when the user wants the list with evidence as a table; for one focused "
        "question use search_corpus or deep_literature. Slow: call it once per run.",
        _schema(),
        run_extract,
        reads_private_data=False, category="literature", requires=("paper-qa",),
    )
