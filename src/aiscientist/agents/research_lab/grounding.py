"""Facts a report may state: tool-result facts, evidence pointers, coverage counts, label vocabularies."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from ..marker_claims import check_marker_claims
from ..label_claims import check_label_conflation, dataset_labels, label_glossary, labels_prompt
from ..step_numbers import contrast_arms
from ..research_harness import evidence_pointers

from .models import LabRound, Specialist


def _annotated_cell_types(rounds: "list[LabRound]") -> list[str]:
    """The cell-type labels an accepted annotation step assigned (Unassigned left out)."""
    found: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for lab in (res.get("labels") or {}).values() if isinstance(res.get("labels"), dict) else []:
                if lab and lab != "Unassigned" and lab not in found:
                    found.append(str(lab))
    return found


def _tool_classes(rounds: "list[LabRound]") -> "list[str]":
    """The group labels the accepted steps' packaged tools reported results for."""
    classes: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for key in ("top_genes_by_group", "top_terms_by_group"):
                for grp in (res.get(key) or {}):
                    if grp and str(grp) not in classes:
                        classes.append(str(grp))
    return classes


def _class_labels(rounds: "list[LabRound]",
                  dataset_result: "dict[str, Any] | None") -> "tuple[str, list[str]]":
    """``(label column, labels)``: the dataset profile's label column and its levels, then any other
    group a packaged tool reported. A run whose steps are all ``run_code`` has no tool groups, and
    run 74873590ca7b's writer was given no labels at all; its answer called MG "Microglia-associated"
    in a dataset where Microglia is another class."""
    column, labels = dataset_labels(dataset_result)
    return column, list(dict.fromkeys([*labels, *_tool_classes(rounds)]))


def _label_reference(rounds: "list[LabRound]",
                     dataset_result: "dict[str, Any] | None") -> "dict[str, Any] | None":
    """The curated reference that may spell a label out: the one an accepted ``run_marker_annotation``
    step used, else the one the dataset profile's tissue hint names (``tissue_hint`` writes "markers
    in the curated <name> reference"). None when neither applies: then no label is expanded."""
    ref = _marker_reference(rounds)
    if ref is not None:
        return ref
    hint = str((dataset_result or {}).get("tissue_hint") or "") if isinstance(dataset_result, dict) else ""
    m = re.search(r"\bcurated (\S+) reference\b", hint)
    if not m:
        return None
    from ...tools.api import available_references, load_marker_reference
    try:
        return load_marker_reference(m.group(1)) if m.group(1) in available_references() else None
    except (OSError, ValueError):      # an unreadable reference expands nothing; it never costs the report
        return None


def _grounding_vocab(rounds: "list[LabRound]", dataset_result: "dict[str, Any] | None" = None,
                     reference: "dict[str, Any] | None" = None) -> str:
    """A CLOSED vocabulary the report writer must stay within — the exact cell-class labels and the
    exact enriched pathway terms the tools actually produced. Injected into the synthesize prompt so
    the model cannot invent pathways (e.g. an EMT/ECM term that never appeared in the enrichment
    table) or expand a class label into a different cell type (e.g. 'MG' -> 'Müller/ganglion').
    The labels include the dataset profile's label column (``dataset_result``), and a label may be
    spelled out only as the curated ``reference`` spells it (``label_claims.labels_prompt``).
    Empty string when there is nothing to pin (the system prompt still forbids invention)."""
    column, profile_labels = dataset_labels(dataset_result)
    classes = list(dict.fromkeys([*profile_labels, *_tool_classes(rounds)]))
    terms: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result") if isinstance(s.get("result"), dict) else {}
            for lst in (res.get("top_terms_by_group") or {}).values():
                for t in (lst or []):
                    if t and str(t) not in terms:
                        terms.append(str(t))
    if not classes and not terms:
        return ""
    lines = ["Grounding vocabulary — stay STRICTLY within these; introduce nothing outside them:"]
    if classes:
        lines.append(
            "- Cell-class labels present in the data (use verbatim; do NOT rename, merge, split, or "
            "expand a single label into a different or additional cell type): " + ", ".join(classes) + "."
        )
        lines += labels_prompt(column, classes, label_glossary(classes, reference),
                               str((reference or {}).get("tissue") or ""))
    if terms:
        lines.append(
            "- Enriched pathway/term names ACTUALLY found (name enriched pathways ONLY from this list; "
            "a pathway not listed here was NOT enriched — do not present it as a finding): "
            + "; ".join(terms[:40]) + "."
        )
    return "\n".join(lines)


def _methods_performed(rounds: "list[LabRound]") -> str:
    """A CLOSED allowlist of the analyses/tools ACTUALLY executed in accepted steps, so the report's
    Methods cannot describe a technique that never ran (scGPT, multi-omics, trajectory, …) — not even
    as 'planned but failed'. Complements _grounding_vocab (which pins labels/terms). '' when empty."""
    tools: list[str] = []
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            tl = s.get("tool")
            if tl and str(tl) not in tools and str(tl) != "finish":
                tools.append(str(tl))
    if not tools:
        return ""
    return (
        "Tools/analyses ACTUALLY executed (the Methods/Results may describe ONLY analyses among these; "
        "do NOT mention any other tool, model, or technique, and never describe one as planned/attempted/"
        "failed): " + ", ".join(tools) + "."
    )


# The AUTHORITATIVE scalar figures the tools reported — whitelisted keys (+ common variants), pulled
# recursively so nested blocks like ``variant_filters`` are covered.
_FACT_KEYS = frozenset({
    "assembly", "execution_mode", "normalized",
    "n_input_variants", "n_input", "n_kept", "n_dropped_common_af", "n_dropped_off_panel",
    "max_pop_af", "gene_panel_size", "total_variants", "n_pathogenic", "n_pathogenic_clinvar",
    "n_high_priority_rare_deleterious",
    "n_pass", "n_nonpass", "n_filtered", "n_records", "n_snps", "n_indels", "n_multiallelic",
    "n_samples", "ti_tv", "ti_tv_ratio", "het_hom", "het_hom_ratio", "call_rate",
    "n_cells", "n_genes", "n_clusters", "n_de_genes", "resolution",
})
_FACT_DIST_KEYS = ("by_impact", "by_consequence", "by_clinical_significance",
                   "impact_distribution", "consequence_distribution", "clinical_significance_distribution")


def _collect_facts(rounds: "list[LabRound]") -> "tuple[dict[str, Any], dict[str, dict]]":
    """Recursively pull the whitelisted authoritative figures (scalars) + category distributions from the
    ACCEPTED step results (nested blocks like ``variant_filters`` included). Shared by the synthesize
    grounding block and the post-report fact-check."""
    facts: dict[str, Any] = {}
    dists: dict[str, dict] = {}

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(obj, dict):
            return
        for k, v in obj.items():
            if k in _FACT_KEYS and isinstance(v, (int, float, str, bool)):
                facts[str(k)] = v                     # a later accepted step wins on a key clash
            elif k in _FACT_DIST_KEYS and isinstance(v, dict) and v:
                dists[str(k)] = v
            elif isinstance(v, dict):
                _walk(v, depth + 1)

    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            _walk(s.get("result"))
    return facts, dists


def resolve_evidence(paths: "list[str]", workspace: "Path | None") -> "tuple[list[str], list[str]]":
    """Split claimed artifact paths into (present on disk, missing). Deterministic — no model.

    ``evidence_pointers`` is called in four places and NOTHING ever checked that the files it names
    exist. So a tool could report a figure it never wrote and the Critic would ground its verdict on
    it: ``run_de`` returned ``figures/rank_genes_groups_leiden_de.png`` hardcoded, which resolves to
    nothing whenever the DE was grouped by anything other than ``leiden`` — a dangling evidence
    pointer that survived in production until it was read by hand.

    This is the cheapest member of the class of defects that matter here: the tool SUCCEEDS, the
    system contract holds, and only an independent check against the filesystem reveals it. Reports
    rather than judges — see ``_critic`` for why the verdict is not flipped on this alone."""
    if workspace is None:
        return list(paths), []
    art = Path(workspace) / "artifacts"
    present, missing = [], []
    for p in paths:
        rel = str(p).strip()
        if not rel:
            continue
        if Path(rel).is_absolute():
            # An absolute path is checked HERE, on the gateway host — but the HPC3 shell tools
            # (list_dir / read_text / stat_path / find_files) return the dfs3b paths they looked
            # at, and analysis jobs run there too. Those paths never exist on the eyeserver, so
            # every step that used the shell was told ALL its evidence was missing (a production
            # ORA step scored 0.0 for it while its tables sat right there in artifacts/). Map a
            # remote artifact to its local mirror by its artifacts/-relative suffix; treat any
            # other remote path as unverifiable from here — neither present nor missing.
            if "/artifacts/" in rel:
                mirror = art / rel.split("/artifacts/", 1)[1]
                (present if mirror.exists() else missing).append(rel)
                continue
            if Path(rel).exists():
                present.append(rel)
            elif rel.startswith(("/dfs3b/", "/pub/", "/data/homezvol", "/tmp/", "/scratch/")):
                continue                              # lives on the cluster; not checkable here
            else:
                missing.append(rel)
            continue
        (present if (art / rel).exists() else missing).append(rel)
    return present, missing


def _uncovered_groups(rounds: "list[LabRound]") -> "dict[str, str]":
    """Groups a step REFUSED to analyse, and why — e.g. cell types with too few cells in one arm.

    This is a RESULT, not a degradation: "we could not test this cell type" is part of what the
    study found, and a report that omits it implies a coverage it does not have. The DEG protocol
    requires stating it, but nothing carried it to the writer: ``_collect_facts`` only recurses into
    dicts, and both producers emit a LIST (``run_de``) or a flat dict (``run_pseudobulk_de``) of
    skips, so on the lab's real Ddx41 data 5 of 12 cell types went untested and the grounding block
    never mentioned it.

    Handles both shapes. Returns {group: reason}, empty when everything was covered."""
    out: dict[str, str] = {}
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result")
            if not isinstance(res, dict):
                continue
            skipped = res.get("skipped_groups")
            if isinstance(skipped, dict):                       # run_pseudobulk_de: {label: reason}
                for label, reason in skipped.items():
                    out[str(label)] = str(reason)[:200]
            elif isinstance(skipped, list):                     # run_de: [{group, n_*, reason}]
                # Name the ARMS, not the roles. "condition=7, reference=27" leaves the writer to work
                # out which arm is which; run 8847d521ba32's report has three of its five unequal
                # skipped pairs backwards.
                arms = contrast_arms(res)
                for item in skipped:
                    if not isinstance(item, dict) or not item.get("group"):
                        continue
                    if arms and "n_condition" in item and "n_reference" in item:
                        counts = (f"cells after QC: {arms[0]}={item['n_condition']}, "
                                  f"{arms[1]}={item['n_reference']}")
                    else:
                        counts = ", ".join(
                            f"{k.replace('n_', '')}={item[k]}" for k in ("n_condition", "n_reference")
                            if k in item)
                    reason = str(item.get("reason", "not analysed"))[:160]
                    out[str(item["group"])] = f"{reason}{f' ({counts})' if counts else ''}"
    return out


def _tested_counts(rounds: "list[LabRound]") -> "dict[str, dict[str, int]]":
    """Cells per group and arm AFTER QC, as the tools counted them: ``cells_by_group_and_arm`` (run_de,
    run_composition) and a contrast's ``skipped_groups``. These are the numbers a cell floor was
    applied to. The dataset profile counts the uploaded file before QC; in run f3731e0b7136 QC removed
    30% of nuclei, so Endothelial went from 7 / 27 in the file to 5 / 22 in the analysis."""
    out: dict[str, dict[str, int]] = {}
    for r in rounds:
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            res = s.get("result")
            if not isinstance(res, dict):
                continue
            table = res.get("cells_by_group_and_arm")
            if isinstance(table, dict):
                for group, per in table.items():
                    if isinstance(per, dict) and per:
                        out[str(group)] = {str(a): int(n) for a, n in per.items()
                                           if isinstance(n, (int, float)) and not isinstance(n, bool)}
            arms = contrast_arms(res)
            skipped = res.get("skipped_groups")
            if arms and isinstance(skipped, list):
                for item in skipped:
                    if (isinstance(item, dict) and item.get("group")
                            and isinstance(item.get("n_condition"), int)
                            and isinstance(item.get("n_reference"), int)):
                        out[str(item["group"])] = {arms[0]: item["n_condition"],
                                                   arms[1]: item["n_reference"]}
    return out


def pre_qc_counts(dataset_result: "dict[str, Any] | None") -> "dict[str, dict[str, int]]":
    """Cells per label and arm IN THE UPLOADED FILE (the dataset profile), before any QC."""
    dba = (dataset_result or {}).get("design_by_arm") if isinstance(dataset_result, dict) else None
    table = (dba or {}).get("cells_by_label_and_arm") if isinstance(dba, dict) else None
    return {str(k): dict(v) for k, v in (table or {}).items() if isinstance(v, dict)}


# A statement about coverage — which groups were tested, and why some were not. The only kind of
# sentence correct_coverage_counts may touch: elsewhere a report may rightly quote the uploaded file.
_COVERAGE_RE = re.compile(
    r"floor|below|too few|fewer than|not covered|not analy[sz]ed|not tested|untested|excluded|"
    r"skipped|insufficient|underpowered|could not be (?:tested|analy[sz]ed)|no primary", re.I)
_PRE_QC_RE = re.compile(r"before QC|pre-?QC|prior to QC|uploaded|raw (?:file|data|object|counts?)", re.I)
_COUNT_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3})*(?![\w.,]*\d)")


def _swap_to_tested(window: str, pre: "dict[str, int]", tested: "dict[str, int]") -> "tuple[str, bool]":
    """Replace the uploaded-file counts in ``window`` with the counts the analysis tested. Only a
    number EQUAL to the group's pre-QC count for its arm is touched, so a correct number never is."""
    arms = [a for a in tested if a in pre and pre[a] != tested[a]]
    if not arms:
        return window, False
    edits: dict[tuple[int, int], str] = {}
    for a in [a for a in tested if a in pre]:
        esc = re.escape(a)
        for pat in (rf"(?<![\w.,])(\d{{1,3}}(?:,\d{{3}})*)\s*{esc}\b",                 # "7 DDX41"
                    rf"\b{esc}\b(?:\s+arm)?\s*(?:=|:|\()?\s*(\d{{1,3}}(?:,\d{{3}})*)(?![\w.,]*\d)"):  # "WT=27", "WT arm (24"
            for m in re.finditer(pat, window):
                n = int(m.group(1).replace(",", ""))
                if n == pre[a] and pre[a] != tested[a]:
                    edits[m.span(1)] = str(tested[a])
    if not edits and len(tested) == 2 and all(a in pre for a in tested):
        # A bare pair, "HC (5 / 11)": the two arms in whichever order the pre-QC values sit.
        nums = list(_COUNT_RE.finditer(window))[:2]
        if len(nums) == 2:
            got = [int(m.group(0).replace(",", "")) for m in nums]
            a, b = list(tested)
            for first, second in ((a, b), (b, a)):
                if got == [pre[first], pre[second]] and [pre[first], pre[second]] != [tested[first], tested[second]]:
                    edits = {nums[0].span(): str(tested[first]), nums[1].span(): str(tested[second])}
                    break
    if not edits:
        return window, False
    out = window
    for (s, e), new in sorted(edits.items(), reverse=True):
        out = out[:s] + new + out[e:]
    return out, out != window


def correct_coverage_counts(md: str, uncovered: "dict[str, str] | Any",
                            tested: "dict[str, dict[str, int]]",
                            pre: "dict[str, dict[str, int]]") -> "tuple[str, list[str]]":
    """Put the tested (post-QC) counts back into the report's coverage statements.

    Run f3731e0b7136's manuscript said "Endothelial (7 DDX41 / 27 WT) … had both arms below the
    30-cell floor"; the analysis had tested 5 / 22. 7 / 27 were the uploaded file's counts, which the
    writers had been handed as authoritative. For each group the analysis refused, a count next to its
    name in a coverage sentence that equals its pre-QC count (and not its tested one) is replaced. A
    sentence that says it is describing the file before QC is left alone. Returns (md, issues)."""
    issues: list[str] = []
    groups = [g for g in (uncovered or {}) if g in tested and g in pre
              and any(pre[g].get(a) != n for a, n in tested[g].items() if a in pre[g])]
    if not groups or not md:
        return md, issues
    names = sorted({*map(str, pre), *map(str, tested)}, key=len, reverse=True)
    any_group = re.compile("|".join(rf"\b{re.escape(n)}\b" for n in names))
    for g in groups:
        pos, fixed = 0, False
        pattern = re.compile(rf"\b{re.escape(g)}\b")
        while True:
            m = pattern.search(md, pos)
            if not m:
                break
            s_start = max(md.rfind(". ", 0, m.start()), md.rfind("\n", 0, m.start())) + 1
            s_end_candidates = [i for i in (md.find(". ", m.end()), md.find("\n", m.end())) if i != -1]
            s_end = min(s_end_candidates) if s_end_candidates else len(md)
            sentence = md[s_start:s_end]
            end = min(s_end, m.end() + 100)
            nxt = any_group.search(md, m.end(), end)
            if nxt:
                end = nxt.start()
            if _COVERAGE_RE.search(sentence) and not _PRE_QC_RE.search(sentence):
                new, changed = _swap_to_tested(md[m.end():end], pre[g], tested[g])
                if changed:
                    md = md[:m.end()] + new + md[end:]
                    end = m.end() + len(new)
                    fixed = True
            pos = end
        if fixed:
            issues.append(
                f"coverage counts: the report gave '{g}' the uploaded file's counts "
                f"({', '.join(f'{a} {n}' for a, n in pre[g].items())}); the analysis tested "
                f"{', '.join(f'{a} {n}' for a, n in tested[g].items())} after QC — corrected.")
    return md, issues


def _grounding_facts(rounds: "list[LabRound]") -> str:
    """A CLOSED set of the AUTHORITATIVE figures the tools reported — genome assembly, execution mode,
    PASS/non-PASS + variant/cell counts, thresholds, ratios, and category distributions — injected into
    the synthesize prompt so the report cannot invent a number, echo the plan's ASSUMED assembly over the
    one actually used, or write "0 non-PASS" when the result says otherwise. "" when nothing quantitative
    was produced. Complements _grounding_vocab (labels/terms) + _methods_performed (tools)."""
    facts, dists = _collect_facts(rounds)
    uncovered = _uncovered_groups(rounds)
    if not facts and not dists and not uncovered:
        return ""
    lines = [
        "Authoritative figures — every number, count, ratio, threshold, genome assembly, and execution "
        "mode in the report MUST match these EXACTLY. Do NOT state a figure that is not here, do NOT round "
        "or infer one, and do NOT echo the plan's assumed value (e.g. its genome assembly) over these. "
        "Never write 'all PASS' or '0 non-PASS' unless a non-PASS count of 0 appears here:",
    ]
    lines += [f"- {k} = {v}" for k, v in facts.items()]
    for k, d in dists.items():
        pairs = ", ".join(f"{kk}: {vv}" for kk, vv in list(d.items())[:20])
        lines.append(f"- {k}: {pairs}")
    if uncovered:
        # Coverage is a RESULT. Without this the writer sees results for 7 cell types, has no way
        # to know 5 more exist and were refused, and writes a manuscript that reads as though the
        # analysis covered the dataset.
        lines.append(
            "NOT ANALYSED — these groups were REFUSED by the analysis and have NO results. The "
            "report MUST state that they were not covered and why, MUST NOT present any finding "
            "for them, and MUST NOT describe the analysis as covering all groups. The cell counts "
            "in brackets are AFTER QC, the numbers the cell floor was applied to; quote THESE. The "
            "dataset profile and the plan count the uploaded file before QC, so their per-group "
            "numbers are larger and are not what the analysis tested:")
        lines += [f"- {g}: {why}" for g, why in list(uncovered.items())[:20]]
    return "\n".join(lines)


_ASSEMBLY_CANON = {"grch38": "GRCh38", "hg38": "GRCh38", "grch37": "GRCh37", "hg19": "GRCh37"}


def verify_report_facts(report_md: str, facts: "dict[str, Any]",
                        uncovered: "dict[str, str] | None" = None, *,
                        tested_counts: "dict[str, dict[str, int]] | None" = None,
                        pre_counts: "dict[str, dict[str, int]] | None" = None,
                        marker_reference: "dict[str, Any] | None" = None,
                        class_labels: "list[str] | None" = None, label_column: str = "",
                        label_reference: "dict[str, Any] | None" = None) -> "tuple[str, list[str]]":
    """The GUARANTEE layer: a deterministic post-generation fact-check of the manuscript against the
    authoritative figures, so a fabrication that slipped past the grounding prompt is caught regardless of
    whether the LLM obeyed it. Corrects two UNAMBIGUOUS cases in place — a wrong genome assembly (the
    report named a build other than the one actually used), and a '0/no/zero non-PASS' claim when the QC
    reported non-PASS records — and returns ``(corrected_md, issues)``. It only swaps those tokens/numbers;
    the prose edits are removing a sentence that calls a gene a marker of a lineage the run's curated
    ``marker_reference`` gives to another (``marker_claims``), and removing another class's name from a
    class label (``label_claims``: "Microglia-associated (MG)" -> "MG" when Microglia is one of
    ``class_labels``). ``issues`` are for the technical-report Diagnostics."""
    import re
    md = report_md or ""
    issues: list[str] = []

    # 1. Genome assembly — the report must name the build ACTUALLY used, not the plan's assumption.
    auth = _ASSEMBLY_CANON.get(str(facts.get("assembly") or "").strip().lower())
    if auth:
        for token in ("GRCh38", "GRCh37", "hg38", "hg19"):
            if _ASSEMBLY_CANON.get(token.lower()) == auth:
                continue                                  # this token already IS the right build
            pat = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
            hits = pat.findall(md)
            if hits:
                md = pat.sub(auth, md)
                issues.append(f"assembly: report stated '{token}' but the annotation used {auth} — "
                              f"corrected {len(hits)} mention(s).")

    # 2. PASS / non-PASS — a '0/no/zero non-PASS' claim when non-PASS records exist is a fabrication.
    nonpass = facts.get("n_nonpass")
    if nonpass is None:
        nonpass = facts.get("n_filtered")
    if isinstance(nonpass, (int, float)) and not isinstance(nonpass, bool) and nonpass > 0:
        pat = re.compile(r"\b(?:0|zero|no)\s+non-?PASS\b", re.IGNORECASE)
        if pat.search(md):
            md = pat.sub(f"{int(nonpass):,} non-PASS", md)
            issues.append(f"pass-split: report claimed 0/no non-PASS but {int(nonpass):,} records are "
                          f"non-PASS — corrected.")

    # 3. Coverage — groups the analysis REFUSED must not vanish from the write-up. Audited, not
    # rewritten: the grounding block already instructs the writer to state them, and inventing the
    # sentence here would put prose in the manuscript that no model wrote. An unmentioned group is
    # therefore reported as an issue so the Diagnostics show whether the instruction was obeyed.
    for group in (uncovered or {}):
        if not re.search(rf"\b{re.escape(str(group))}\b", md):
            issues.append(
                f"coverage: '{group}' was NOT analysed (too few cells) but the report never mentions "
                "it — the manuscript reads as though every group was covered.")
    # 4. Coverage counts — a refused group described with the uploaded file's counts, not the tested ones.
    md, count_issues = correct_coverage_counts(md, uncovered or {}, tested_counts or {}, pre_counts or {})
    issues += count_issues
    # 5. Marker claims — "RLBP1 is a canonical rod photoreceptor marker" (run c57071e7dc94) when the
    # curated reference the annotation used lists RLBP1 under Muller glia: the sentence is removed.
    md, marker_issues = check_marker_claims(md, marker_reference)
    issues += marker_issues
    # 6. Class labels — one class written under another class's name (run 74873590ca7b: "Microglia-
    # associated (MG) nuclei" in a dataset whose `majorclass` has both MG and Microglia).
    md, label_issues = check_label_conflation(md, class_labels or [], label_reference, label_column)
    issues += label_issues
    return md, issues


def _marker_reference(rounds: "list[LabRound]") -> "dict[str, Any] | None":
    """The curated marker reference (tissue JSON) an accepted ``run_marker_annotation`` step used."""
    from ...tools.api import available_references, load_marker_reference
    names = set(available_references())
    for r in reversed(rounds):
        if r.verdict.verdict != "accept":
            continue
        for s in r.scientist_result.get("steps", []):
            ref = s.get("result", {}).get("reference") if isinstance(s.get("result"), dict) else None
            if isinstance(ref, dict) and ref.get("name") in names:
                return load_marker_reference(ref["name"])
    return None


def _term_label(t: Any) -> str:
    """A pathway term as text. ``run_enrichment`` lists plain strings; ``run_gsea_prerank`` lists
    ``{"term", "gene_set", "nes", "fdr", "direction"}`` dicts. Every consumer that joined these
    as strings raised on the dict shape — and one of them (the literature digest) sat outside its
    caller's try, so the whole literature step degraded to searching for the pasted step brief."""
    if isinstance(t, dict):
        return str(t.get("term") or t.get("name") or t.get("Term") or "").strip()
    return str(t or "").strip()


def _assign_evidence(experts: "tuple[Specialist, ...]",
                     rounds: "list[LabRound]") -> "list[str]":
    """Give each expert a DIFFERENT slice of the accepted findings — one block per expert.

    Without this, every member of a "team" sees the identical prompt and differs only by a persona
    string, so their disagreement is drawn from one posterior: it is self-consistency sampling
    wearing name tags, not a team. Disagreement between agents that examined DIFFERENT evidence is
    information; disagreement between agents that read the same paragraph is sampling noise.

    Findings are dealt round-robin so each expert owns a comparable share, and each is told the
    others hold the rest — an expert that does not know its view is partial reports it as the whole
    picture. Empty (no accepted findings yet, i.e. the design meeting) returns blank blocks, and the
    asymmetry there comes from the tools instead."""
    n = max(1, len(experts))
    accepted = [r for r in rounds if r.verdict.verdict == "accept"]
    if not accepted:
        return [""] * n

    def _line(r: "LabRound") -> str:
        res = r.scientist_result or {}
        answer = str(res.get("final_answer") or "").strip() or "(no write-up)"
        ev: list[str] = []
        for s in res.get("steps", []) or []:
            ev += [p for p in evidence_pointers(s.get("result")) if p not in ev]
        tail = f"\n   evidence on disk: {', '.join(ev[:6])}" if ev else ""
        return f"- Step {r.step_index}: {r.step}\n   result: {answer[:600]}{tail}"

    blocks: list[str] = []
    for i in range(n):
        mine = accepted[i::n]
        others = len(accepted) - len(mine)
        if not mine:
            blocks.append(
                "YOUR ASSIGNED EVIDENCE: none — the findings were shared among the other members. "
                "Contribute from your expertise and interrogate what they report; do not invent "
                "results you were not given.")
            continue
        head = (f"YOUR ASSIGNED EVIDENCE — you are the member responsible for these "
                f"{len(mine)} finding(s). The other {others} finding(s) were assigned to OTHER "
                "members, so your view is deliberately PARTIAL:")
        foot = ("\nReport what YOUR evidence actually shows — read the artifacts above with your "
                "tools if the write-up is thin or you doubt it. Say plainly where your evidence "
                "may CONTRADICT what another member is likely to report, and what would settle it. "
                "Do NOT claim anything about findings you were not assigned.")
        blocks.append(head + "\n" + "\n".join(_line(r) for r in mine) + foot)
    return blocks


def _round_feedback(score: float, critique: str) -> str:
    """Score-driven instruction for the NEXT round's experts: the challenge is conditioned on
    the Critic's ACTUAL assessment (not a blanket 'disagree'). Low score -> push back hard;
    mid -> address concerns + challenge weak claims; high -> consolidate (the meeting also ends
    early before reaching here when the score clears ``meeting_accept_score``)."""
    if score < 0.5:
        return (f"\n\nThe Critic found serious problems (score {score:.2f}): {critique}\n"
                "Reconsider your position and push back HARD where the evidence is weak — do NOT just agree.")
    if score < 0.8:
        return (f"\n\nThe Critic raised concerns (score {score:.2f}): {critique}\n"
                "Address these specifically and challenge any claim you find unsupported.")
    return (f"\n\nThe Critic was largely satisfied (score {score:.2f}) but noted: {critique}\n"
            "Consolidate and tighten — refine only what materially matters. A high score is NOT a "
            "reason to drop a disagreement you still hold: if your evidence still conflicts with "
            "the synthesis, say so and name what would settle it.")
