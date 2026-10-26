"""Which skill line a request goes to (RCHENLAB/AiScientist issue 21).

The composer has no Research / Chat / Corpus scan menu any more: the agent decides the route. There
are two lines:

- ``literature``: a general scientific question that published papers answer and that needs none of
  the user's data, such as what is known about a gene, a disease or a mechanism, or which genes cause
  a condition. It becomes a one-step plan: search PubMed, read the abstracts, answer with citations
  (``agents/literature_answer.py``).
- ``analysis``: everything else, which is the research lab. Any request to compute something, any
  question about the user's own data or results ("why is cluster 3 high in RHO"), a patient's
  phenotype to diagnose, a variant to annotate.

The model decides and code guards it. A wrong route costs one message, because every request starts
with a plan the user reviews, and a note on a literature plan is routed again. The two errors still
differ: a data question answered from the literature ignores the data. So ``literature`` needs a
confident verdict, a more confident one when data is attached or the chat already ran an analysis;
a question that names the user's data is an analysis whatever the model says; and when the model
cannot be asked or its reply cannot be read, the request is an analysis, which is what every request
was before, unless it plainly asks for the literature and nothing else.

The routing prompt is written for the smallest model a session may serve (Qwen3.6-35B-A3B): short
definitions, worked examples in English and Chinese, and a one-line JSON reply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from .loop_utils import safe_json_loads

LITERATURE = "literature"
ANALYSIS = "analysis"
ROUTES = (LITERATURE, ANALYSIS)

# The confidence a ``literature`` verdict needs: alone, and with data attached or an earlier
# analysis in the chat (where a question is more often about them).
LITERATURE_CONFIDENCE = 0.6
LITERATURE_CONFIDENCE_WITH_CONTEXT = 0.75

ROUTER_SYSTEM = (
    "You route one message in AiScientist, a research assistant for eye and single-cell biology. "
    "Every message goes to exactly one line:\n"
    '- "literature": a general scientific question that published papers answer, needing none of '
    "the user's data and no computation: what is known about a gene, protein, disease, cell type, "
    "pathway, drug or mechanism; which genes cause a condition; what the evidence says; what a "
    "term, score, threshold or method means in general; a summary of recent papers. It is answered "
    "by a PubMed search with cited papers.\n"
    '- "analysis": anything that needs computation, data or analysis results: analyse, run, QC, '
    "cluster, annotate, normalise, integrate, compare groups, find differential or marker genes, "
    "plot, score, re-run or change a step; any question about the user's own data, files, samples, "
    "clusters, cells, variants or results; a patient's symptoms or phenotype to diagnose or to rank "
    "genes for; a VCF or variant to annotate.\n"
    "Rules:\n"
    '1. A message that refers to the user\'s data or results ("my data", "this dataset", "cluster 3", '
    '"these cells", "the samples I uploaded") is "analysis", even when it also asks for literature.\n'
    '2. A message that asks for an analysis AND a literature search is "analysis" (the analysis plan '
    "includes a literature step).\n"
    '3. With data attached or an earlier analysis in the chat, choose "literature" only when the '
    "message clearly does not need them.\n"
    '4. When unsure, choose "analysis".\n'
    "Examples:\n"
    '- "What is known about DDX41 in the retina?" -> literature\n'
    '- "Which genes cause Leber congenital amaurosis?" -> literature\n'
    '- "Summarize recent papers on Müller glia reprogramming in zebrafish" -> literature\n'
    '- "RHO 突变如何导致视网膜色素变性？" -> literature\n'
    '- "Is there evidence that ABCA4 haploinsufficiency causes disease?" -> literature\n'
    '- "Run QC on my two samples and cluster them" -> analysis\n'
    '- "Why is cluster 3 high in RHO?" -> analysis\n'
    '- "Find the marker genes of each cell type and check the literature on the top ones" -> analysis\n'
    '- "A patient has night blindness, constricted visual fields and bone-spicule pigment. Likely '
    'diagnosis?" -> analysis\n'
    '- "Annotate the variants in this VCF" -> analysis\n'
    '- "帮我比较一下两组样本里视杆细胞的差异表达基因" -> analysis\n'
    "Reply with ONLY one line of JSON: "
    '{"route": "literature" or "analysis", "confidence": 0.0 to 1.0, "reason": "at most 12 words"}'
)

# A message that names the user's own data or results. Rule 1 of the prompt, enforced: a question
# about the data must see the data, whatever the model answered.
_REFERS_TO_DATA = re.compile(
    r"\b(?:my|our|this|these|the\s+attached|the\s+uploaded|the\s+loaded)\s+"
    r"(?:data(?:set)?s?|files?|samples?|cells?|clusters?|results?|matrix|matrices|libraries|"
    r"h5ad|vcf|counts?|run|analysis|umap|plots?|figures?|report)\b"
    r"|\bcluster\s*#?\d+\b|\b(?:i|we)\s+(?:uploaded|attached|sequenced|profiled)\b"
    r"|\bin\s+(?:my|our|the)\s+(?:data|dataset|samples?|results?)\b"
    r"|我的数据|这个数据|这份数据|该数据|数据集里|这些细胞|这些样本|我上传|上传的|第\s*\d+\s*(?:个)?簇|cluster\s*\d+",
    re.IGNORECASE)

# The deterministic floor, used only when the model cannot be asked or its reply cannot be read.
# Literature only when the message plainly asks for it and asks for nothing that computes.
_LITERATURE_ASK = re.compile(
    r"\b(?:literature|papers?|publications?|published|pubmed|europe\s*pmc|review\s+(?:of|on)|"
    r"what\s+is\s+known|what'?s\s+known|what\s+do\s+we\s+know|is\s+there\s+(?:any\s+)?evidence|"
    r"evidence\s+(?:for|that|on|of)|which\s+genes\s+cause|what\s+genes\s+cause|genes?\s+(?:that|which)\s+cause)\b"
    r"|文献|论文|已知|研究进展|综述|哪些基因(?:会)?导致",
    re.IGNORECASE)
_ANALYSIS_ASK = re.compile(
    r"\b(?:analy[sz]e|analysis|run|re-?run|cluster(?:ing)?|qc|quality\s+control|annotat\w*|"
    r"normali[sz]\w*|integrat\w*|differential(?:ly)?|marker\s+genes?|plot\w*|umap|heatmap|"
    r"dot\s*plot|compare|comparison|score|upload\w*|vcf|h5ad|fastq|patient|diagnos\w*|phenotype)\b"
    r"|分析|聚类|注释|质控|比较|画图|作图|打分|差异表达|患者|诊断|表型|上传",
    re.IGNORECASE)


# The lab's own paper collection, named in a question (RCHENLAB/AiScientist issue 46): "RetiGene papers",
# "the corpus", "the lab's corpus". A question about those papers is answered from them alone. Anatomy
# that happens to be called a corpus (the corpus callosum) is not the collection.
_NAMES_CORPUS = re.compile(
    r"\bretigene\b"
    r"|\b(?:the|this|our|lab'?s|indexed)\s+(?:(?:lab'?s|literature|paper|papers'?|text|retinal)\s+)?corpus\b"
    r"(?!\s+(?:callosum|luteum|cavernosum|spongiosum|striatum|albicans))"
    r"|\bcorpus\s+(?:papers?|scan|search|curation)\b"
    r"|\bthe\s+lab'?s\s+(?:papers|paper\s+collection)\b"
    r"|语料库",
    re.IGNORECASE)
# A question that asks for sources beyond the lab's papers as well: PubMed, the public literature.
_ASKS_PUBMED = re.compile(
    r"\bpubmed\b|\beurope\s*pmc\b|\bmedline\b"
    r"|\b(?:public|published|wider|broader|general)\s+literature\b"
    r"|\b(?:outside|beyond)\s+(?:the\s+)?(?:retigene|corpus)\b"
    r"|\bother\s+(?:papers|publications|literature)\b"
    r"|公开文献|其他文献",
    re.IGNORECASE)


def names_corpus(text: str) -> bool:
    """Does the message name the lab's paper collection (the RetiGene corpus)?"""
    return bool(_NAMES_CORPUS.search(text or ""))


def asks_for_pubmed(text: str) -> bool:
    """Does the message ask for PubMed or the public literature?"""
    return bool(_ASKS_PUBMED.search(text or ""))


def corpus_only(text: str) -> bool:
    """A question about the RetiGene papers that does not also ask for PubMed: it is answered from
    the corpus alone (issue 46). PubMed is added only when the message asks for it."""
    return names_corpus(text) and not asks_for_pubmed(text)


@dataclass(frozen=True)
class Route:
    """Where a request goes, and why. ``source`` is ``model`` (the verdict was used as given),
    ``guard`` (code overruled the model) or ``rules`` (the model gave nothing usable)."""

    route: str
    confidence: float
    reason: str
    source: str
    raw_route: str = ""          # what the model answered, before any guard

    @property
    def literature(self) -> bool:
        return self.route == LITERATURE

    def to_dict(self) -> dict:
        return {"route": self.route, "confidence": round(self.confidence, 2), "reason": self.reason,
                "source": self.source, **({"model_route": self.raw_route} if self.raw_route else {})}


def refers_to_data(text: str) -> bool:
    """Does the message name the user's own data or results?"""
    return bool(_REFERS_TO_DATA.search(text or ""))


def route_by_rules(question: str, *, has_data: bool = False, has_prior: bool = False) -> Route:
    """The floor: literature only for a plain literature ask with no data, no earlier analysis and
    nothing in it that computes; an analysis otherwise."""
    q = question or ""
    if (not has_data and not has_prior and _LITERATURE_ASK.search(q)
            and not _ANALYSIS_ASK.search(q) and not refers_to_data(q)):
        return Route(LITERATURE, LITERATURE_CONFIDENCE, "asks for the literature, and nothing to compute",
                     "rules")
    return Route(ANALYSIS, 0.5, "the default line", "rules")


def router_messages(question: str, *, data: Iterable[str] = (), prior_request: str = "") -> list[dict]:
    """The routing call: the system prompt, then what the chat holds and the message."""
    names = [str(n) for n in data if str(n or "").strip()]
    shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
    held = f"{len(names)} item(s): {shown}" if names else "none"
    prior = " ".join(str(prior_request or "").split())[:300]
    user = (f"Data attached to this chat: {held}\n"
            f"Earlier analysis in this chat: {('“' + prior + '”') if prior else 'none'}\n"
            f"Message: {str(question or '').strip()[:4000]}")
    return [{"role": "system", "content": ROUTER_SYSTEM}, {"role": "user", "content": user}]


def parse_route(raw: str) -> "tuple[str, float, str] | None":
    """``(route, confidence, reason)`` from the model's reply, or None when it names no route.
    Tolerates a fenced block, a sentence around the JSON, a bare word, and a confidence given as a
    percentage or a string."""
    text = str(raw or "").strip()
    obj = safe_json_loads(text)
    if not isinstance(obj, dict):
        m = re.search(r"\{.*?\}", text, re.DOTALL)
        obj = safe_json_loads(m.group(0)) if m else None
    if isinstance(obj, dict):
        word = str(obj.get("route") or obj.get("line") or obj.get("intent") or "").strip().lower()
        try:
            conf = float(str(obj.get("confidence", "")).strip().rstrip("%"))
        except ValueError:
            conf = 0.0
        if conf > 1.0:
            conf = conf / 100.0
        reason = " ".join(str(obj.get("reason") or "").split())[:160]
    else:
        word, conf, reason = text.lower(), 0.0, ""
    hits = [r for r in ROUTES if r in word]
    if len(hits) != 1:
        return None
    return hits[0], max(0.0, min(1.0, conf)), reason


def route_request(complete: "Callable[[list[dict]], str] | None", question: str, *,
                  data: Iterable[str] = (), prior_request: str = "") -> Route:
    """Route one request. ``complete`` sends messages to the session's model and returns the reply
    (None = no model to ask). ``data`` names what is attached to the chat; ``prior_request`` is the
    request of the chat's earlier analysis, if any. Never raises."""
    names = [str(n) for n in data if str(n or "").strip()]
    has_data, has_prior = bool(names), bool(str(prior_request or "").strip())
    if complete is None:
        return route_by_rules(question, has_data=has_data, has_prior=has_prior)
    try:
        raw = complete(router_messages(question, data=names, prior_request=prior_request))
    except Exception:  # noqa: BLE001 - a routing failure must never strand the request
        return route_by_rules(question, has_data=has_data, has_prior=has_prior)
    parsed = parse_route(raw if isinstance(raw, str) else str(raw))
    if parsed is None:
        return route_by_rules(question, has_data=has_data, has_prior=has_prior)
    route, conf, reason = parsed
    if route == ANALYSIS:
        return Route(ANALYSIS, conf, reason, "model", route)
    if refers_to_data(question):
        return Route(ANALYSIS, conf, "the message refers to the user's data or results", "guard", route)
    need = LITERATURE_CONFIDENCE_WITH_CONTEXT if (has_data or has_prior) else LITERATURE_CONFIDENCE
    if conf < need:
        return Route(ANALYSIS, conf, f"not confident enough for a literature answer ({conf:.2f} < {need})",
                     "guard", route)
    return Route(LITERATURE, conf, reason, "model", route)
