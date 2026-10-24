"""Cell-class labels in a written answer: the dataset's own, never the name of another class.

Prod run 74873590ca7b (2026-10-23, mouse retina, Claude Haiku 5.5): the dataset's ``majorclass``
column has MG (Müller glia) and Microglia as two classes. The step-2 Scientist wrote that "the MG
result should be checked against microglia-specific ISG expression"; its Critic asked to "label MG
explicitly (likely Müller glia) and keep it distinct from Microglia"; an expert in the interpretation
meeting wrote "a DDX41-higher shift in microglia/MG"; and the answer's headline read
"**Microglia-associated (MG) nuclei** show the clearest DDX41 excess". The writer's prompt forbade
expanding a label, but its list of labels came only from packaged tools' per-group outputs, and every
analysis step of that run was ``run_code``, so the writer was handed no labels at all.

Three pieces:

- ``dataset_labels``: the label column and its levels, read from the dataset profile, so the labels
  are known whichever tool the steps used.
- ``label_glossary``: a label's expansion, and only where the curated reference for the tissue gives
  one: a reference cell type the label names (Rod -> Rod photoreceptor), or whose initials it is
  (MG -> Muller glia). With no reference a label has no expansion.
- ``check_label_conflation``: an answer that gives one class's label the name of ANOTHER class of the
  same dataset has that name removed, leaving the label as the data writes it:
  "Microglia-associated (MG) nuclei" -> "MG nuclei", "MG (microglia)" -> "MG". Narrow on purpose:
  only a label in parentheses after a name, or a name in parentheses after a label, is judged; the
  name must be the whole phrase (a cell or nucleus word may follow it), so "rod bipolar (RBC)" is not
  read as Rod; and a contrast ("not microglia (MG)", "MG (vs Microglia)") or a number is left alone.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any, Iterable

# Words that may follow a class name and still leave it the name of that class.
_FILLER = (r"(?:\s+(?:cells?|nucle(?:us|i)|populations?|clusters?|class(?:es)?|types?|lineages?|"
           r"subsets?|subpopulations?|groups?|compartments?))*")
# A hyphenated tail that keeps the phrase about that class ("microglia-associated", "-like").
_SUFFIX = r"(?:-(?:associated|like|derived|lineage|type|related|enriched|rich|dominated|origin|specific))?"
# Words in front of a parenthesised name that make it a guess at the label, not a contrast.
_QUALIFIER = r"(?:(?:likely|probably|presumably|presumed|putative|possibly|i\.?e\.?,?|or|aka|also|called)\s+)*"
# A word right before the name that turns the phrase into a contrast.
_CONTRAST_BEFORE = frozenset({"not", "non", "vs", "vs.", "versus", "unlike", "than", "nor", "excluding",
                              "except", "without", "rather", "instead", "and", "or"})
_MARKUP = "*_`"


def _fold1(ch: str) -> str:
    """One character, lower case and without its accent; always ONE character, so positions in the
    folded text are positions in the original."""
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return base.lower() if len(base) == 1 else ch.lower()[:1] or ch


def fold(text: str) -> str:
    return "".join(_fold1(c) for c in str(text))


def _norm(name: str) -> str:
    """Letters only, as the curated references normalise names ("Müller glia" -> "mullerglia")."""
    return re.sub(r"[^a-z]", "", fold(name))


def _usable(label: str) -> bool:
    text = str(label).strip()
    return len(text) >= 2 and any(c.isalpha() for c in text)


def _letters(label: str) -> str:
    return re.sub(r"[^A-Za-z]", "", str(label))


def _abbreviation(label: str) -> bool:
    """MG, RGC, RPE: two to five capital letters and little else."""
    letters = _letters(label)
    return 2 <= len(letters) <= 5 and letters.isupper() and len(str(label)) <= 6


def _initials(name: str) -> str:
    return "".join(w[0] for w in re.findall(r"[a-z]+", fold(name))).upper()


def dataset_labels(dataset_result: "dict[str, Any] | None") -> "tuple[str, list[str]]":
    """``(label column, its levels)`` from the dataset profile; ``('', [])`` when it names none."""
    if not isinstance(dataset_result, dict):
        return "", []
    dba = dataset_result.get("design_by_arm")
    dba = dba if isinstance(dba, dict) else {}
    column = str(dba.get("label_column") or "")
    table = dba.get("cells_by_label_and_arm")
    labels = [str(k) for k in table] if isinstance(table, dict) else []
    if not labels and column:
        cats = (dataset_result.get("obs_categoricals") or {}).get(column)
        if isinstance(cats, dict):
            labels = [str(v) for v in (cats.get("values") or [])]
    return column, [lab for lab in dict.fromkeys(labels) if _usable(lab)]


def reference_types(reference: "dict[str, Any] | None") -> "dict[str, list[str]]":
    """``{cell type: its aliases}`` over every species of a curated reference."""
    out: dict[str, list[str]] = {}
    species = (reference or {}).get("species") if isinstance(reference, dict) else None
    for types in (species.values() if isinstance(species, dict) else []):
        for name, spec in (types.items() if isinstance(types, dict) else []):
            aliases = spec.get("aliases", []) if isinstance(spec, dict) else []
            out.setdefault(str(name), [str(a) for a in aliases if a])
    return out


def label_glossary(labels: "Iterable[str]", reference: "dict[str, Any] | None") -> "dict[str, str]":
    """``{label: the reference cell type it stands for}``, only where exactly one type fits and no
    other label claims that type. Empty without a reference: then no label has an expansion."""
    types = reference_types(reference)
    if not types:
        return {}
    found: dict[str, str] = {}
    for label in labels:
        key = _norm(label)
        if not key:
            continue
        named = [t for t, aliases in types.items()
                 if _norm(t) == key or any(a and a in key for a in aliases)]
        if not named and _abbreviation(label):
            named = [t for t in types if _initials(t) == _letters(label).upper()]
        if len(named) == 1:
            found[label] = named[0]
    taken = Counter(found.values())
    return {label: t for label, t in found.items() if taken[t] == 1}


def _subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(c in it for c in short)


def confusable_pairs(labels: "Iterable[str]") -> "list[tuple[str, str]]":
    """Pairs of labels a writer can take for one class: an abbreviation whose letters start, and run
    in order through, another label's name — MG and Microglia."""
    labels = list(labels)
    pairs: list[tuple[str, str]] = []
    for a in labels:
        if not _abbreviation(a):
            continue
        letters = _letters(a).lower()
        for b in labels:
            nb = _norm(b)
            if b != a and not _abbreviation(b) and nb.startswith(letters[0]) and _subsequence(letters, nb):
                pairs.append((a, b))
    return pairs


def _names(label: str, glossary: "dict[str, str]", types: "dict[str, list[str]]") -> "list[str]":
    """Regex alternatives (on folded text) that name ``label``'s class: the label, its reference cell
    type, and that type's aliases as word stems."""
    words = [label, *([glossary[label]] if label in glossary else [])]
    alts = [re.escape(fold(w)) + r"(?:s|es|l|al)?" for w in words]
    alts += [re.escape(a) + r"[a-z]*" for a in types.get(glossary.get(label, ""), []) if len(a) >= 3]
    return alts


def _whole_name(alts: "list[str]") -> "re.Pattern[str]":
    """A phrase that is nothing but a name of the class (plus a cell/nucleus word)."""
    return re.compile(rf"^(?:{'|'.join(alts)}){_SUFFIX}{_FILLER}$")


def _strip_markup(text: str) -> str:
    return text.strip().strip(_MARKUP).strip()


def check_label_conflation(md: str, labels: "Iterable[str]",
                           reference: "dict[str, Any] | None" = None,
                           column: str = "") -> "tuple[str, list[str]]":
    """Remove from ``md`` each name of another class attached to a class label; ``(md, issues)``.

    ``labels`` are the dataset's classes (``dataset_labels`` plus the tools' group labels); a name of
    a class is its label, its expansion in ``reference`` and that type's aliases."""
    labels = [str(lab) for lab in dict.fromkeys(labels or []) if _usable(lab)]
    if not md or len(labels) < 2:
        return md, []
    types = reference_types(reference)
    glossary = label_glossary(labels, reference)
    whole = {lab: _whole_name(_names(lab, glossary, types)) for lab in labels}
    folded = fold(md)
    where = f" of `{column}`" if column else " of this dataset"
    edits: dict[tuple[int, int], tuple[str, str]] = {}      # span -> (replacement, issue)

    def other_class(phrase: str, label: str) -> "str | None":
        """The OTHER label ``phrase`` is wholly a name of, or None (also when it names ``label``)."""
        text = _strip_markup(phrase)
        if not text or re.search(r"\d", text) or whole[label].match(text):
            return None
        hits = [b for b in labels if b != label and whole[b].match(text)]
        return hits[0] if len(hits) == 1 else None

    for label in labels:
        esc = re.escape(label)
        # (1) "<name> (MG)": the label in parentheses after a name of another class.
        for m in re.finditer(rf"\(\s*([{_MARKUP}]*){esc}([{_MARKUP}]*)\s*\)", md):
            clause_start = max(md.rfind(ch, 0, m.start()) for ch in ",;:.!?()[]|\n") + 1
            before = md[clause_start:m.start()]
            tokens = list(re.finditer(r"\S+", before))[-6:]
            for i, tok in enumerate(tokens):
                phrase = folded[clause_start + tok.start():clause_start + tokens[-1].end()]
                other = other_class(phrase, label)
                if other is None:
                    continue
                prev = _strip_markup(folded[clause_start + tokens[i - 1].start():
                                            clause_start + tokens[i - 1].end()]) if i else ""
                if prev in _CONTRAST_BEFORE:
                    break
                lead = len(tok.group(0)) - len(tok.group(0).lstrip(_MARKUP))
                start = clause_start + tok.start() + lead
                said = md[start:m.start()].strip()
                edits.setdefault((start, m.end()), (
                    m.group(1) + label + m.group(2),
                    f"labels: the answer called class '{label}' '{said}', but '{other}' is a separate "
                    f"class{where} — written as '{label}'."))
                break
        # (2) "MG (microglia)": a name of another class in parentheses right after the label.
        for m in re.finditer(rf"(?<![\w-]){esc}([{_MARKUP}]*)(\s*\(([^()\n]{{1,80}})\))", md):
            if _strip_markup(m.group(3)) in labels:
                continue                     # "Microglia (MG)": a bare label in parentheses is the
                #                              anchor, judged by (1) from that label's side
            inner = folded[m.start(3):m.end(3)]
            guess = re.match(_QUALIFIER, inner.strip())
            other = other_class(inner.strip()[guess.end():] if guess else inner, label)
            if other is None:
                continue
            edits.setdefault((m.start(2), m.end(2)), (
                "", f"labels: the answer glossed class '{label}' as '{m.group(3).strip()}', but "
                    f"'{other}' is a separate class{where} — the gloss was removed."))

    issues: list[str] = []
    last = len(md) + 1
    for (start, end), (new, issue) in sorted(edits.items(), reverse=True):
        if end > last:                       # overlaps an edit already made further on
            continue
        md = md[:start] + new + md[end:]
        last = start
        issues.append(issue)
    return md, list(reversed(issues))


def labels_prompt(column: str, labels: "list[str]", glossary: "dict[str, str]",
                  reference_name: str = "") -> "list[str]":
    """Prompt lines for the writer: which labels the data has, which may be spelled out and how, and
    which pairs are two classes although they look alike."""
    if not labels:
        return []
    lines: list[str] = []
    where = f" (the levels of the dataset's `{column}` column)" if column else ""
    lines.append(
        f"- Each of these labels{where} is its own class: never describe one as another, as part of "
        "another, or by another's name.")
    pairs = confusable_pairs(labels)
    if pairs:
        lines.append("- Labels that look alike but are DIFFERENT classes here: "
                     + "; ".join(f"{a} is not {b}" for a, b in pairs) + ".")
    spelled = {lab: t for lab, t in glossary.items() if lab in labels and _norm(lab) != _norm(t)}
    source = f"the curated {reference_name} reference" if reference_name else "the curated reference"
    if spelled:
        lines.append(
            f"- Expansions {source} supports (use only these, or the label alone): "
            + "; ".join(f"{lab} = {t}" for lab, t in spelled.items()) + ".")
    abbreviations = [lab for lab in labels if _abbreviation(lab) and lab not in glossary]
    if abbreviations:
        lines.append(
            "- No reference spells out " + ", ".join(abbreviations)
            + ": write each as it is, and do not expand it from memory.")
    return lines
