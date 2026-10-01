"""Do the per-arm cell counts a step's answer states match what its own tools returned?

Run 8847d521ba32, stratified DE step. ``run_de`` computed the numbers correctly — its
``skipped_groups`` held Endothelial n_condition=7 (DDX41), n_reference=27 (WT) — and the step's
final answer tabulated every skipped class with the two arms swapped ("Endothelial: DDX41 27,
WT 7"). The same answer gave the five TESTED classes a per-arm split no tool had computed ("AC: WT
469 / DDX41 181" against the data's DDX41 392 / WT 258): the per-class totals survived, the split
was invented, and read literally it reverses the composition shift for AC, BC and Cone. The Critic
accepted it at 0.95, and the report writer later copied the table into a manuscript that also
carried the correct numbers, so the report contradicts itself.

The report writer has closed-set grounding (``_grounding_facts`` / ``verify_report_facts``); the
step had none. This module is that check, for the claim class that did the damage: a cell count
bound to one (group, arm) pair. It is deterministic — no model reads anything here — and narrow on
purpose, because the Critic refuses acceptance on what it returns:

* A number is a CLAIM only when the text binds it to a known group AND a known arm: a Markdown
  table row under an arm-named cell-count column, or a line such as "Endothelial (WT: 7,
  DDX41: 27)". Percentages, decimals, approximations and anything framed as depth, genes or
  expression are left alone.
* A claim is CONTRADICTED when a tool in this step counted that exact (group, arm) pair on the
  data it analysed and got a different number, or when the claim exceeds what the raw dataset
  holds for that pair — no filter adds cells. A count BELOW the raw file's is never flagged on the
  profile alone: QC may have removed those cells.

Anything it cannot bind or verify it ignores. A missed claim costs what it cost before this
existed; a false one would cost a correct step its acceptance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .research_harness import step_succeeded

# A stated count: plain or with thousands separators ("1,708"). ``_INT_END`` stops "46,49" being
# read as 46, "7.5" as 7 and "12%" as a count.
_INT = r"\d{1,3}(?:,\d{3})+|\d+"
_INT_END = r"(?![.,]?\d)(?!\s*%)"

# What makes a column or a line a CELL count: "WT cells", "n (DDX41)", "n_condition", "cell
# counts". Singular "cell" is deliberately absent — it is how "Cell type" and "per cell" begin, and
# a "| Cell type | WT | DDX41 |" table of median depths is exactly what must not be read as counts.
_CELL_CUE = re.compile(
    r"\bcells\b|\bnuclei\b|\bcell[- ]counts?\b|\bnumber of cells\b|\bn\b|\bn_", re.IGNORECASE)
# ...and what makes it some other per-arm number. Letter-bounded rather than \b-bounded so the
# underscored field names count too: "n_genes", "n_umi", "total_counts" are not cells.
_NOT_CELLS = re.compile(
    r"%|(?<![a-z])(?:pct|fc|fdr|mean|median|ratio|up|down|scores?|genes?|degs?|umis?|reads?)"
    r"(?![a-z])|_counts?\b|percent|fraction|proportion|\bfold|\blog|average|depth|ncount|"
    r"nfeature|express|regulat|signific|p-?val|q-?val|padj", re.IGNORECASE)

_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z(]|\s*$)")
_WILD_TYPE = ("wt", "wild-type", "wild type", "wildtype")
_PROFILE = "the dataset profile (design_by_arm)"


@dataclass
class KnownCounts:
    """Cells per (group, arm) as the tools counted them, kept per source ({source: {group: {arm:
    n}}}). ``analysed`` holds what a tool in this step counted on the data it analysed — exact.
    ``dataset`` holds the raw file's counts — exact for the file, and a CEILING once QC has run."""

    analysed: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    dataset: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)      # another spelling -> arm label

    def _tables(self) -> list[dict[str, dict[str, int]]]:
        return [*self.analysed.values(), *self.dataset.values()]

    @property
    def groups(self) -> list[str]:
        return list(dict.fromkeys(g for t in self._tables() for g in t))

    @property
    def arms(self) -> list[str]:
        return list(dict.fromkeys(a for t in self._tables() for per in t.values() for a in per))


@dataclass(frozen=True)
class CountClaim:
    group: str
    arm: str
    value: int
    where: str                  # the table row or line it was read from


@dataclass(frozen=True)
class CountMismatch:
    group: str
    arm: str
    stated: int
    expected: int
    source: str                 # e.g. "run_de.skipped_groups"
    truth: tuple                # ((arm, n), ...): that source's counts for every arm of the group
    exceeds_dataset: bool       # the source is the raw file, and the answer states MORE cells
    where: str


def contrast_arms(result: Any) -> "tuple[str, str] | None":
    """(condition, reference) level names of a ``run_de`` contrast — what its ``n_condition`` and
    ``n_reference`` count. Read from ``condition`` when the tool states it, else parsed from
    ``comparison`` ("sampleid: DDX41 vs WT (reference=WT), ..."), which is all an older result
    carries. None for one-vs-rest markers, or when either name is unknown."""
    if not isinstance(result, dict):
        return None
    ref = str(result.get("reference") or "").strip()
    if not ref or ref == "rest":
        return None
    cond = str(result.get("condition") or "").strip()
    if not cond:
        m = re.match(rf"[^:]*:\s*(.+?)\s+vs\s+{re.escape(ref)}\s+\(reference=",
                     str(result.get("comparison") or ""))
        cond = m.group(1).strip() if m else ""
        if "/" in cond:             # several tested levels joined by "/": no single condition arm
            return None
    return (cond, ref) if cond and cond != ref else None


def known_counts(steps: "list[dict[str, Any]] | None",
                 dataset_result: "dict[str, Any] | None" = None) -> KnownCounts:
    """Every per-(group, arm) cell count this step's tools reported, plus the dataset profile's.

    ``dataset_result`` is the run's profile of its primary file (``decisions["dataset_result"]``).
    Pass None when it may not describe what the step analysed, e.g. with several datasets bound."""
    known = KnownCounts()
    roles: set[tuple[str, str]] = set()
    for st in steps or []:
        if isinstance(st, dict) and st.get("tool") != "finish" and step_succeeded(st):
            _collect(known, roles, st.get("result"), str(st.get("tool") or "tool"), 0)
    if isinstance(dataset_result, dict):
        _add_design(known, dataset_result.get("design_by_arm"), _PROFILE)
    for label in known.arms:
        if label.casefold() in _WILD_TYPE:
            for spelling in _WILD_TYPE:
                known.aliases.setdefault(spelling, label)
    if len(roles) == 1:
        # An answer may copy the tool's own field names ("n_condition = 7"); those are only
        # bindable when every contrast in the step agrees on which arm is which.
        (cond, ref), = roles
        known.aliases.setdefault("n_condition", cond)
        known.aliases.setdefault("n_reference", ref)
    return known


def count_claims(text: str, known: KnownCounts) -> list[CountClaim]:
    """The (group, arm) -> count statements ``text`` makes about groups and arms in ``known``."""
    vocab = _Vocab(known)
    if not vocab.groups or not vocab.arm_rx:
        return []
    lines = (text or "").splitlines()
    claims: list[CountClaim] = []
    i = 0
    while i < len(lines):
        if _is_table_line(lines[i]):
            j = i
            while j < len(lines) and _is_table_line(lines[j]):
                j += 1
            claims += _table_claims(lines[i:j], _lead_in(lines, i), vocab)
            i = j
        else:
            claims += _prose_claims(lines[i], _lead_in(lines, i), vocab)
            i += 1
    return claims


def find_count_mismatches(answer: str, steps: "list[dict[str, Any]] | None",
                          dataset_result: "dict[str, Any] | None" = None) -> list[CountMismatch]:
    """The per-arm cell counts ``answer`` states that this step's own tool results (or the dataset
    itself) contradict. Empty when nothing was checkable or everything checked out."""
    known = known_counts(steps, dataset_result)
    if not known.analysed and not known.dataset:
        return []
    out: list[CountMismatch] = []
    seen: set[tuple[str, str, int]] = set()
    for claim in count_claims(answer, known):
        key = (claim.group, claim.arm, claim.value)
        if key in seen:
            continue
        seen.add(key)
        mismatch = _judge(claim, known)
        if mismatch is not None:
            out.append(mismatch)
    return out


def describe_count_mismatches(mismatches: "list[CountMismatch]", limit: int = 12) -> list[str]:
    """One line per group, naming what the answer said and what the tools computed — written for
    the Critic payload and for the Scientist's retry, so it carries the right values."""
    by_group: dict[tuple[str, str], list[CountMismatch]] = {}
    for m in mismatches:
        by_group.setdefault((m.group, m.source), []).append(m)
    lines: list[str] = []
    for (group, source), ms in by_group.items():
        truth = dict(ms[0].truth)
        stated = {m.arm: m.stated for m in ms}
        order = [a for a in truth if a in stated] + [a for a in stated if a not in truth]
        said = ", ".join(f"{a}={stated[a]}" for a in order)
        has = ", ".join(f"{a}={n}" for a, n in truth.items())
        if ms[0].exceeds_dataset:
            lines.append(f"{group}: the answer states {said}, more {group} cells than the dataset "
                         f"itself holds ({has}, from {source}); no filter adds cells.")
            continue
        arms = list(truth)
        swapped = (len(arms) == 2 and set(stated) == set(arms) and truth[arms[0]] != truth[arms[1]]
                   and stated[arms[0]] == truth[arms[1]] and stated[arms[1]] == truth[arms[0]])
        lines.append(f"{group}: the answer states {said}, but {source} computed {has}"
                     + (" — the two arms are swapped." if swapped else "."))
    if len(lines) > limit:
        lines = lines[:limit] + [f"(+{len(lines) - limit} more group(s) with the same problem)"]
    return lines


# --- truth ----------------------------------------------------------------------------------


def _count(value: Any) -> "int | None":
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 and float(value).is_integer() else None


def _add_table(dest: dict[str, dict[str, dict[str, int]]], source: str, table: Any) -> None:
    if not isinstance(table, dict):
        return
    rows = dest.setdefault(source, {})
    for group, per in table.items():
        if not isinstance(per, dict):
            continue
        for arm, value in per.items():
            n = _count(value)
            if n is not None:
                rows.setdefault(str(group), {})[str(arm)] = n


def _add_design(known: KnownCounts, dba: Any, source: str) -> None:
    """A ``design_by_arm`` block (the dataset profile, or ``inspect_dataset`` in the step)."""
    if not isinstance(dba, dict) or not isinstance(dba.get("cells_by_label_and_arm"), dict):
        return
    scanned, total = _count(dba.get("cells_scanned")), _count(dba.get("cells_total"))
    if scanned is not None and total is not None and scanned < total:
        return      # the table covers only the first `scanned` cells: an undercount, not a ceiling
    _add_table(known.dataset, source, dba["cells_by_label_and_arm"])


def _collect(known: KnownCounts, roles: "set[tuple[str, str]]", obj: Any, tool: str,
             depth: int) -> None:
    if depth > 4 or not isinstance(obj, dict):
        return
    _add_table(known.analysed, f"{tool}.cells_by_group_and_arm", obj.get("cells_by_group_and_arm"))
    skipped = obj.get("skipped_groups")
    arms = contrast_arms(obj) if isinstance(skipped, list) else None
    if arms:
        roles.add(arms)
        table = {str(item["group"]): {arms[0]: item.get("n_condition"),
                                      arms[1]: item.get("n_reference")}
                 for item in skipped if isinstance(item, dict) and item.get("group") is not None}
        _add_table(known.analysed, f"{tool}.skipped_groups", table)
    _add_design(known, obj.get("design_by_arm"), f"{tool}.design_by_arm")
    for key, value in obj.items():
        if key not in ("cells_by_group_and_arm", "skipped_groups", "design_by_arm"):
            _collect(known, roles, value, tool, depth + 1)


def _judge(claim: CountClaim, known: KnownCounts) -> "CountMismatch | None":
    def _having(tables: dict[str, dict[str, dict[str, int]]]) -> list[tuple[str, dict[str, int]]]:
        return [(src, t[claim.group]) for src, t in tables.items()
                if claim.arm in t.get(claim.group, {})]

    analysed, dataset = _having(known.analysed), _having(known.dataset)
    if any(per[claim.arm] == claim.value for _, per in analysed + dataset):
        return None                 # a tool in this step, or the file itself, says exactly this
    if analysed:
        src, per = analysed[0]
        return CountMismatch(claim.group, claim.arm, claim.value, per[claim.arm], src,
                             tuple(per.items()), False, claim.where)
    for src, per in dataset:
        if claim.value > per[claim.arm]:
            return CountMismatch(claim.group, claim.arm, claim.value, per[claim.arm], src,
                                 tuple(per.items()), True, claim.where)
    return None                     # below the raw count: QC may explain it, so not a contradiction


# --- claims ---------------------------------------------------------------------------------


class _Vocab:
    """The groups and arms in play, compiled once per answer."""

    def __init__(self, known: KnownCounts) -> None:
        self.groups = known.groups
        self._by_fold: dict[str, list[str]] = {}
        for g in self.groups:
            self._by_fold.setdefault(g.casefold(), []).append(g)
        # Group names in prose must contain a letter: a bare "3" is a count far more often than a
        # cluster. Short all-caps labels (AC, BC, MG) match case-sensitively, so "ac" is not one.
        self.group_rx = {
            g: re.compile(rf"(?<![\w-]){re.escape(g)}s?(?!\w)",
                          0 if g.isupper() and len(g) <= 5 else re.IGNORECASE)
            for g in self.groups if re.search(r"[A-Za-z]", g)}
        spellings = {a: [a] for a in known.arms if len(a) >= 2 and re.search(r"[A-Za-z]", a)}
        for spelling, arm in known.aliases.items():
            if arm in spellings and spelling.casefold() != arm.casefold():
                spellings[arm].append(spelling)
        self.arm_rx: dict[str, re.Pattern[str]] = {}
        self.pair_rx: dict[str, tuple[re.Pattern[str], ...]] = {}
        for arm, names in spellings.items():
            alt = "|".join(re.escape(s) for s in sorted(names, key=len, reverse=True))
            word = rf"(?<![\w-])(?:{alt})(?!\w)"          # "non-WT" is not WT; "DDX41-mutant" is
            self.arm_rx[arm] = re.compile(word, re.IGNORECASE)
            self.pair_rx[arm] = tuple(re.compile(p, re.IGNORECASE) for p in (
                rf"{word}\s*(?::|=|\(\s*n\s*=)\s*(?P<n>{_INT}){_INT_END}",   # WT: 7 / WT (n = 7)
                rf"{word}\s+(?:n\s*=\s*)?(?P<n>{_INT}){_INT_END}",           # WT 258 / WT n=258
                rf"(?<![\d.,])(?P<n>{_INT})\s+{word}",                       # 258 WT
            ))

    def match_group(self, cell: str) -> "str | None":
        """A table cell naming exactly one group: "AC", "**Endothelial**", "RGC (Retinal Ganglion)",
        "Müller glia (MG)", "Rods"."""
        text = _plain(cell)
        head, paren, rest = text.partition("(")
        for cand in (text, head.strip(), rest.split(")", 1)[0].strip() if paren else ""):
            for form in (cand, cand[:-1] if len(cand) > 2 and cand[-1] in "sS" else ""):
                hits = self._by_fold.get(form.casefold()) if form else None
                if hits and len(hits) == 1:
                    return hits[0]
        return None

    def arms_in(self, text: str) -> list[str]:
        return [a for a, rx in self.arm_rx.items() if rx.search(text)]

    def arm_values(self, text: str) -> dict[str, set[int]]:
        found: dict[str, set[int]] = {}
        for arm, patterns in self.pair_rx.items():
            for rx in patterns:
                for m in rx.finditer(text):
                    found.setdefault(arm, set()).add(int(m.group("n").replace(",", "")))
        return found


def _plain(text: str) -> str:
    """A cell or line without Markdown emphasis. Underscores stay: ``n_condition`` is a name."""
    text = str(text).replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip(" *")


def _clip(text: str, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.strip() for c in s.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells)


def _lead_in(lines: list[str], idx: int) -> str:
    """The sentence that introduces a table or a list — "Six classes were excluded for too few
    cells:" — which is often the only place its numbers are said to be cell counts."""
    for k in range(idx - 1, max(-1, idx - 12), -1):
        s = lines[k].strip()
        if not s or _is_table_line(s) or _LIST_ITEM.match(s):
            continue
        return re.sub(r"^#+\s*", "", _plain(s))
    return ""


def _is_cells(text: str) -> bool:
    return bool(_CELL_CUE.search(text)) and not _NOT_CELLS.search(text)


def _parse_count(cell: str) -> "int | None":
    m = re.fullmatch(rf"({_INT})(?:\s*cells?)?(?:\s*\([^()]*\))?", _plain(cell))
    return int(m.group(1).replace(",", "")) if m else None


def _table_claims(block: list[str], lead: str, vocab: _Vocab) -> list[CountClaim]:
    rows = [_split_row(r) for r in block]
    if len(rows) < 3 or not _is_separator(rows[1]):
        return []
    header, width = rows[0], len(rows[1])
    if len(header) != width:
        return []   # an unescaped "|" in a cell shifted the columns; binding them would be a guess
    cue_elsewhere = any(_CELL_CUE.search(_plain(h)) for h in header) or _is_cells(lead)
    columns: dict[int, str] = {}
    for j, head in enumerate(header):
        text = _plain(head)
        arms = vocab.arms_in(text)
        if len(arms) == 1 and not _NOT_CELLS.search(text) and (_CELL_CUE.search(text)
                                                              or cue_elsewhere):
            columns[j] = arms[0]
    if not columns:
        return []
    out: list[CountClaim] = []
    for row in rows[2:]:
        if len(row) != width:
            continue
        others = [c for j, c in enumerate(row) if j not in columns]
        group = vocab.match_group(others[0]) if others else None
        if group is None:
            hits = {vocab.match_group(c) for c in others[1:]} - {None}
            group = hits.pop() if len(hits) == 1 else None
        if group is None:
            continue
        for j, arm in columns.items():
            n = _parse_count(row[j])
            if n is not None:
                out.append(CountClaim(group, arm, n, _clip("| " + " | ".join(row) + " |")))
    return out


def _prose_claims(line: str, lead: str, vocab: _Vocab) -> list[CountClaim]:
    """"- **Endothelial** (WT: 7, DDX41: 27)", "Endothelial (27 WT / 7 DDX41 cells), HC (11 WT /
    5 DDX41 cells)": each group mention owns the text up to the next mention or the end of its
    sentence, and binds there only when at least two arms carry one number each and something
    says the numbers are cells. Nothing before the mention may frame them as anything else —
    "Up-regulated genes in AC: DDX41 392, WT 258" is not a cell count. A list item is also framed
    by the sentence that introduces its list; a paragraph only by itself (the line above it is as
    often a section heading — "3. Stratified Differential Expression" — as an introduction)."""
    text = _plain(line)
    starts: dict[int, str] = {}
    for group, rx in vocab.group_rx.items():
        for m in rx.finditer(text):
            if len(group) > len(starts.get(m.start(), "")):
                starts[m.start()] = group       # "Rod bipolar" over "Rod" at the same spot
    lead = lead if _LIST_ITEM.match(line.strip()) else ""
    if not starts or _NOT_CELLS.search(lead) or not (_CELL_CUE.search(text) or _is_cells(lead)):
        return []           # a list introduced as genes / depth / percentages is not about cells
    marks = sorted(starts)
    out: list[CountClaim] = []
    for n, start in enumerate(marks):
        end = marks[n + 1] if n + 1 < len(marks) else len(text)
        stop = _SENTENCE_END.search(text, start)
        end = min(end, stop.end()) if stop else end
        if _NOT_CELLS.search(text[:end]):
            break           # everything after this point is framed by it too
        values = vocab.arm_values(text[start:end])
        if len(values) < 2 or any(len(v) != 1 for v in values.values()):
            continue        # one arm read two ways ("DDX41 392 WT 258" also parses as "392 WT")
        out += [CountClaim(starts[start], arm, next(iter(v)), _clip(text[start:end]))
                for arm, v in values.items()]
    return out
