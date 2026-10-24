"""What the user limited a run to, read once and then enforced by code.

Asked for on 2026-10-11: to test CellQC by hand, Yijun wanted a prompt that runs "only the matrix QC
and hands back the result matrix — none of the run_de / enrichment that follows — with a short
report, not the full one". Nothing in the planning path could honour that. The PI is told to "plan
EVERY analysis the question AND the data genuinely warrant", a matched research protocol lists the
whole pipeline, the design meeting adds steps, the plan review restores what looks missing, a
literature step is appended when the request mentions a report, and exploration grows the plan
mid-run. Any one of them puts the excluded work back.

So the limit is read from the request ONCE (:func:`read_scope`) and applied where a step can enter
the plan (:meth:`RunScope.filter`): the model still writes the plan, the code decides which steps a
limited request can contain.

Reading it. Explicit instructions are parsed deterministically — a clause that says "only do / just
run / 只做 …" names what to KEEP, one that says "do not run / skip / no … / 不做 …" names what to
EXCLUDE, each by a tool's own name (``run_de``, "run de") or a short list of plain words (QC, DE,
enrichment, GSEA, clustering, annotation, literature, composition). The cues are bound to a verb of
doing, so a question ABOUT biology ("genes expressed only in cluster 3", "no differential
expression between arms") is not read as a limit. When the request has looser limiting words and
the rules find nothing, the model is asked and its answer is checked against the tool catalog. A
request with no limiting words costs nothing and changes nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .loop_utils import safe_json_loads

# Tools that can do anything; a limit to named analyses does not include them unless it names them.
GENERIC_TOOLS = frozenset({"run_code", "run_shell", "run_in_environment"})
LITERATURE_TOOLS = frozenset({"deep_literature", "search_corpus", "literature_search"})
QC_ONLY_TOOLS = frozenset({"run_scanpy_qc", "run_cellqc", "run_doublet_detection"})
# Tools whose success does not make a run "more than QC": lookups, generic code, the closing call.
NOT_AN_ANALYSIS = frozenset({
    "finish", "inspect_dataset", "read_tool_source", "describe_environment", "search_skills",
    "read_skill_reference", "list_dir", "stat_path", "find_files", "read_text", "disk_usage",
    "run_code", "run_shell", "run_in_environment"})


def tools_that_ran(rounds: Any) -> "list[str]":
    """Each tool with at least one successful call in ``rounds``, in first-use order. Reads both
    round shapes: LabRound objects in a live run, dicts in run_state.json replays and tests."""
    seen: list[str] = []
    for rnd in rounds or []:
        sr = (rnd.get("scientist_result") if isinstance(rnd, dict)
              else getattr(rnd, "scientist_result", None))
        for st in ((sr or {}).get("steps") or []):
            if not isinstance(st, dict) or not st.get("tool") or st["tool"] in seen:
                continue
            r = st.get("result")
            if st.get("ok") or (isinstance(r, dict) and r.get("status") == "ok"):
                seen.append(str(st["tool"]))
    return seen


# A written report asked for in so many words. "brief report" and "不需要完整报告" still ask for one (a
# short one); only a report ruled out altogether ("no report", "不需要报告") does not.
_REPORT_WORD = re.compile(r"\breports?\b|\bwrite-?ups?\b|\bmanuscript\b|\b(?:pdf|docx)\b|报告|书面",
                          re.IGNORECASE)
_NO_REPORT = re.compile(
    r"\b(?:no|without(?: a| any)?|skip(?: the)?|don'?t (?:need|want|write)(?: a| any)?|"
    r"no need for(?: a| any)?)\s+(?:written\s+)?(?:report|write-?up)s?\b|"
    r"(?:不需要|不要|不用|无需|不必|别)(?:写|出|生成|做)?(?:任何)?(?:书面)?报告", re.IGNORECASE)


def wants_written_report(question: "str | None") -> bool:
    """Does the request ask for a written report? A run that only did QC answers in the chat unless
    it does (Yijun, 2026-10-18: no report asked for, the chat reply; a report asked for, the model
    writes it)."""
    q = question or ""
    return bool(_REPORT_WORD.search(q)) and not _NO_REPORT.search(q)


def qc_only(tools: "Iterable[str]") -> bool:
    """Is quality control all a run did? At least one QC tool succeeded and no other analysis did.

    Such a run is answered in the chat with the QC tool's own numbers — no research report, no
    model-written synthesis (Yijun, 2026-10-16, on run 6275df1213ab: a one-step CellQC run needs a
    short chat reply with the data, not a report)."""
    analyses = [t for t in tools if t not in NOT_AN_ANALYSIS]
    return bool(analyses) and all(t in QC_ONLY_TOOLS for t in analyses)


# What a generic-code script looks like when it writes a table, figure or file. Only consulted for a
# step that ran outside HPC3: an HPC3 run reports every file it wrote, so its list is the answer.
SCRIPT_WRITES = re.compile(
    r"AISCIENTIST_ARTIFACTS|savefig|\bsave\s*=|\.to_(?:csv|tsv|excel|parquet|json|pickle|feather|"
    r"hdf|zarr)\b|\.write\w*\s*\(|\bopen\s*\([^)]*['\"][wax]|\bnp\.save|\b(?:json|pickle)\.dump\b|"
    r"\bshutil\.|\bos\.(?:rename|replace|remove|makedirs|mkdir)\b|\.mkdir\s*\(")


def code_wrote_outputs(rounds: Any) -> bool:
    """Did a successful generic-code step write a table, figure or file? Such a step did analysis
    that no tool name shows: a skill applied with ``run_code`` (run 74873590ca7b scored every cell for
    an ISG signature and compared the arms that way, after ``run_scanpy_qc``), or a script the model
    added (run 1e887ed2bf8e's "ambient rod-leak audit" after CellQC). Reads both round shapes."""
    for rnd in rounds or []:
        sr = (rnd.get("scientist_result") if isinstance(rnd, dict)
              else getattr(rnd, "scientist_result", None))
        for st in ((sr or {}).get("steps") or []):
            if not isinstance(st, dict) or st.get("tool") not in GENERIC_TOOLS or not st.get("ok"):
                continue
            res = st.get("result")
            if isinstance(res, dict) and res.get("status") not in (None, "ok"):
                continue
            if isinstance(res, dict) and any(res.get(k) for k in ("tables", "figures", "artifacts")):
                return True
            if isinstance(res, dict) and res.get("execution_mode") == "hpc_slurm":
                # An HPC3 run reports every file it wrote (slurm_sandbox.classify_written), so it
                # reporting none is the answer. The code's look is only a guess: run 57c6b0c6ba0c's
                # script READ tables/cellqc_metrics.csv through AISCIENTIST_ARTIFACTS, counted as a
                # write, and the requested QC report went to the chat instead of the PDF.
                continue
            args = st.get("args") or {}
            if SCRIPT_WRITES.search(str(args.get("code") or args.get("command") or "")):
                return True
    return False


def qc_only_run(rounds: Any) -> bool:
    """Is quality control ALL the run did? :func:`qc_only` over the tools that ran, and no
    generic-code step wrote a table, figure or file. A run whose analysis was done in code is not a
    QC run, whatever its tool list says: run 74873590ca7b ran ``run_scanpy_qc`` and three ``run_code``
    steps that scored and compared an ISG signature, was written up as a QC report with no claim
    audit, and its report said "This run did quality control only"."""
    return qc_only(tools_that_ran(rounds)) and not code_wrote_outputs(rounds)

_CLAUSE_SPLIT = re.compile(r"[,;.!?，；。！？\n]+|\bbut\b|\bthen\b|但是?|然后|另外")
# A limit is an instruction about the WORK, so each cue is bound to a verb of doing or stands where
# an instruction stands. "Which genes are expressed only in cluster 3?" and "genes with no
# differential expression" are questions about biology and must not read as limits.
_LEAD = r"^\W*(?:(?:please|and|also|so|i|we)\s+)*"
_ONLY = re.compile(
    _LEAD + r"(?:only|just)\b|"
    r"\b(?:only|just)\s+(?:do|run|perform|need|want|plan)\b|"
    r"\b(?:do|run|perform|need|want|plan)\s+(?:only|just)\b|"
    r"\bonly\W*$|\bnothing (?:else|more)\b|\bstop after\b|"
    r"只做|只要|只需|只跑|只运行|只进行|仅做|仅需|只测|只执行", re.IGNORECASE)
_NEGATED = re.compile(
    r"\b(?:do not|don'?t|never)\s+(?:run|do|perform|include|plan|add|need|want)\b|"
    r"\bno need (?:for|to)\b|\bskip\b|"
    + _LEAD + r"(?:no|without)\b|\band no\b|"
    r"不做|不要|不用|不需要|无需|别做|别跑|跳过|不跑|不运行|不进行|不执行", re.IGNORECASE)
_EXCEPT = re.compile(r"\b(?:except|other than|apart from)\b|除了", re.IGNORECASE)
# Looser limiting words: worth asking the model about when the rules above found nothing.
_LIMIT_CUE = re.compile(r"\b(?:only|just|no|not|don'?t|skip|without|except|nothing else)\b|"
                        r"只|仅|不做|不要|不用|不需要|无需|别|跳过|除了", re.IGNORECASE)
_BRIEF_REPORT = re.compile(
    r"\b(?:brief|short|concise|minimal|compact|one[- ]page)\b[^.;\n]{0,24}\b(?:report|summary|write-?up)\b|"
    r"\b(?:report|summary|write-?up)\b[^.;\n]{0,30}\b(?:brief|short|concise|minimal)\b|"
    r"\bno(?:t)? (?:need (?:for )?)?(?:a |the )?(?:full|complete|long|detailed) report\b|"
    r"报告[^。；\n]{0,12}(?:简短|简要|精简|简洁|越短越好)|(?:简短|简要|精简|简洁)[^。；\n]{0,8}报告|"
    r"(?:不需要|不要|不用|无需)[^。；\n]{0,6}(?:完整|详细|长篇)[^。；\n]{0,4}报告|短报告", re.IGNORECASE)

# Plain words for an analysis -> the tools that run it. KEEP and EXCLUDE differ where an analysis
# needs a prerequisite: asking for "only annotation" needs clusters; ruling annotation out does not
# rule clustering out.
_QC = ("run_cellqc", "run_scanpy_qc", "run_doublet_detection")
_WORDS: "tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]" = (
    # (pattern, tools to keep, tools to exclude)
    (r"cell\s*-?\s*qc", ("run_cellqc",), ("run_cellqc",)),
    (r"\bqc\b|quality[- ]control|质控|质量控制", _QC, _QC),
    (r"\bdeg?s?\b|differential(?:ly)?[- ]express\w*|差异表达|差异分析|差异基因",
     ("run_de", "run_pseudobulk_de", "run_depth_matched_de"),
     ("run_de", "run_pseudobulk_de", "run_depth_matched_de")),
    (r"\benrichment\b|\bora\b|pathway analysis|富集|通路分析",
     ("run_enrichment", "run_gsea_prerank"), ("run_enrichment", "run_gsea_prerank")),
    (r"\bgsea\b", ("run_gsea_prerank",), ("run_gsea_prerank",)),
    (r"\bcluster(?:ing|s)?\b|聚类", ("run_clustering",), ("run_clustering",)),
    (r"\bannotat\w*|\bcell[- ]?types?\b|细胞注释|细胞类型|注释",
     ("run_clustering", "run_marker_annotation", "scgpt_annotate"),
     ("run_marker_annotation", "scgpt_annotate")),
    # "references" in the plural only, and not the things an analysis is run AGAINST: "annotate with
    # the retina reference" and "align to the reference genome" say nothing about citations.
    (r"\bliterature\b|\bcitations?\b|文献|参考文献|"
     r"\breferences\b(?!\s+(?:genomes?|atlas(?:es)?|datasets?|panels?|markers?|samples?))",
     tuple(sorted(LITERATURE_TOOLS)), tuple(sorted(LITERATURE_TOOLS))),
    (r"\bcomposition\b|\bproportions?\b|组成|比例", ("run_composition",), ("run_composition",)),
    (r"\bintegrat\w*|\bbatch correction\b|整合|批次校正", ("run_integration",), ("run_integration",)),
)

_SCOPE_SYSTEM = (
    "You read a researcher's request and report ONLY the limits they set on the work. Reply with a "
    'JSON object and nothing else: {"keep": [...], "exclude": [...], "brief_report": true|false}.\n'
    "- keep: fill it ONLY when the request says to do nothing but certain analyses (\"only QC\", "
    "\"just cluster and annotate\"). List the tool names, from the list below, that those analyses "
    "need — including a prerequisite the named analysis cannot run without — and no others.\n"
    "- exclude: tools the request says NOT to run (\"no DE\", \"skip enrichment\", \"without "
    "literature\").\n"
    "- brief_report: true when they ask for a short / brief / minimal report or say a full report "
    "is not needed.\n"
    "Use tool names exactly as listed. A request that sets no limit gets empty lists and false. Do "
    "not infer a limit from what the request merely leaves unmentioned.")


@dataclass(frozen=True)
class RunScope:
    """The limit a request put on a run. Empty (the default) limits nothing."""
    keep: "tuple[str, ...]" = ()        # run ONLY these tools
    exclude: "tuple[str, ...]" = ()     # never run these
    brief_report: bool = False
    source: str = ""                    # how it was read: "rules" | "model" | ""

    @property
    def limited(self) -> bool:
        return bool(self.keep or self.exclude)

    def allows(self, tool: "str | None") -> bool:
        """May a step whose own tool is ``tool`` be in the plan? A step naming no tool is kept
        under an exclusion (nothing says it is the excluded work) and dropped under a keep-list
        (nothing says it is the kept work)."""
        if tool is None:
            return not self.keep
        if tool in self.exclude:
            return False
        return not self.keep or tool in self.keep

    def allows_literature(self) -> bool:
        return any(self.allows(t) for t in LITERATURE_TOOLS)

    def filter(self, agenda: "list[str]",
               primary_tool: "Callable[[str], str | None]") -> "tuple[list[str], list[str]]":
        """``(kept, dropped)``. Never returns an empty plan: when every step would go, the limit
        could not be matched to the plan and the plan is returned whole (``dropped`` empty) for
        the caller to say so."""
        if not self.limited:
            return list(agenda), []
        kept = [s for s in agenda if self.allows(primary_tool(s))]
        if not kept:
            return list(agenda), []
        return kept, [s for s in agenda if s not in kept]

    def prompt_block(self) -> str:
        """The limit as a binding paragraph for whoever drafts or reviews the plan ('' if none)."""
        if not self.limited:
            return ""
        parts = ["SCOPE SET BY THE USER — binding. It overrides any research-path guidance and the "
                 "rule to plan every analysis the data warrant:"]
        if self.keep:
            parts.append("every step must be run by one of " + ", ".join(f"`{t}`" for t in self.keep)
                         + " — and of those, plan only the ones THIS dataset needs (the data "
                           "profile says which applies);")
        if self.exclude:
            parts.append("plan NO step run by " + ", ".join(f"`{t}`" for t in self.exclude) + ";")
        parts.append("do not add other work to make up for it (no literature, figure, audit or "
                     "follow-up step the request did not ask for). The processed dataset and the "
                     "report are produced automatically when the run ends — plan no step for them.")
        return " ".join(parts)

    def describe(self) -> str:
        """One line for the chat feed."""
        bits = []
        if self.keep:
            bits.append("only " + ", ".join(self.keep))
        if self.exclude:
            bits.append("not " + ", ".join(self.exclude))
        if self.brief_report:
            bits.append("brief report")
        return "; ".join(bits)

    def to_dict(self) -> "dict[str, Any]":
        return {"keep": list(self.keep), "exclude": list(self.exclude),
                "brief_report": self.brief_report, "source": self.source}

    @classmethod
    def from_dict(cls, data: Any) -> "RunScope":
        if not isinstance(data, dict):
            return cls()
        return cls(tuple(str(t) for t in data.get("keep") or ()),
                   tuple(str(t) for t in data.get("exclude") or ()),
                   bool(data.get("brief_report")), str(data.get("source") or ""))


def _mentioned(clause: str, tool_names: "frozenset[str]", keep: bool) -> "list[str]":
    """Tools a clause names: by their own name ("run_de", "run de") or by a plain word."""
    low = clause.lower()
    found: list[str] = []
    for name in sorted(tool_names, key=len, reverse=True):
        if re.search(r"(?<![a-z0-9_])" + re.escape(name).replace("_", r"[_ ]") + r"(?![a-z0-9_])", low):
            found.append(name)
    for pattern, keeps, excludes in _WORDS:
        if re.search(pattern, low, re.IGNORECASE):
            found += [t for t in (keeps if keep else excludes) if t in tool_names]
    return list(dict.fromkeys(found))


def _by_rules(question: str, tool_names: "frozenset[str]") -> "tuple[list[str], list[str]]":
    keep: list[str] = []
    exclude: list[str] = []
    for clause in _CLAUSE_SPLIT.split(question or ""):
        if not clause.strip():
            continue
        neg = _NEGATED.search(clause)
        if _ONLY.search(clause) or (neg and _EXCEPT.search(clause)):
            keep += _mentioned(clause, tool_names, keep=True)
        elif neg:
            # What is ruled out is named AFTER the cue ("QC and no DE" rules out DE, not QC).
            exclude += _mentioned(clause[neg.start():], tool_names, keep=False)
    keep = list(dict.fromkeys(keep))
    # What the request says to keep wins over a "no …" that happens to name the same tool.
    exclude = [t for t in dict.fromkeys(exclude) if t not in keep]
    return keep, exclude


def read_scope(question: str, tools: "dict[str, str]",
               complete: "Callable[[list[dict[str, Any]]], str] | None" = None) -> RunScope:
    """The limit ``question`` sets, given the catalog (``{tool name: one-line description}``).

    Rules first; the model (``complete``) only when the request has limiting words the rules could
    not turn into tools. Never raises: an unreadable reply is "no limit"."""
    names = frozenset(n for n in tools if n != "finish")
    brief = bool(_BRIEF_REPORT.search(question or ""))
    keep, exclude = _by_rules(question, names)
    if keep or exclude:
        return RunScope(tuple(keep), tuple(exclude), brief, "rules")
    if complete is None or not _LIMIT_CUE.search(question or ""):
        return RunScope(brief_report=brief, source="rules" if brief else "")
    listing = "\n".join(f"- {n}: {' '.join(str(d).split())[:160]}" for n, d in tools.items()
                        if n != "finish")
    try:
        raw = complete([{"role": "system", "content": _SCOPE_SYSTEM},
                        {"role": "user", "content": f"Request:\n{question}\n\nTools:\n{listing}\n\n"
                                                    "Reply with the JSON object."}])
        data = safe_json_loads(raw or "") or {}
    except Exception:  # noqa: BLE001 - reading a limit must never cost the run
        data = {}
    keep = [str(t) for t in (data.get("keep") or []) if str(t) in names]
    exclude = [str(t) for t in (data.get("exclude") or []) if str(t) in names and str(t) not in keep]
    brief = brief or bool(data.get("brief_report"))
    if not (keep or exclude or brief):
        return RunScope()
    return RunScope(tuple(dict.fromkeys(keep)), tuple(dict.fromkeys(exclude)), brief,
                    "model" if (keep or exclude) else "rules")
