"""What kind of step a plan line is: literature, report, enrichment, DE, clustering; dataset mode rules."""

from __future__ import annotations

import re
from typing import Any, Callable
from ..dag import TaskNode
from ..tool_schema import REPORT_TOOL
from ..tool_names import canonical_tool_name

from .tooling import _declared_tools, _primary_tool


# --- concurrency safety (DAG scheduler) --------------------------------------
# Two ready nodes may run CONCURRENTLY only if their mutable footprints are disjoint. The scanpy
# analysis line shares BOTH the on-disk checkpoint chain (adata_qc/clustered/de.h5ad) AND scanpy's
# process-global state (sc.settings.figdir) — so every analysis/code node carries an "__analysis__"
# sentinel and no two of them can overlap. A literature/background node touches no checkpoint and no
# scanpy state (it reads accepted findings in-memory + hits an external API), so its footprint is
# empty and it CAN run alongside the analysis chain. This is the conservative default: anything not
# clearly independent stays sequential.
_READ_ONLY_TOOLS = frozenset({"literature_search", "deep_literature", "search_corpus", "make_schematic"})


def _node_resources(node: TaskNode) -> set[str]:
    if _is_literature_step(node.goal) or (canonical_tool_name(node.suggested_tool) in _READ_ONLY_TOOLS):
        return set()                          # external/read-only: no shared mutable resource
    return {"__analysis__", *node.produces, *node.consumes}


def _concurrency_safe(a: TaskNode, b: TaskNode) -> bool:
    """True when nodes ``a`` and ``b`` can safely run at the same time (disjoint footprints)."""
    return _node_resources(a).isdisjoint(_node_resources(b))


#: Tools whose presence MAKES a step a literature step. The catalog's real names — the historical
#: set named ``run_literature``/``run_paperqa``, neither of which the Scientist catalog serves, so
#: once the guard could see tool names at all a genuine ``literature_search`` step would have been
#: classified as NOT-literature. Kept together to make that pairing impossible to miss.
_LITERATURE_TOOLS = frozenset({
    "literature_search", "deep_literature", "search_corpus", "run_literature", "run_paperqa",
    "run_paperqa_search",
})


_LITERATURE_STEP_RE = re.compile(
    # `references?` must not fire on the `reference=` ARGUMENT of run_de — a step reading
    # `run_de(reference="WT")` was being classified as a literature step and routed to the
    # literature fast path, which is how the plan's central contrast goes missing.
    r"\b(literature|references?(?!\s*[=:])|citations?|published papers?|paper search)\b"
    r"|\b(?:published|biomedical|biological|scientific|literature)\s+background\b"
    r"|\bliterature\s+context\b|\bbiological\s+context\b|\bbiological\s+interpretation\b",
    re.I,
)
# "references" in the plural only, and not the things an analysis is run AGAINST: "annotate with
# the retina reference" and "align to the reference genome" are not requests for citations.
_REQUESTS_LITERATURE_RE = re.compile(
    r"\b(literature|citations?|published papers?|paper search|bibliography)\b"
    r"|\breferences\b(?!\s+(?:genomes?|atlas(?:es)?|datasets?|panels?|markers?|samples?))"
    r"|\b(?:published|biomedical|biological|scientific|literature)\s+background\b"
    r"|\bliterature\s+context\b|\bbiological\s+context\b|\bbiological\s+interpretation\b",
    re.I,
)
_LOW_PRIORITY_FOR_LITERATURE_RE = re.compile(
    r"\b(enrich|enrichment|pathway|annotat|assign|label|marker genes per cluster|markers per cluster)\b",
    re.I,
)

#judging whether is literature step
def _is_literature_step(step: str) -> bool:
    """Is this step a literature search? Decided by the TOOL it declares, not by its prose.

    A step's role is a structured fact the planner already produces — the tool name — and reading it
    from the prose instead is what kept going wrong. The word ``reference`` is unavoidable in this
    domain (reference genome, reference allele, reference atlas, reference-transferred labels), and
    the phrase list below treats it as a literature signal; a DE step "…to guide biological
    interpretation" trips the list the same way. Both were real production failures. So: a step that
    declares a tool IS that tool's kind of step, full stop; the phrase list is the fallback for a
    step that names no tool at all, which is the only case where prose is the only evidence there is.
    """
    declared = _declared_tools(step)
    if declared:
        return bool({canonical_tool_name(name) for name in declared} & _LITERATURE_TOOLS)
    # No tool named: judge the step by its HEADLINE (the bold title, else the first clause), not by
    # every word of its body. A tool-less analysis step routinely MENTIONS literature or a reference
    # in passing — "Reconcile transferred and original annotations … reference taxonomy …" and
    # "Reconcile evidence and generate the final report … literature context …" were both sent to
    # the literature path in run 3c5fbc8608a7 (rounds 12, 24) — while a real literature step says so
    # in its title ("Ground provisional mechanisms in literature"). And in the headline a singular
    # "reference" is an analysis word (reference atlas / genome / labels), so only the plural counts.
    headline = _step_headline(step).replace("_", " ")
    return bool(_LITERATURE_HEADLINE_RE.search(headline))


_LITERATURE_HEADLINE_RE = re.compile(
    r"\b(literature|references(?!\s*[=:])|citations?|published papers?|paper search|pubmed|europe\s*pmc)\b"
    r"|\b(?:published|biomedical|biological|scientific|literature)\s+background\b"
    r"|\bliterature\s+context\b|\bbiological\s+context\b|\bbiological\s+interpretation\b",
    re.I,
)


def _step_headline(step: str) -> str:
    """A step's title: the leading ``**bold**`` span the planner writes, else its first clause."""
    text = (step or "").strip()
    m = re.match(r"\*\*(.+?)\*\*", text)
    if m:
        return m.group(1)
    return re.split(r"(?<=[.;:!?])\s|\s[\u2014\u2013]\s|\s-\s", text, maxsplit=1)[0]

#judging whether user is asking for literature
def _requests_literature(*texts: str | None) -> bool:
    return _literature_wish(*texts) is True


# What a sentence SAYS about literature, not only whether it names it. A mention used to count as
# a request whatever the sentence said, so in plan review "also discard the literature" put back
# the step it asked to remove (prod run 1e887ed2bf8e). Chinese is read too: users type it.
_LITERATURE_ZH_RE = re.compile(r"文献|引文|引用")
_WISH_CLAUSE_SPLIT_RE = re.compile(r"[,;.!?，；。！？\n]+|\bbut\b|\bthen\b|但是?|然后|另外")
_NEGATION = (r"(?:(?:do|does|did|should|would|could|will|can|must|need)\s+)?not|\w+n['’]t|never"
             r"|no\s+need\s+to")
# The verbs that ask for a step: those of ``_ADD_INTENT_RE``, and the ones that keep it or ask for
# what it produces ("give me references", "ground it in the literature").
_ASK_VERBS = (r"add|insert|include|append|introduce|keep|restore|cite|ground|search|find|review"
              r"|provide|give|attach|want|need")
_REMOVAL_CUE_RE = re.compile(
    # A negation takes its verb with it ("don't add", "no need to include"), so that verb is not
    # then read as a request of its own. Not across "and": "don't skip QC and cite the literature".
    r"\b(?:" + _NEGATION + r")\s+(?:(?!and\b|or\b)\w+\s+){0,4}?"
    r"(?:" + _ASK_VERBS + r"|run|do|plan|use|perform)\b"
    r"|\b(?:" + _NEGATION + r"|remov(?:e|es|ed|ing)|delet(?:e|es|ed|ing)|drop(?:s|ped|ping)?"
    r"|discard(?:s|ed|ing)?|skip(?:s|ped|ping)?|omit(?:s|ted|ting)?|exclud(?:e|es|ed|ing)"
    r"|scrap|ditch|get\s+rid\s+of|take\s+out|leave\s+out|cut|without|no\s+need|no|instead\s+of"
    r"|rather\s+than)\b"
    r"|不(?:再)?(?:需要|要|用|做|加|跑|含|包括|保留|搜|查|进行|执行|必)|无需|无须|没必要"
    r"|别(?:做|跑|加|搜|查|要|用)|去掉|去除|删|移除|取消|跳过|省略|拿掉|砍掉",
    re.I)
_KEEP_CUE_RE = re.compile(
    r"\b(?:" + _ASK_VERBS + r"|also\s+run|also\s+do|put\s+back|bring\s+back)\b"
    r"|加上|加一|增加|新增|添加|再跑|也跑|保留|加入|加回|补上|恢复",
    re.I)
# The removal said AFTER the thing it removes: "the literature step is not needed", "文献去掉".
_REMOVAL_AFTER_RE = re.compile(
    r"(?:\bnot|\bno\s+longer|n['’]t)\s+(?:\w+\s+)?(?:needed|necessary|required|wanted|relevant|useful)\b"
    r"|\bunnecessary\b"
    r"|\b(?:can|could|should|may|must)\s+(?:go|be\s+(?:removed|dropped|deleted|skipped|omitted|"
    r"discarded|excluded|cut))\b"
    r"|^\W*(?:steps?|search|part|section|review|grounding)?\W*(?:too\W+|also\W+)?"
    r"(?:remove|delete|drop|discard|skip|omit|out|off|gone|no)\b"
    r"|不(?:再)?(?:需要|要|用|做|加|跑|保留|搜|查|必)|无需|无须|没必要|别(?:做|跑|加|搜|查|要)"
    r"|去掉|去除|删|移除|取消|跳过|省略|拿掉|砍掉|算了|免了",
    re.I)
# A plan-review note that asks for something to go: a removal, or a narrowing / replacing /
# shortening of the plan ("only keep step 1", "replace step 3", "make it simpler", "只保留第一步").
_PLAN_CUT_RE = re.compile(
    _REMOVAL_CUE_RE.pattern
    + r"|\b(?:only|just|nothing\s+(?:else|more)|replac(?:e|es|ed|ing)|instead|swap(?:s|ped|ping)?"
      r"|simpl(?:er|ify|ified)|short(?:er|en|ened)|fewer|less|reduc(?:e|es|ed|ing)|trim(?:med)?"
      r"|minimal|merg(?:e|es|ed|ing))\b"
      r"|只|仅|换成|改成|替换|简化|精简|减少|缩短|合并",
    re.I)


def _literature_wish(*texts: str | None) -> "bool | None":
    """What the user's own words LAST said about literature: ``True`` asked for it, ``False`` asked
    for none, ``None`` never mentioned it. ``texts`` are read in order (the question, then the
    review notes), clause by clause, so a later sentence overrules an earlier one.

    Within a clause the cue nearest BEFORE the mention decides ("remove GSEA and add a literature
    step" asks for one; "don't add references" does not); with no cue before it, a removal said
    after it does ("文献步骤不要了"); a plain mention is a request. When the two readings are
    close this leans to ``False``: not forcing a step costs the user one more message, forcing one
    they declined is the failure this replaces."""
    wish: "bool | None" = None
    for text in texts:
        for clause in _WISH_CLAUSE_SPLIT_RE.split(text or ""):
            mention = _REQUESTS_LITERATURE_RE.search(clause) or _LITERATURE_ZH_RE.search(clause)
            if not mention:
                continue
            before = clause[:mention.start()]
            cut = max((m.end() for m in _REMOVAL_CUE_RE.finditer(before)), default=-1)
            keep = max((m.start() for m in _KEEP_CUE_RE.finditer(before)), default=-1)
            if cut >= 0 or keep >= 0:
                # A keep cue AFTER the last removal cue. Strictly after: Chinese writes the
                # negation against its verb ("不要保留文献"), which is one removal, not a keep.
                wish = keep > cut
            else:
                wish = not _REMOVAL_AFTER_RE.search(clause[mention.end():])
    return wish


def _literature_step_wanted(agenda: list[str], *, question: str, feedback: str,
                            prior_agenda: "list[str] | None", latest_feedback: str) -> bool:
    """Should the guard make sure ``agenda`` has a literature step?

    A first draft follows the user's words. A revision is judged against the plan the user just
    reviewed, because that plan is what their note is about: a literature step it did not have is
    added only when the note asks for one, and one it had is put back only when the note asked
    for nothing to go. "Remove step 2", "only keep 1 … discard all other step" and "只保留第一步"
    name no literature, and the redraft dropping the literature step is the user's request, not a
    slip to repair."""
    said = _literature_wish(question, feedback)
    note = (latest_feedback or feedback or "").strip()
    if prior_agenda is None or not note:
        return said is True
    if any(_is_literature_step(s) for s in agenda):
        return said is True              # planned already; only duplicates are left to collapse
    if any(_is_literature_step(s) for s in prior_agenda):
        return said is not False and not _PLAN_CUT_RE.search(note)
    return _literature_wish(note) is True

#if the 5 steps are full kick out ... in priority
# obs columns that carry an EXISTING cell-type annotation (as opposed to de-novo cluster labels
# like leiden/louvain, or non-biological metadata like donor/age/sex). When one is present the PI
# should ground marker/enrichment analysis in it, not in raw cluster numbers.
_CELLTYPE_COL_RE = re.compile(
    r"(?i)^(cell[_.]?type|major[_.]?class|sub[_.]?class|cell[_.]?class|cell[_.]?ontology"
    r"(?:[_.]?class)?|cell[_.]?label|celltype[_.]?annotation|annotation|predicted[_.]?label|"
    r"cell[_.]?identity)s?$"
)


def _looks_like_celltype_col(name: str) -> bool:
    return bool(_CELLTYPE_COL_RE.match(str(name or "").strip()))


# Per-cell QC / doublet bookkeeping. Low-cardinality like a design column and completely unlike one
# in meaning: `DF.classifications` holding the single value "Singlet" says the doublets were already
# removed, not that the study has one arm. Excluded from the replication summary so the columns that
# actually define the experiment are the only ones counted there.
_QC_COL_RE = re.compile(
    r"(?i)^(df[_.]?classification|doublet|scrublet|is[_.]?doublet|pann|percent[_.]?mt|pct[_.]?"
    r"counts?[_.]?mt|n[_.]?count|n[_.]?feature|nuclear[_.]?fraction|qc[_.]?)\w*s?$"
)


def _looks_like_qc_col(name: str) -> bool:
    return bool(_QC_COL_RE.match(str(name or "").strip()))


# A step whose ENTIRE action is to re-read what an earlier step already wrote to disk. It produces
# no new knowledge, and it costs a step: a production plan spent one of six on "Parse the generated
# DE tables to extract and report the exact tabular output", which the DE step had already
# returned. Deliberately much narrower than ``_is_report_busywork`` (which matches a bare "report"
# and would delete the perfectly good "…and report how many cells remain"): BOTH a read-back verb
# AND a reference to previously-produced output are required.
_READBACK_VERB_RE = re.compile(
    r"\b(parse|re-?read|tabulate|collate|transcribe|extract and report|report the exact"
    r"|report exactly|read out)\b", re.I)
_READBACK_TARGET_RE = re.compile(
    r"\b(generated|produced|resulting|existing|saved|written|output|above|previous|prior)\b"
    r"[^.]{0,40}?\b(tables?|results?|csvs?|files?|outputs?|statistics)\b", re.I)


def _is_readback_step(step: str) -> bool:
    s = (step or "").replace("_", " ")
    return bool(_READBACK_VERB_RE.search(s) and _READBACK_TARGET_RE.search(s))


# A pathway/GO/ORA enrichment step (as opposed to a marker-DE step, which is kept for annotation
# validation). Used to deterministically drop enrichment when the dataset has no experimental
# contrast — enriching a known cell type's identity markers is circular ("meaningless enrichment").
_ENRICHMENT_STEP_RE = re.compile(
    r"\b(enrich(?:ment|ed|es|ing)?|over[-\s]?represent(?:ation|ed|s|ing)?|pathway|gene[-\s]?ontology"
    r"|reactome|gsea|gene[-\s]?set[-\s]?enrichment|msigdb|hallmark gene set)\b",
    re.I,
)


# A step that PRODUCES a differential-expression table — what run_enrichment / run_gsea_prerank read.
_DE_PRODUCER_RE = re.compile(
    r"\b(run[_ ]de\b|run[_ ]pseudobulk[_ ]de|rank[_ ]genes[_ ]groups|differential(?:ly)?[- ]"
    r"express|marker genes?|pseudobulk|wilcoxon|deseq2?|DEGs?)\b", re.I)


_ENRICHMENT_TOOLS = frozenset({"run_enrichment", "run_gsea_prerank"})
_DE_PRODUCER_TOOLS = frozenset({"run_de", "run_pseudobulk_de", "run_depth_matched_de"})
# Tools that can do anything: naming one says nothing about what the step is, so its prose decides.
_GENERIC_TOOLS = frozenset({"run_code", "run_shell", "run_in_environment"})


def _is_de_producer_step(step: str) -> bool:
    if _is_literature_step(step):
        return False
    tool = _primary_tool(step)
    if tool and tool not in _GENERIC_TOOLS:
        return canonical_tool_name(tool) in _DE_PRODUCER_TOOLS
    if _is_enrichment_step(step):
        return False
    return bool(_DE_PRODUCER_RE.search((step or "").replace("_", " ")))


def _is_enrichment_step(step: str) -> bool:
    # A literature / biological-interpretation step may MENTION "enriched pathways" without BEING an
    # enrichment-analysis step — never classify those as enrichment, so they survive the no-contrast
    # prune (only the actual ORA/pathway analysis step should be dropped).
    if _is_literature_step(step):
        return False
    # A step that names its tool is that tool's step, whatever its prose says. On 2026-10-03 the
    # prose test called four steps of one plan "enrichment" and dropped them: the marker-DE step (it
    # said what its markers would feed: "pathway"), ORA, GSEA, and the cell-type annotation step
    # ("a label is assigned only when a marker is ENRICHED in the cluster"). With the DE step gone
    # nothing produced a DE table, so the run lost its markers and its labels.
    tool = _primary_tool(step)
    if tool and tool not in _GENERIC_TOOLS:
        return canonical_tool_name(tool) in _ENRICHMENT_TOOLS
    return bool(_ENRICHMENT_STEP_RE.search((step or "").replace("_", " ")))


# Report/packaging busywork the PI is already told never to plan (the tools write the figures and
# tables, the bundle is assembled when the run finishes, and a written report is the plan's own
# ``write_report`` step, which ``_is_report_step`` sets apart first). Re-checked DETERMINISTICALLY for a
# step proposed mid-run by exploration, where no plan-time review will catch it.
_REPORT_BUSYWORK_RE = re.compile(
    r"\b(write|writing|render|rendering|compile|compiling|assemble|assembling|package|packaging"
    r"|bundle|bundling|zip|export|exporting|organi[sz]e|organi[sz]ing)\b[^.]{0,40}"
    r"\b(report|manuscript|document|archive|deliverable|outputs?|figures? and tables?)\b"
    r"|\b(\.docx|\.pdf|\.html|\.zip)\b"
    # PURE-TRANSPORT verbs (export/package/bundle/zip/organize — never write/render, which a real
    # analysis step legitimately does to a summary table) aimed at what the tools already write:
    # "Export all computed DE tables … to the artifacts directory for automatic report assembly"
    # reached a plan card. Everything computed is already in artifacts/; the step is a slot for
    # nothing.
    r"|\b(export|exporting|package|packaging|bundle|bundling|zip|organi[sz]e|organi[sz]ing)\b"
    r"[^.]{0,60}\b(artifacts?(?: director(?:y|ies)| folder)?|tables?|results?|metrics)\b"
    # The step's stated PURPOSE, when its verb is an innocent one. Both branches above key on a
    # verb, and a live Qwen3.6 plan (2026-08-19, run 2) opened with "Generate" — which is also how
    # a real analysis step opens — so nothing fired on:
    #   "**Figures & tables** — Generate the visual summaries required FOR THE REPORT … volcano
    #    plots … heatmaps … enrichment bar plots … for automatic inclusion in the final PDF+DOCX
    #    report."
    # It ran: 8 run_code turns, 54 seconds, and produced two summary CSVs. Every figure it promised
    # was already on disk — `run_de` writes the volcanoes (_volcano) and `run_enrichment` writes the
    # bar plots — so the step re-made what the tools had already made and displaced a real analysis.
    # A purpose clause is the reliable signal: no genuine analysis step exists in order to be put
    # in the report. (The dotted-extension branch above missed it too: "PDF+DOCX" has no dots.)
    r"|\bfor\s+(?:automatic\s+)?inclusion\s+in\s+the\b"
    r"|\b(?:required|needed)\s+for\s+the\s+(?:final\s+)?(?:report|manuscript|deliverable)\b"
    r"|\b(?:pdf|docx|html)\s*[+/&]\s*(?:pdf|docx|html)\b",
    re.I,
)


def _is_report_busywork(step: str) -> bool:
    from ..mcp_bridge import configured_tool_names
    if not (_declared_tools(step) & _GENERIC_TOOLS) and _primary_tool(step) in configured_tool_names():
        return False  # a typed domain output (e.g. Atlas report.md) is an actual tool operation
    return bool(_REPORT_BUSYWORK_RE.search((step or "").replace("_", " ")))


# The written report is a step of its own, planned only when asked for (RCHENLAB/AiScientist#102):
# the request asks for a written report, or a skill the plan uses declares ``writes-report``. The
# Scientist never runs it; the lab hands it to the gateway, which writes the report after the analysis
# (``write_report``, also an MCP tool for a finished run). Without the step a run ends with its answer
# in the chat.
REPORT_STEP = ("**Written report** — Write the methods and results report from the accepted results "
               "with `write_report`: the manuscript with its references and verification statement, as "
               "PDF and DOCX. It runs after the analysis steps.")


def _is_report_step(step: str) -> bool:
    """A step the plan gives to ``write_report`` (it names the tool)."""
    return REPORT_TOOL in _declared_tools(step or "")


# The planner's line for it, beside the catalog's tools.
_REPORT_TOOL_LINE = (
    f"- {REPORT_TOOL}: the run's written report (the methods and results manuscript with its "
    "references and verification statement, as PDF and DOCX), written from the accepted results "
    "after the analysis. Plan it only when the request asks for a written report, as the LAST step.")


# ---------------------------------------------------------------------------
# Resolving what a change request POINTS AT, before anything acts on it.
#
# Two measured failures, opposite in direction and neither of them announced (Ziyao,
# plan_mode_report_v2_5, B-5 and B-6):
#
#   "use resolution 0.5 for the clustering step" — sent to a plan with NO clustering step. Instead
#   of saying so, the redraft INVENTED one, complete with an `n_pcs=30, n_neighbors=15` the user had
#   never mentioned, wedged at position 2, consumed by nothing downstream. Asked the same thing as a
#   QUESTION ("how do I change the resolution?") the system answered correctly that the plan does
#   not cluster — so the two paths held different beliefs about the same plan.
#
#   "drop the enrichment step" — singular — sent to a plan with TWO (`run_enrichment` and
#   `run_gsea_prerank`). Both were deleted. The mirror case, "could you use padj 0.01 instead?" with
#   padj in two steps, changed exactly one. Delete took the maximum reading, modify took the
#   minimum, and neither said a choice had been made.
#
# One rule covers both: work out which steps the request names, and when the answer is "none" or
# "more than one", ask rather than act. Deterministic — a clarify that was not needed costs one
# exchange, while acting on the wrong reading costs the plan.
# Each topic pairs the words a REQUEST uses for it with the test for whether a PLAN STEP is one.
# The two are deliberately different jobs, and conflating them produced false alarms both ways:
# a step reading "…`run_enrichment` on the DE table" MENTIONS differential expression without being
# a DE step, and a user who says "the enrichment step" means either of ORA and GSEA — which is
# exactly why naming one of two is ambiguous. The step-side tests are the predicates the rest of
# the planner already uses, so a topic cannot drift from what the pruning guards believe.
_PLAN_TOPICS: "dict[str, tuple[re.Pattern[str], Callable[[str], bool]]]" = {
    "clustering": (
        re.compile(r"\b(cluster\w*|leiden|louvain|resolution)\b", re.I),
        lambda s: bool(_CLUSTERING_STEP_RE.search(s.replace("_", " ")))),
    "pathway enrichment": (
        re.compile(r"\b(enrichment|enrich\w*|ora|over.?representation|go terms?)\b", re.I),
        _is_enrichment_step),
    "GSEA": (
        re.compile(r"\b(gsea|prerank|pre.?ranked)\b", re.I),
        lambda s: bool(re.search(r"\b(gsea|prerank)\b", s.replace("_", " "), re.I))),
    "QC": (
        re.compile(r"\b(qc|quality control|percent\.?mt|mitochondrial)\b", re.I),
        # "CellQC" / `run_cellqc` is a QC step too: without it, "raise the mitochondrial limit to 20 and
        # cluster at 0.8" on a CellQC plan was told the plan has no QC step.
        lambda s: bool(re.search(r"\b(qc|quality control|scanpy qc|cellqc)\b", s.replace("_", " "), re.I))),
    "differential expression": (
        re.compile(r"\b(differential expression|de|wilcoxon|rank.?genes)\b", re.I),
        _is_de_producer_step),
    "pseudobulk": (
        re.compile(r"\b(pseudobulk|pseudo.?bulk|deseq2?|edger)\b", re.I),
        lambda s: bool(re.search(r"\b(pseudobulk|deseq2?)\b", s.replace("_", " "), re.I))),
    "doublet detection": (
        re.compile(r"\b(doublets?|scrublet)\b", re.I),
        lambda s: bool(re.search(r"\b(doublets?|scrublet)\b", s.replace("_", " "), re.I))),
    "composition": (
        re.compile(r"\b(composition|proportions?|abundance)\b", re.I),
        lambda s: bool(re.search(r"\b(composition|proportions?|abundance)\b", s.replace("_", " "), re.I))),
    "batch integration": (
        re.compile(r"\b(integration|batch correct\w*|harmony|combat)\b", re.I),
        lambda s: bool(re.search(r"\b(integration|batch correct\w*|harmony|combat)\b", s.replace("_", " "), re.I))),
    "literature": (
        re.compile(r"\b(literature|papers?|citations?|pubmed)\b", re.I),
        _is_literature_step),
}

# An ADD is allowed to name something the plan does not have yet — that is what adding means. Only
# requests that point at an EXISTING step are checked for existence.
_ADD_INTENT_RE = re.compile(
    r"\b(add|insert|include|append|introduce|also run|also do|also add)\b"
    r"|加上|加一|增加|新增|添加|再跑|也跑", re.I)
def _is_definite_reference(request: str, word: str) -> bool:
    """True when ``word`` appears in ``request`` as a reference to an EXISTING step.

    A bare mention is not enough: "compare the arms with a wilcoxon test" names a method, while
    "the wilcoxon step" names a step the speaker believes is already in the plan. Only the second
    is checked for existence — the first is just how a new step gets described."""
    w = re.escape(word)
    return bool(
        # "the clustering step", "that enrichment analysis", "this DE step"
        re.search(rf"\b(?:the|that|this)\s+(?:\w+\s+){{0,3}}{w}\b", request, re.I)
        # "clustering step", "enrichment step" — the noun "step" makes it definite on its own
        or re.search(rf"\b{w}(?:\s+\w+){{0,2}}\s+步骤?\b|\b{w}(?:\s+\w+){{0,2}}\s+step\b", request, re.I)
        # "for the clustering", "in the DE", "from the enrichment"
        or re.search(rf"\b(?:for|in|from|of|on)\s+the\s+(?:\w+\s+){{0,3}}{w}\b", request, re.I)
    )


def _topics_named(request: str) -> "list[str]":
    """Plan topics the request refers to DEFINITELY (see :func:`_is_definite_reference`)."""
    named: list[str] = []
    for topic, (words, _) in _PLAN_TOPICS.items():
        if any(_is_definite_reference(request, m.group(0)) for m in words.finditer(request or "")):
            named.append(topic)
    return named


def resolve_plan_reference(request: str, agenda: "list[str]") -> "tuple[str, str, list[int]]":
    """``(verdict, topic, 1-based step numbers)`` for what a change request points at.

    ``verdict`` is ``"ok"`` (act on it), ``"missing"`` (the plan has no such step) or
    ``"ambiguous"`` (it has more than one, and the request named only one). ``"ok"`` is also the
    answer for a request that names nothing in particular — most requests — so this only ever
    interrupts when it has something specific to say."""
    if not request or not agenda or _ADD_INTENT_RE.search(request):
        return "ok", "", []
    for topic in _topics_named(request):
        is_step = _PLAN_TOPICS[topic][1]
        hits = [i + 1 for i, s in enumerate(agenda) if is_step(str(s))]
        if not hits:
            return "missing", topic, []
        if len(hits) > 1:
            return "ambiguous", topic, hits
    return "ok", "", []


# A step that clusters the cells de-novo (Leiden/Louvain). Used to detect the "analyze by existing
# labels vs re-cluster de-novo" methodological fork when the dataset already carries cell-type labels.
_CLUSTERING_STEP_RE = re.compile(r"\b(clusters?|clustering|clustered|leiden|louvain)\b", re.I)
# A step that says NOT to cluster is the plan deciding the fork, not a clustering step. Read as one,
# "reuse them and do not re-cluster" (the `\b` before "cluster" matches after the hyphen) raised
# "use the labels or re-cluster?" about the very step that had answered it (run f107bcf7b660).
_NEGATED_CLUSTERING_RE = re.compile(
    r"\b(?:do\s+not|don'?t|does\s+not|never|no|not|without|rather\s+than|instead\s+of|avoid(?:ing)?|"
    r"skip(?:ping)?)\s+(?:\w+\s+){0,3}?(?:de[-\s]?novo\s+)?(?:re-?)?"
    r"(?:clusters?|clustering|clustered|leiden|louvain)"
    r"(?:\s+(?:clusters?|clustering|clustered|leiden|louvain))*\b", re.I)


def _clusters_de_novo(step: str) -> bool:
    """True when a step CLUSTERS the cells de-novo, and not merely when it says not to."""
    text = _NEGATED_CLUSTERING_RE.sub(" ", (step or "").replace("_", " "))
    return bool(_CLUSTERING_STEP_RE.search(text))


def _plan_has_clustering(agenda: "list[str]") -> bool:
    return any(_clusters_de_novo(str(s)) for s in agenda)


def _is_label_fork(options: "Any") -> bool:
    """Whether a decision's options are the 'existing labels vs re-cluster' fork."""
    text = " ".join(str(o) for o in (options or ())).lower()
    return "label" in text and "cluster" in text


_CLUSTER_COLS = ("leiden", "louvain", "cluster", "clusters", "seurat_clusters")


def handed_on_note(dr: "dict[str, Any] | None") -> str:
    """The planning note for a dataset an earlier run handed on (profile key ``handed_on``), or ''.

    Such a file is already filtered and log-normalised and carries that run's clusters and labels.
    Nothing said so, and nothing about the file looks unusual, so a plan for it opened with QC and
    normalisation again — on log values, with no symptom."""
    rec = dr.get("handed_on") if isinstance(dr, dict) else None
    if not isinstance(rec, dict) or not rec.get("processed"):
        return ""
    cats = dr.get("obs_categoricals") if isinstance(dr.get("obs_categoricals"), dict) else {}
    cols = [*cats, *[str(k) for k in (dr.get("obs_keys") or [])]]
    clusters = next((c for c in cols if c in _CLUSTER_COLS), None)
    labels = next((c for c in cols if _looks_like_celltype_col(c)), None)
    counts = str(rec.get("counts_layer") or "integer counts")
    has = ["quality-controlled and log-normalised expression (do not filter or normalise again)"]
    if "counts" in (rec.get("layers") or []):
        has.append(f"the counts in layers['counts'] ({counts})")
    if clusters:
        has.append(f"clusters in `{clusters}`")
    if labels:
        has.append(f"cell-type labels in `{labels}`")
    if "X_umap" in (rec.get("obsm") or []):
        has.append("a UMAP embedding")
    run = str(rec.get("run_id") or "").strip()
    return ("⚠ THIS DATASET IS THE PROCESSED OUTPUT OF AN EARLIER RUN"
            + (f" (run {run})" if run else "") + ". It carries: " + "; ".join(has) + ". It is "
            "already in place as this run's checkpoint, so `run_de`, `run_marker_annotation`, "
            "`run_composition` and `run_clustering` read it directly. Do NOT plan QC, doublet "
            "removal, normalisation or a matrix-provenance step (QC cannot be redone from this "
            "file: it holds only the cells the earlier QC kept), and do not plan clustering or "
            "annotation again unless the request asks to redo them. Tables and figures of the "
            "earlier run are in its own report; a step that needs a table (enrichment needs a DE "
            "table) must have the step that computes it in THIS plan.")


def _contrast_columns(dr: "dict[str, Any] | None") -> "list[tuple[str, list[str]]]":
    """The obs columns the mode rule reads as an experimental contrast, with up to three of their
    values: a non-QC, non-label column with 2-3 levels."""
    if not isinstance(dr, dict):
        return []
    cats = dr.get("obs_categoricals") or {}
    if not isinstance(cats, dict):
        return []
    return [(str(c), [str(v) for v in (info.get("values") or [])][:3])
            for c, info in cats.items()
            if isinstance(info, dict) and isinstance(info.get("n"), int) and 2 <= info["n"] <= 3
            and not _looks_like_celltype_col(c) and not _looks_like_qc_col(c)]


def _dataset_mode_rule(dr: "dict[str, Any] | None") -> str:
    """``"team"`` / ``"single"`` when the dataset profile settles it, else ``""``.

    team   — an experimental CONTRAST (a non-QC, non-label obs column with 2-3 levels) is present:
             the dataset can carry a comparative study, and comparative single-cell design is where
             a statistician, a biologist and a data expert disagree productively (unit of
             replication, composition confounding, stratification). Whether THIS request asks for a
             comparison is :meth:`ResearchLab._route_mode`'s question.
    single — cell-type labels present but NO contrast: a descriptive atlas; nothing to debate.
    ""     — no profile, or a profile with neither (a VCF, a folder, an unlabelled object): defer.
    """
    if not isinstance(dr, dict):
        return ""
    cats = dr.get("obs_categoricals") or {}
    if not isinstance(cats, dict) or not cats:
        return ""
    has_labels = any(_looks_like_celltype_col(c) for c in cats)
    if _contrast_columns(dr):
        return "team"
    if has_labels:
        return "single"
    return ""


# A comparison asked for in so many words. On a dataset with a contrast it keeps the design meeting
# without asking the model; a request without one is asked about (``_COMPARISON_ROUTE_SYSTEM``).
_COMPARISON_CUE = re.compile(
    r"\b(?:compar\w*|versus|vs\.?|contrast\w*|differs?|differed|differen(?:ce|ces|tial|tially)|"
    r"DEGs?|DE|changes?|changed|changing|up-?regulat\w*|down-?regulat\w*|effects?\s+of|"
    r"impact\s+of|loss\s+of|knock-?outs?|mutants?|treat\w*)\b"
    r"|比较|对比|差异|区别|变化|上调|下调|影响|敲除|突变",
    re.IGNORECASE)


def _annotated_without_contrast(dr: "dict[str, Any] | None") -> bool:
    """True when the dataset ALREADY carries a cell-type annotation column AND has NO experimental
    contrast — no non-annotation obs column with 2+ categories (a single sample / condition). In that
    regime there is no differential question, so pathway/GO enrichment on a cell type's own identity
    markers is circular (it just restates the cell type's definition) and should not be planned."""
    if not isinstance(dr, dict):
        return False
    cats = dr.get("obs_categoricals") or {}
    if not isinstance(cats, dict):
        return False
    universe = {**cats, **{k: {} for k in (dr.get("obs_keys") or [])}}
    annotated = any(_looks_like_celltype_col(c) for c in universe)
    has_contrast = any(
        isinstance(info, dict) and isinstance(info.get("n"), int) and info["n"] >= 2
        and not _looks_like_celltype_col(c)
        for c, info in cats.items()
    )
    return annotated and not has_contrast


# The Critic writes its issues in prose, so "did it report a missing analysis, and WHICH one"
# are both text questions. The family matters: a first version of this asked only whether the
# agenda named *any* analysis tool, and every draft it was meant to catch named run_scanpy_qc,
# run_composition and run_enrichment — just not the DE step the Critic was complaining about — so
# the guard never fired once. Resolve the issue to a tool FAMILY and check for that family.
# NB: no trailing \b — the truncated stems ("never comput") must still match "never computed",
# and a \b after "comput" demands a boundary that "computed" does not have. The leading \b stays.
_MISSING_WORDS = re.compile(
    r"\b(missing|absent|lacks?|lacking|no\s+\S+\s+step|no step|never comput|not comput|"
    r"nothing comput|does not comput)", re.I)
# Ordered: the first family whose words appear in the issue is the one the Critic means.
_ANALYSIS_FAMILIES: "tuple[tuple[Any, tuple[str, ...]], ...]" = (
    (re.compile(r"\b(differential expression|\bDE\b|contrast|fold[- ]change|pseudobulk)\b", re.I),
     ("run_de", "run_pseudobulk_de")),
    (re.compile(r"\b(composition|proportion|abundance)\b", re.I), ("run_composition",)),
    (re.compile(r"\b(enrichment|pathway|GSEA|ORA)\b", re.I), ("run_enrichment", "run_gsea_prerank")),
    (re.compile(r"\b(clustering|cluster|annotation|cell[- ]type label)\b", re.I),
     ("run_clustering", "scgpt_annotate")),
    (re.compile(r"\b(quality control|\bQC\b|filtering)\b", re.I), ("run_scanpy_qc", "run_cellqc")),
    (re.compile(r"\b(depth|library size|down-?sampl)\b", re.I), ("run_depth_matched_de",)),
)


def _missing_analysis_family(issues: "list[str]") -> "tuple[str, ...] | None":
    """The tool family a Critic issue says is ABSENT, or None if none does.

    Narrow on purpose: the issue must say something is missing AND name an analysis, so
    "the DE step should also report effect sizes" does not trip it.
    """
    for issue in issues:
        if not _MISSING_WORDS.search(issue):
            continue
        for pattern, tools in _ANALYSIS_FAMILIES:
            if pattern.search(issue):
                return tools
    return None
