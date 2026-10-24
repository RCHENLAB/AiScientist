"""A statistic printed from the first rows of a matrix is a property of those rows, not of the file.

Prod run 74873590ca7b (2026-10-23), step 2: to check that ``.raw`` held log-normalised values, the
Scientist's ``run_code`` took ``M[:300]`` (the first 300 nuclei), then the first 200,000 stored
values of those, and printed ``print('raw max', float(v.max()), ...)`` -> "raw max 7.123". Its answer
said ".raw holding log-normalised values (max 7.12, non-integer)"; step 1 had printed X's maximum over
the whole matrix, 8.48; and the final answer reported "X max 8.48 vs .raw max 7.12" as an unresolved
provenance discrepancy, in its QC section, its Limitations and its Conclusions. ``run_scanpy_qc`` sets
``adata.raw = adata`` right after log1p, so the two are one matrix; 7.12 was the maximum of a sample.
Run 7f2955e8e834 (step 12) did the same: ``x[:300]`` and 200,000 values -> "`.raw` max 6.845". Both
copied the sampled integer-count guard of the signature-scoring skill and added a print the skill
does not have.

This reads the snippet before its result goes back to the Scientist (``sampled_statistics``): a
name assigned from a leading slice (``X[:300]``, ``v.ravel()[:200000]``, ``X[:100, :100]``) or from
an expression that uses such a name is a sample, and a ``print`` that computes max / min / mean /
median / sum / std / var / quantile of a sample gets a warning on the result: what the value was
computed on, and that it is not the whole matrix's. The Scientist reads it before writing its
answer, and the Critic and the writer see it among the result's warnings. Narrow on purpose: only a
LEADING slice with a literal bound (``[:N]``, ``[:min(N, …)]``) is a sample; a column subset by
name, ``.head()`` of a sorted table, a random draw (a bootstrap is meant to be one) and a slice that
is only printed, not summarised (``var_names[:5]``), are not judged.
"""

from __future__ import annotations

import ast
import re
from typing import Any

_STATS = frozenset({"max", "min", "mean", "median", "sum", "std", "var", "quantile", "percentile",
                    "nanmax", "nanmin", "nanmean", "nanmedian", "nansum", "amax", "amin", "ptp"})
_MAX_WARNINGS = 3


def _bound(node: "ast.AST | None") -> "int | None":
    """The literal upper bound of a leading slice: ``300`` in ``[:300]`` and ``[:min(500, n)]``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "min"):
        literal = [_bound(a) for a in node.args]
        literal = [b for b in literal if b is not None]
        return min(literal) if literal else None
    return None


# A slice of a RANKING is its top N, not a sample of the data.
_RANKED = frozenset({"sort_values", "sort_index", "sorted", "nlargest", "nsmallest", "argsort",
                     "most_common", "value_counts"})


def _leading_slice(node: ast.AST) -> "int | None":
    """``N`` when ``node`` is ``x[:N]`` or ``x[:N, …]`` / ``x[…, :N]`` (start omitted or 0)."""
    if not isinstance(node, ast.Subscript):
        return None
    base = node.value.value if isinstance(node.value, ast.Attribute) and node.value.attr in (
        "iloc", "loc") else node.value
    if isinstance(base, ast.Call):
        fn = base.func.attr if isinstance(base.func, ast.Attribute) else getattr(base.func, "id", "")
        if fn in _RANKED:
            return None
    parts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    for part in parts:
        if isinstance(part, ast.Slice) and part.step is None and (
                part.lower is None or (isinstance(part.lower, ast.Constant) and part.lower.value == 0)):
            n = _bound(part.upper)
            if n is not None:
                return n
    return None


# A print whose own words already name the sample ("raw sample max in first 100 rows:") is read right.
_SCOPE_WORDS = re.compile(r"\b(?:sample[ds]?|subsample[ds]?|first|subset|head|preview|top)\b", re.I)


def _says_sample(call: ast.Call) -> bool:
    """Does the printed text itself say the value comes from a sample?"""
    for sub in ast.walk(call):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and _SCOPE_WORDS.search(sub.value):
            return True
    return False


def _target_names(target: ast.AST) -> "list[str]":
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for elt in target.elts for n in _target_names(elt)]
    return []


class _Reader(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.sampled: dict[str, str] = {}      # name -> how it was sampled ("`M[:300]`, …")
        self.warnings: list[str] = []

    # --- what is a sample ------------------------------------------------------------------------
    def _how(self, node: ast.AST) -> str:
        """How ``node`` is sampled ('' when it is not): its own leading slices and the sampled names
        it uses, in the order they appear."""
        found: list[str] = []
        for sub in ast.walk(node):
            n = _leading_slice(sub)
            if n is not None:
                text = ast.get_source_segment(self.source, sub) or ""
                if len(text) > 30:
                    text = "…[" + (ast.get_source_segment(self.source, sub.slice) or f":{n}") + "]"
                found.append(f"`{text}`")
            elif isinstance(sub, ast.Name) and sub.id in self.sampled:
                found.append(self.sampled[sub.id])
        return ", ".join(dict.fromkeys(found))

    def visit_Assign(self, node: ast.Assign) -> None:
        how = self._how(node.value)
        for target in node.targets:
            for name in _target_names(target):
                if how:
                    self.sampled[name] = how
                else:
                    self.sampled.pop(name, None)     # reassigned from the whole thing
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        how = self._how(node.value)
        if how and isinstance(node.target, ast.Name):
            self.sampled[node.target.id] = how
        self.generic_visit(node)

    # --- a statistic of a sample, printed ---------------------------------------------------------
    def _stat_of_sample(self, node: ast.AST) -> "tuple[str, str] | None":
        """``(statistic, how sampled)`` for the first statistic in ``node`` computed on a sample."""
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else "")
            if name not in _STATS:
                continue
            if isinstance(func, ast.Attribute) and not (
                    isinstance(func.value, ast.Name) and func.value.id in ("np", "numpy")):
                subject: list[ast.AST] = [func.value]       # v.max()
            else:
                subject = list(sub.args[:1])                # np.max(v), max(v)
            how = ", ".join(filter(None, (self._how(s) for s in subject)))
            if how:
                return name, how
        return None

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "print" and not _says_sample(node):
            hit = self._stat_of_sample(node)
            if hit and len(self.warnings) < _MAX_WARNINGS:
                stat, how = hit
                line = (ast.get_source_segment(self.source, node) or "print(...)").splitlines()[0]
                line = line if len(line) <= 120 else line[:117] + "..."
                self.warnings.append(
                    f"SAMPLED STATISTIC: `{line}` prints the {stat} of a SAMPLE ({how}), not of the "
                    f"whole matrix or table. Do not report it as the file's or the matrix's {stat}: "
                    "say what it was computed on, or compute it on the whole object.")
        self.generic_visit(node)


def sampled_statistics(code: str) -> "list[str]":
    """Warnings for each ``print`` in ``code`` that reports a statistic of a leading slice."""
    try:
        tree = ast.parse(code or "")
    except (SyntaxError, ValueError):
        return []
    reader = _Reader(code)
    # Statements in order, so a name is a sample only after the line that made it one.
    for stmt in tree.body:
        reader.visit(stmt)
    return reader.warnings


def annotate(result: Any, code: str) -> Any:
    """``result`` (a ``run_code`` result dict) with the sampled-statistic warnings added."""
    if not isinstance(result, dict) or not result.get("stdout"):
        return result
    found = sampled_statistics(code)
    if not found:
        return result
    existing = result.get("warnings")
    if isinstance(existing, list):
        result["warnings"] = [*existing, *found]
    elif existing:
        result["warnings"] = [str(existing), *found]
    else:
        result["warnings"] = found
    return result
