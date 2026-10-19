"""How we know these results are correct: a statement built by code from the run's own files.

Asked for on 2026-10-08, after run f3b8268c4fd4's report headlined "absent RPE" on a cluster that
was mislabelled Muller glia and a percentage ("83.8% in Muller glia") that was the table's "other"
row. Every line here is a check run on the run's tables, the values its tools returned and the final
manuscript text; no model is asked whether its own work is right. A check that could not run says so.

Five checks:

* **Numbers.** Every percentage, decimal and integer of 100 or more in the manuscript is looked up in
  the run's outputs: verbatim (at the precision shown), or as arithmetic on numbers of ONE table
  column, row or tool result (a ratio, a difference, a column total). A derived number is listed with
  its formula, so a reader can see that "83.8% were in Muller glia" is ``n_Best2_positive_other /
  n_Best2_positive_total``. A number found nowhere is listed as untraced.
* **Cell-type labels.** What each label rests on (the lineage markers enriched in its clusters), which
  clusters stayed unassigned and why, and labelled clusters whose own top genes are another lineage's.
* **QC accounting.** Whether the QC step's per-stage counts add up.
* **Steps.** Planned steps that did not complete, or were accepted with reservations.
* **Scope.** Literature checked or not; what a single sample or a dominant population cannot show.

Pure: reads files and the run record, returns Markdown plus a summary dict.
"""
from __future__ import annotations

import csv
import json
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# A table larger than this contributes its first rows and column extremes only, and is not searched
# for derived numbers: a 5,000-row DE table holds a near match for almost any decimal.
_BIG_TABLE_VALUES = 150
_BIG_TABLE_ROWS = 30
_SKIP_SECTIONS = re.compile(r"^#{1,3}\s*(References|Background consulted|Output Files Index|"
                            r"Verification)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_MASKS = (re.compile(r"`[^`]*`"), re.compile(r"\]\([^)]*\)"), re.compile(r"https?://\S+"),
          re.compile(r"<!--.*?-->"), re.compile(r"^\s*\d+(?:\.\d+)*[.)]\s"))


@dataclass
class _Claim:
    text: str
    value: float
    decimals: int
    pct: bool
    snippet: str
    sentence: str = ""
    status: str = "untraced"
    source: str = ""


_PCTLIKE = re.compile(r"pct|percent|frac|ratio|proportion|rate|share|%", re.IGNORECASE)


@dataclass
class _Pool:
    """Evidence values in three tiers, strongest first:

    * ``table``   — small result tables, structured tool-result fields, data/ JSON, tool arguments;
    * ``big``     — the first rows and column extremes of large tables (DE, GSEA, enrichment);
    * ``printed`` — numbers inside printed output (a script's stdout). The number was computed, but
      the words around it were written by the model, so its meaning is not verified.

    Groups (one table column or row, one tool result's sibling fields) are what derived numbers may
    be computed within."""
    values: dict[str, list[tuple[float, str, bool]]] = field(
        default_factory=lambda: {"table": [], "big": [], "printed": []})
    groups: list[tuple[str, list[tuple[float, str]]]] = field(default_factory=list)
    _keys: dict[str, list[float]] = field(default_factory=dict)

    def add(self, value: float, where: str, tier: str = "table") -> None:
        self.values[tier].append((value, where, bool(_PCTLIKE.search(where))))

    def group(self, name: str, items: list[tuple[float, str]]) -> None:
        nums = [(v, w) for v, w in items if v == v]               # drop NaN
        if 2 <= len(nums) <= 60:
            self.groups.append((name, nums))

    def freeze(self) -> None:
        for tier, vals in self.values.items():
            vals.sort(key=lambda t: t[0])
            self._keys[tier] = [v for v, _, _ in vals]

    def find(self, tier: str, target: float, tol: float, pctlike: bool = False,
             integer: bool = False) -> "str | None":
        keys, vals = self._keys.get(tier, []), self.values.get(tier, [])
        i = bisect_left(keys, target - tol)
        while i < len(keys) and keys[i] <= target + tol:
            v, where, is_pct = vals[i]
            if (not pctlike or is_pct) and (not integer or float(v).is_integer()):
                return where
            i += 1
        return None


# --- the manuscript's numbers ------------------------------------------------------------------

def _mask(line: str) -> str:
    for rx in _MASKS:
        line = rx.sub(lambda m: " " * len(m.group()), line)
    return line


def _sentence(line: str, start: int, end: int, full: bool = False) -> str:
    left = max(line.rfind(". ", 0, start), line.rfind("| ", 0, start), -1) + 1
    right = [i for i in (line.find(". ", end), line.find(" |", end)) if i != -1]
    stop = min(right) + 1 if right else len(line)
    text = re.sub(r"[*_]{1,2}", "", line[left:stop]).strip(" |")
    if full:
        return text
    if len(text) > 170:
        mid = start - left
        text = ("…" if mid > 85 else "") + text[max(0, mid - 85):mid + 85].strip() + "…"
    return text


def manuscript_numbers(md: str) -> list[_Claim]:
    """Every checkable number in the manuscript body: percentages, decimals, integers >= 100.

    Skipped: headings, references and the file index, code spans and link targets (file names),
    list markers, numbers inside identifiers (Cldn2, GRCm39, GO:0007601, 10x), years, and integers
    below 100 — cluster ids and small counts would trace to some table by chance."""
    claims: list[_Claim] = []
    skipping = False
    for raw in md.splitlines():
        if raw.lstrip().startswith("#"):
            skipping = bool(_SKIP_SECTIONS.match(raw.lstrip()))
            continue
        if skipping or re.match(r"^\s*\|?\s*:?-{3,}", raw):
            continue
        line = _mask(raw)
        for m in _NUMBER.finditer(line):
            s, e = m.span()
            tok = m.group()
            prev = line[s - 1] if s else ""
            before = line[s - 2] if s >= 2 else ""
            nxt = line[e] if e < len(line) else ""
            if prev and (prev.isalnum() or prev in "_.:/^"):
                continue
            if prev == "-" and before.isalpha():
                continue                                          # R-HSA-123, ON-1
            if nxt and (nxt.isalpha() or nxt in "_'′"):
                continue                                          # 10x, 3', 2nd
            pct = bool(re.match(r"\s?%", line[e:e + 2]))
            negative = prev == "−" or (prev == "-" and (not before or before in " (=:[/"))
            value = float(tok.replace(",", ""))
            decimals = len(tok.split(".", 1)[1]) if "." in tok else 0
            if value == 0 or (pct and value == 100):
                continue                                          # "0.0%", "100%": nothing to trace
            if not pct and decimals == 0:
                if value < 100 or ("," not in tok and 1900 <= value <= 2100):
                    continue
            claims.append(_Claim(tok + ("%" if pct else ""), -value if negative else value,
                                 decimals, pct, _sentence(raw, s, e), _sentence(raw, s, e, True)))
    return claims


# --- the run's outputs -------------------------------------------------------------------------

def _num(x: Any) -> "float | None":
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        t = x.strip().replace(",", "")
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", t):
            return float(t)
    return None


# Tools whose printed output is code the MODEL wrote; every other tool's strings are the platform's.
_MODEL_CODE_TOOLS = {"run_code", "run_in_environment", "run_shell", "shell"}
# Tools whose returns are not results: the Scientist's own answer (finish), lookups (source code,
# directory listings, a file re-read), and literature (a paper's numbers are not this run's).
_NOT_EVIDENCE = {"finish", "read_tool_source", "describe_environment", "search_skills",
                 "read_skill_reference", "list_dir", "stat_path", "find_files", "read_text",
                 "disk_usage", "literature_search", "deep_literature", "search_corpus"}
_ID_COLUMN = re.compile(r"^(leiden|louvain|cluster|clusters|cl|rank|index|idx|id|row|sample|"
                        r"sampleid|group|groups|slug|round|step)$", re.IGNORECASE)


def _walk(obj: Any, path: str, pool: _Pool, source: str, depth: int = 0,
          printed: str = "printed") -> None:
    """Every number in a tool result or JSON file. Strings are scanned too: a platform tool's
    evidence text ("Rho=4.61") is its own output; a script's stdout goes to the ``printed`` tier.
    Sibling whole numbers of one dict form a group (plus their total)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        sibs: list[tuple[float, str]] = []
        for k, v in obj.items():
            n = _num(v) if not isinstance(v, str) else None
            where = f"{path}.{k}" if path else str(k)
            if n is not None:
                pool.add(n, f"{source}: {where}")
                if not _ID_COLUMN.match(str(k)):
                    sibs.append((n, where))
            else:
                _walk(v, where, pool, source, depth + 1, printed)
        ints = [(n, w) for n, w in sibs if float(n).is_integer()]
        if len(ints) >= 2:
            total = sum(n for n, _ in ints)
            pool.add(total, f"{source}: total of {path or 'result'}")
            pool.group(f"{source} ({path or 'result'})", ints + [(total, f"total of {path or 'result'}")])
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:500]):
            _walk(v, f"{path}[{i}]", pool, source, depth + 1, printed)
    elif isinstance(obj, str) and len(obj) <= 20000 and printed != "skip":
        for m in re.finditer(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?", obj):
            n = _num(m.group().replace("−", "-"))
            if n is not None:
                tag = " (printed)" if printed == "printed" else " (text)"
                pool.add(n, f"{source}: {path}{tag}", printed)
    else:
        n = _num(obj)
        if n is not None:
            pool.add(n, f"{source}: {path}")


def _read_table(path: Path) -> "tuple[list[str], list[list[str]]] | None":
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return (rows[0], rows[1:]) if rows else None


def _add_table(path: Path, rel: str, pool: _Pool) -> None:
    got = _read_table(path)
    if got is None:
        return
    header, rows = got
    numeric = sum(1 for r in rows for c in r if _num(c) is not None)
    big = numeric > _BIG_TABLE_VALUES
    tier = "big" if big else "table"
    cols: dict[int, list[tuple[float, str]]] = {}
    for ri, row in enumerate(rows):
        label = row[0] if row else str(ri)
        rnums: list[tuple[float, str]] = []
        for ci, cell in enumerate(row):
            name = header[ci] if ci < len(header) else f"col{ci}"
            if ci == 0 or _ID_COLUMN.match(name.strip()):
                continue                                   # a row label or an id, not a result
            if big and ri >= _BIG_TABLE_ROWS:
                n = _num(cell)
                if n is not None:
                    cols.setdefault(ci, []).append((n, f"{label} / {name}"))
                continue
            n = _num(cell)
            where = f"{label} / {name}"
            if n is None:
                # evidence text a tool wrote into a cell ("Rho=4.61, Nrl=2.27")
                for m in re.finditer(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", cell or ""):
                    pool.add(float(m.group()), f"{rel}: {where} (text)", tier)
                continue
            cols.setdefault(ci, []).append((n, where))
            pool.add(n, f"{rel}: {where}", tier)
            if float(n).is_integer():
                rnums.append((n, where))
        if not big:
            pool.group(f"{rel} (row {label})", rnums)
    for ci, items in cols.items():
        name = header[ci] if ci < len(header) else f"col{ci}"
        vals = [v for v, _ in items]
        pool.add(max(vals), f"{rel}: maximum of {name}", tier)
        pool.add(min(vals), f"{rel}: minimum of {name}", tier)
        ints = [(v, w) for v, w in items if float(v).is_integer()]
        if not big and len(ints) >= 2:
            total = sum(v for v, _ in ints)
            if len(ints) == len(items):
                pool.add(total, f"{rel}: total of {name}", tier)
            pool.group(f"{rel} (column {name})", ints + [(total, f"total of {name}")])


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _steps(rounds: Iterable[Any]) -> Iterable[tuple[int, dict]]:
    for i, r in enumerate(rounds):
        sr = _get(r, "scientist_result") or {}
        for st in (sr.get("steps") or []) if isinstance(sr, dict) else []:
            if isinstance(st, dict):
                yield i, st


def evidence_pool(art: Path, rounds: "list[Any]") -> _Pool:
    """Everything the run computed: its tables, its data/ JSON, and each tool call's arguments and
    returned values. The Scientist's and the writer's own prose is NOT evidence."""
    pool = _Pool()
    for sub in ("tables", "cellqc"):
        base = art / sub
        if base.is_dir():
            for p in sorted(base.rglob("*.csv")):
                _add_table(p, str(p.relative_to(art)), pool)
    data = art / "data"
    if data.is_dir():
        for p in sorted(data.glob("*.json")):
            try:
                _walk(json.loads(p.read_text(encoding="utf-8")), "", pool, f"data/{p.name}")
            except (OSError, ValueError):
                continue
    for i, st in _steps(rounds):
        tool = str(st.get("tool") or "tool")
        if tool in _NOT_EVIDENCE:
            continue
        printed = "printed" if tool in _MODEL_CODE_TOOLS else "table"
        # Arguments: the numbers a tool was given (thresholds); never the text of code or prose.
        _walk(st.get("args") or {}, "args", pool, f"round {i + 1} {tool}", printed="skip")
        _walk(st.get("result"), "", pool, f"round {i + 1} {tool}", printed=printed)
    pool.freeze()
    return pool


def _tolerance(c: _Claim) -> float:
    return 0.5 * 10 ** -c.decimals + 1e-9


def _significant(text: str) -> int:
    digits = re.sub(r"[^0-9]", "", text).lstrip("0")
    return len(digits)


def trace(claims: list[_Claim], pool: _Pool) -> None:
    """Mark each claim, in place: ``table`` (as it is, in a result table or tool output),
    ``derived`` (a ratio or difference of numbers in one table column/row or tool result),
    ``printed`` (found only in a script's printed output) or ``untraced``.

    Percentages match only fields named like one (pct, fraction, ratio …) or a derived ratio; whole
    numbers match only whole numbers; large tables and printed output are consulted only for numbers
    with three or more significant digits, after a derivation inside one small table has been tried —
    a two-digit decimal is in a 5,000-row DE table somewhere by chance."""
    for c in claims:
        tol = _tolerance(c)
        precise = _significant(c.text) >= 3
        integer = c.decimals == 0 and not c.pct

        def look(tier: str) -> "str | None":
            hit = c.pct and pool.find(tier, c.value / 100, tol / 100, pctlike=True)
            hit = hit or pool.find(tier, c.value, tol, pctlike=c.pct, integer=integer)
            if not hit and c.value < 0:
                hit = pool.find(tier, -c.value, tol, pctlike=c.pct, integer=integer)
            return hit or None

        stated = _in_sentence(c) if c.pct else None
        if stated and stated[0] == "inconsistent":
            c.status, c.source = stated
            continue
        hit = look("table")
        if hit:
            c.status, c.source = "table", hit
            continue
        if stated:
            c.status, c.source = stated
            continue
        # A whole-percent claim ("95%") is ±0.5 points wide: some pair of counts in a column fits it
        # by chance, so only a decimal one is derived from a table.
        found = None if (c.pct and c.decimals == 0) else next(
            ((f, name) for name, items in pool.groups
             if (f := _derive(abs(c.value), tol, c.pct, items))), None)
        if found:
            c.status, c.source = "derived", f"{found[0]} ({found[1]})" + _bucket_warning(found[0], c)
            continue
        if precise and (hit := look("big")):
            c.status, c.source = "table", hit
            continue
        if precise and (hit := look("printed")):
            c.status, c.source = "printed", hit


_TOTAL_LIKE = re.compile(r"total|\ball\b|_all\b|global|\bsum\b|before|input|overall", re.IGNORECASE)


def _derive(target: float, tol: float, pct: bool, items: list[tuple[float, str]]) -> "str | None":
    """A percentage as count ÷ a TOTAL × 100 (a column total, or a field named like one), or a whole
    number as a difference of two, within one group. Ratios of arbitrary decimals, or of two arbitrary
    counts, are not tried: on run 7f2955e8e "−1.04%" (a median's change) matched cluster 23's cells
    over cluster 4's, and "80.3%" two unrelated QC fields."""
    for i, (a, wa) in enumerate(items):
        for j, (b, wb) in enumerate(items):
            if i == j:
                continue
            if (pct and 0 < a < b and b >= 10 and _TOTAL_LIKE.search(wb)
                    and abs(100 * a / b - target) <= tol):
                return f"{wa} ÷ {wb} × 100"
            if not pct and a > b and abs((a - b) - target) <= tol and a - b >= 100:
                return f"{wa} − {wb}"
    return None


_BUCKETS = ("other", "others", "rest", "remaining", "unassigned", "none", "misc")


def _bucket_warning(formula: str, c: _Claim) -> str:
    """Run f3b8268c4fd4: "83.8% were in Muller glia" was ``n_Best2_positive_other ÷ total`` — the
    "other" bucket. Say so when the numerator is a catch-all the sentence never mentions."""
    numerator = formula.split(" ÷ ", 1)[0].lower()
    words = set(re.split(r"[^a-z]+", numerator))
    said = c.sentence.lower()
    hit = next((b for b in _BUCKETS if b in words and b not in said), None)
    return (f" — NOTE: the numerator is the '{hit}' bucket, which the sentence does not mention"
            if hit else "")


_STATED_SHARE = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d+)\s+(?:of|out of)\s+(?:the\s+|all\s+|these\s+)?"
    r"(\d{1,3}(?:,\d{3})+|\d+)\b[^.;%]{0,100}?\(?\s*(\d+(?:\.\d+)?)\s?%")


_STATED_CHANGE = re.compile(
    r"from\s+(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s+to\s+"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\b[^.;%]{0,60}?[−+-]?\s?(\d+(?:\.\d+)?)\s?%")


def _in_sentence(c: _Claim) -> "tuple[str, str] | None":
    """``(status, source)`` when the sentence itself says "A of B … P%" or "from A to B … P%":
    consistent or not."""
    for m in _STATED_CHANGE.finditer(c.sentence):
        a, b, p = (float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")),
                   float(m.group(3)))
        if abs(p - abs(c.value)) > 1e-9 or not a:
            continue
        if abs(100 * abs(b - a) / a - p) <= _tolerance(c) + 0.05:
            return "derived", f"({m.group(2)} − {m.group(1)}) ÷ {m.group(1)} × 100, both stated in the sentence"
    for m in _STATED_SHARE.finditer(c.sentence):
        a, b, p = (float(m.group(1).replace(",", "")), float(m.group(2).replace(",", "")),
                   float(m.group(3)))
        if abs(p - abs(c.value)) > 1e-9 or not b:
            continue
        real, tol = 100 * a / b, _tolerance(c) + 0.05
        if abs(real - p) <= tol:
            return "derived", f"{m.group(1)} ÷ {m.group(2)} × 100, both stated in the sentence"
        if abs((100 - real) - p) <= tol:      # "kept 16,750 of 18,681 (10.34% removed)"
            return "derived", (f"({m.group(2)} − {m.group(1)}) ÷ {m.group(2)} × 100, both stated in "
                               "the sentence")
        gap = c.sentence[m.end(2):m.start(3)]
        if len(gap) <= 30 and not re.search(r"\d", gap):
            return "inconsistent", f"{m.group(1)} of {m.group(2)} is {real:.1f}%, not {m.group(3)}%"
    return None


# --- labels, QC, steps, scope --------------------------------------------------------------------

def _cluster_table(art: Path) -> "dict[str, dict] | None":
    try:
        data = json.loads((art / "tables" / "cluster_cell_types.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def _ck(c: str) -> "tuple[int, str]":
    return (int(c), "") if str(c).isdigit() else (10 ** 9, str(c))


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v.startswith("["):
        return re.findall(r"'([^']+)'", v)
    return []


def labels_section(art: Path) -> "tuple[list[str], dict]":
    table = _cluster_table(art)
    if table is None:
        return ["No marker-based annotation table was written in this run, so no label could be "
                "checked against its markers."], {"labels_checked": False}
    by_label: dict[str, list[str]] = {}
    for c, row in table.items():
        by_label.setdefault(str(row.get("cell_type")), []).append(c)
    lines = ["A cluster keeps a label only when its lineage's markers are the strongest raw signal "
             "AND at least one of the lineage's specific markers (its defining marker, where the "
             "reference names one) is enriched against clusters given other labels. What each "
             "label rests on:", "",
             "| Cell type | Clusters | Cells | Markers enriched in those clusters | Note |",
             "|---|---|---:|---|---|"]
    single, crossed = [], []
    for lab in sorted((k for k in by_label if k != "Unassigned"),
                      key=lambda k: -sum(int(table[c].get("n_cells") or 0) for c in by_label[k])):
        cls = sorted(by_label[lab], key=_ck)
        cells = sum(int(table[c].get("n_cells") or 0) for c in cls)
        enriched = sorted({g for c in cls for g in _as_list(table[c].get("canonical_markers_enriched"))})
        notes = []
        for c in cls:
            mk = _as_list(table[c].get("canonical_markers_enriched"))
            if len(mk) == 1:
                notes.append(f"cluster {c} rests on {mk[0]} alone")
                single.append(c)
            other = _as_list(table[c].get("top_markers_of_other_lineages"))
            if other:
                notes.append(f"cluster {c}'s top genes are led by {', '.join(other)}")
                crossed.append(c)
        lines.append(f"| {lab} | {', '.join(cls)} | {cells:,} | {', '.join(enriched) or '—'} | "
                     f"{'; '.join(notes) or '—'} |")
    un = sorted(by_label.get("Unassigned", []), key=_ck)
    total = sum(int(r.get("n_cells") or 0) for r in table.values()) or 1
    un_cells = sum(int(table[c].get("n_cells") or 0) for c in un)
    if un_cells / total > 0.30:
        lines = [f"**WARNING: {un_cells:,} of {total:,} cells ({100 * un_cells / total:.1f}%) are "
                 "unassigned. A sample is not mostly cells of no type: treat every label and "
                 "composition figure in this report as unreliable until the annotation is "
                 "redone.**", ""] + lines
    if un:
        lines += ["", "Clusters left unassigned, and why (a reason of *mixed* means two lineages' "
                  "markers are both present — doublets, ambient RNA, or a type outside the reference):",
                  "", "| Cluster | Cells | Reason | Its own top genes |", "|---|---:|---|---|"]
        for c in un:
            row = table[c]
            why = str(row.get("evidence") or row.get("unassigned_reason") or "—").replace("|", "/")
            top = ", ".join(_as_list(row.get("top_markers"))[:8]) or "—"
            lines.append(f"| {c} | {int(row.get('n_cells') or 0):,} | {why} | {top} |")
    summary = {"labels_checked": True, "labels": len(by_label) - (1 if un else 0),
               "unassigned_clusters": len(un), "unassigned_share": round(un_cells / total, 4), "single_marker_clusters": single,
               "clusters_led_by_other_lineage": crossed}
    return lines, summary


def qc_section(rounds: "list[Any]") -> "tuple[list[str], dict]":
    qc = None
    for _, st in _steps(rounds):
        if st.get("tool") in ("run_cellqc", "run_scanpy_qc") and isinstance(st.get("result"), dict) \
                and st["result"].get("status") == "ok":
            qc = (st["tool"], st["result"])
    if qc is None:
        return ["No QC tool result was found, so the QC counts could not be reconciled."], {}
    tool, r = qc
    lines: list[str] = []
    ok = True
    before, after = r.get("cells_before"), r.get("cells_after")
    for lib in r.get("libraries") or []:
        if not isinstance(lib, dict):
            continue
        cr, filt, dbl = (lib.get("cells_cellranger"), lib.get("cells_after_filter"),
                         lib.get("cells_after_doublets"))
        removed = lib.get("removed_only_by") or {}
        parts = []
        if isinstance(cr, int) and isinstance(filt, int) and removed:
            s = sum(int(v) for v in removed.values())
            good = cr - filt == s
            ok &= good
            parts.append(f"{cr:,} → {filt:,} after the count/feature/mitochondrial gates: "
                         f"{cr - filt:,} removed, and the per-reason removals sum to {s:,} "
                         f"({'consistent' if good else 'INCONSISTENT'})")
        if isinstance(filt, int) and isinstance(dbl, int) and lib.get("doublets_removed") is not None:
            good = filt - dbl == int(lib["doublets_removed"])
            ok &= good
            parts.append(f"→ {dbl:,} after removing {int(lib['doublets_removed']):,} doublets "
                         f"({'consistent' if good else 'INCONSISTENT'})")
        if parts:
            lines.append(f"- {lib.get('sample', 'library')}: " + " ".join(parts) + ".")
    libs = [lib for lib in r.get("libraries") or [] if isinstance(lib, dict)]
    if isinstance(after, int) and libs and all(isinstance(lib.get("cells_after_doublets"), int)
                                               for lib in libs):
        s = sum(lib["cells_after_doublets"] for lib in libs)
        good = s == after
        ok &= good
        lines.append(f"- The QC checkpoint holds {after:,} cells; the libraries' final counts sum to "
                     f"{s:,} ({'consistent' if good else 'INCONSISTENT'}).")
    if isinstance(before, int) and isinstance(after, int) and not lines:
        lines.append(f"- {before:,} cells before QC, {after:,} after ({tool}); no per-stage counts "
                     "were returned to reconcile.")
    if r.get("counts_layer"):
        lines.append(f"- `layers['counts']` holds {r['counts_layer']}.")
    return lines, {"qc_consistent": ok, "qc_tool": tool}


def steps_section(agenda: "list[str]", rounds: "list[Any]") -> "tuple[list[str], dict]":
    last: dict[str, tuple[str, float, str]] = {}
    for r in rounds:
        step = str(_get(r, "step") or "")
        v = _get(r, "verdict") or {}
        last[step] = (str(_get(v, "verdict") or ""), float(_get(v, "score") or 0.0),
                      str(_get(v, "critique") or ""))
    lines: list[str] = []
    incomplete, reserved = 0, 0
    for i, item in enumerate(agenda or [], 1):
        title = re.sub(r"\*\*", "", item.split(" — ", 1)[0]).strip()[:90]
        got = last.get(item)
        if got is None:
            lines.append(f"- Step {i}, {title}: never ran.")
            incomplete += 1
            continue
        verdict, score, critique = got
        first = re.split(r"(?<=[.!?])\s", critique.strip(), maxsplit=1)[0][:260]
        if verdict != "accept":
            incomplete += 1
            lines.append(f"- Step {i}, {title}: NOT completed (last review {score:.2f}). {first}")
        elif score < 0.6:
            reserved += 1
            lines.append(f"- Step {i}, {title}: accepted with reservations ({score:.2f}). {first}")
    if not lines:
        lines.append(f"- All {len(agenda or [])} planned steps completed and were accepted by the "
                     "reviewer." if len(agenda or []) != 1 else
                     "- The one planned step completed and was accepted by the reviewer.")
    return lines, {"steps_incomplete": incomplete, "steps_reserved": reserved}


def scope_section(art: Path, lit: "dict | None", discussion: bool = True) -> "tuple[list[str], dict]":
    lines: list[str] = []
    cites = list((lit or {}).get("citations") or [])
    if cites:
        lines.append(f"- The interpretation was checked against {len(cites)} published paper(s), "
                     "listed under References.")
    else:
        reason = str((lit or {}).get("reason") or "no literature step produced citations")
        lines.append(f"- The interpretation was NOT checked against published literature ({reason}). "
                     "Statements about known biology in the Discussion are the writer's background "
                     "knowledge and are not verified here." if discussion else
                     f"- Nothing in the report was checked against published literature ({reason}).")
    samples = None
    got = _read_table(art / "tables" / "composition_by_sample.csv")
    if got:
        header, rows = got
        if header and header[0].lower() in ("sample", "sampleid"):
            samples = len({r[0] for r in rows if r})
    if samples == 1:
        lines.append("- One sample: no biological replicate, so nothing here can say how much these "
                     "numbers vary between animals or conditions; every proportion is descriptive.")
    got = _read_table(art / "tables" / "celltype_composition.csv")
    if got:
        header, rows = got
        try:
            pi, ci = header.index("pct"), header.index("cell_type")
            top = max((r for r in rows if r[ci] != "Unassigned"), key=lambda r: float(r[pi]))
            if float(top[pi]) >= 50:
                lines.append(f"- {top[ci]} make up {float(top[pi]):.1f}% of the cells, so every "
                             "per-cluster (one-vs-rest) test compares a cluster mostly against "
                             f"{top[ci]}: a gene or pathway 'down' in the other clusters is high in "
                             f"{top[ci]}, not a property of those clusters.")
        except (ValueError, IndexError):
            pass
    return lines, {"literature_checked": bool(cites), "samples": samples, "discussion": discussion}


def _verb(n: int, plural: str, singular: str) -> str:
    """"1 is", "2 are": the counts here are often 0 or 1 ("1 are arithmetic", run 6275df1213ab)."""
    return singular if n == 1 else plural


# --- the statement ----------------------------------------------------------------------------

def build_verification(art: "str | Path", report_md: str, agenda: "list[str] | None" = None,
                       rounds: "list[Any] | None" = None, lit: "dict | None" = None,
                       corrections: "list[str] | None" = None,
                       brief: bool = False, qc_only: bool = False) -> "tuple[str, dict]":
    """``(markdown, summary)`` — the full statement for ``report/verification.md``. ``brief``: the
    report is the short one, which has no Discussion. ``qc_only``: the run did quality control and
    nothing else, so there are no labels to check and nothing to hold against the literature; the
    statement does not list them as unchecked."""
    art = Path(art)
    rounds = list(rounds or [])
    claims = manuscript_numbers(report_md)
    trace(claims, evidence_pool(art, rounds))
    exact = [c for c in claims if c.status == "table"]
    derived = [c for c in claims if c.status == "derived"]
    printed = [c for c in claims if c.status == "printed"]
    untraced = [c for c in claims if c.status == "untraced"]
    wrong = [c for c in claims if c.status == "inconsistent"]
    coarse = sum(1 for c in exact if _significant(c.text) < 3)

    out = ["# How we know these results are correct", "",
           "Every statement below comes from a check that code ran on this run's own files after the "
           "analysis: its result tables, the values its tools returned, and the final report text. "
           "None of it is a model's opinion of its own work. Where a check could not run, it says so.",
           "", "## 1. Numbers in the report", ""]
    if claims:
        out.append(
            f"{len(claims)} {_verb(len(claims), 'numbers', 'number')} in the report "
            f"{_verb(len(claims), 'were', 'was')} checked (every percentage, every decimal and "
            f"every whole number of 100 or more). **{len(exact)}** "
            f"{_verb(len(exact), 'appear', 'appears')} as {_verb(len(exact), 'they are', 'it is')} "
            f"in a result table or a tool's returned values"
            + (f" ({coarse} of them {_verb(coarse, 'have', 'has')} only one or two significant "
               "digits, such as a parameter value of 0.8, where a match is weak evidence)"
               if coarse else "")
            + f". **{len(derived)}** {_verb(len(derived), 'are', 'is')} arithmetic on numbers of one "
            "table column, row or tool result: each is listed with its formula — check that the "
            f"formula says what the sentence says. **{len(printed)}** "
            f"{_verb(len(printed), 'were', 'was')} found only in output printed by "
            "analysis code the model wrote: the number was computed, but the words describing it "
            f"were the model's, so its meaning is not verified. **{len(untraced)}** could not be "
            "traced to anything this run computed."
            + (f" **{len(wrong)}** {_verb(len(wrong), 'do', 'does')} not match the counts stated "
               f"beside {_verb(len(wrong), 'them', 'it')}." if wrong else ""))
        for title, rows, with_source in (
                ("Arithmetic that does not add up", wrong, True),
                ("Not traced — treat these as unverified", untraced, False),
                ("Derived — check that the formula matches the sentence", derived, True),
                ("Printed by model-written code — meaning not verified", printed, True)):
            if not rows:
                continue
            out += ["", f"**{title}:**", ""]
            if with_source:
                out += ["| Number | Where it appears | Source |", "|---|---|---|"]
                out += [f"| {c.text} | {c.snippet.replace('|', '/')} | {c.source.replace('|', '/')} |"
                        for c in rows]
            else:
                out += ["| Number | Where it appears |", "|---|---|"]
                out += [f"| {c.text} | {c.snippet.replace('|', '/')} |" for c in rows]
    else:
        out.append("The report states no checkable numbers.")

    label_lines, label_sum = labels_section(art)
    qc_lines, qc_sum = qc_section(rounds)
    step_lines, step_sum = steps_section(list(agenda or []), rounds)
    scope_lines, scope_sum = scope_section(art, lit, discussion=not brief)
    if qc_only:
        scope_lines = [ln for ln in scope_lines if "published literature" not in ln]
        scope_sum = {**scope_sum, "literature_checked": None}
    sections = [("Cell-type labels", label_lines) if not qc_only else None,
                ("Quality-control accounting", qc_lines),
                ("Planned steps that did not finish as planned", step_lines),
                ("What this analysis cannot show", scope_lines) if scope_lines else None,
                ("Corrections made to the report automatically", [f"- {c}" for c in corrections])
                if corrections else None]
    for n, (title, lines) in enumerate((x for x in sections if x), start=2):
        out += ["", f"## {n}. {title}", "", *lines]
    summary = {"numbers_checked": len(claims), "numbers_exact": len(exact),
               "numbers_derived": len(derived), "numbers_printed": len(printed),
               "numbers_untraced": len(untraced), "numbers_inconsistent": len(wrong),
               "untraced": [c.text for c in untraced][:40],
               **label_sum, **qc_sum, **step_sum, **scope_sum}
    return "\n".join(out).rstrip() + "\n", summary


def manuscript_note(summary: dict) -> str:
    """The short ``## Verification`` section the manuscript carries; the full statement is a file."""
    if not summary:
        return ""
    bits = ["After the analysis, code checked this report against the run's own outputs; the full "
            "statement is `report/verification.md`."]
    n = summary.get("numbers_checked") or 0
    if n:
        exact, derived, printed = (int(summary.get(k) or 0) for k in
                                   ("numbers_exact", "numbers_derived", "numbers_printed"))
        bits.append(f"Of {n} {_verb(n, 'numbers', 'number')} checked, {exact} "
                    f"{_verb(exact, 'appear', 'appears')} in a result table or tool output, {derived} "
                    f"{_verb(derived, 'are', 'is')} arithmetic on one table (listed there with "
                    f"{_verb(derived, 'their formulas', 'its formula')}), {printed} "
                    f"{_verb(printed, 'appear', 'appears')} only in output printed by model-written "
                    f"code, and {summary.get('numbers_untraced', 0)} could not be traced.")
    if summary.get("numbers_inconsistent"):
        wrong = int(summary["numbers_inconsistent"])
        bits.append(f"{wrong} {_verb(wrong, 'percentages do', 'percentage does')} not match the "
                    f"counts stated beside {_verb(wrong, 'them', 'it')} (listed in the statement).")
    if summary.get("labels_checked"):
        bits.append(f"Each of the {summary.get('labels', 0)} cell-type labels rests on lineage markers "
                    f"enriched in its clusters; {summary.get('unassigned_clusters', 0)} cluster(s) "
                    "were left unassigned, each with its reason.")
    if (summary.get("unassigned_share") or 0) > 0.30:
        bits.append(f"WARNING: {100 * summary['unassigned_share']:.0f}% of the cells are unassigned, "
                    "so the cell-type labels and composition are not reliable.")
    if summary.get("qc_consistent") is False:
        bits.append("The QC step's per-stage counts do NOT add up (see the statement).")
    if summary.get("steps_incomplete"):
        bits.append(f"{summary['steps_incomplete']} planned step(s) did not complete.")
    if summary.get("literature_checked") is False:
        bits.append("The interpretation was not checked against published literature."
                    if summary.get("discussion", True) else
                    "Nothing in the report was checked against published literature.")
    return "## Verification\n\n" + " ".join(bits) + "\n"
