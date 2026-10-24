"""Which lines of a step's code call a skill's own functions, found in code.

A plan step the approved plan gave a skill is judged on whether its code used the skill's method
(``research_lab._critic``), and the skill's version is counted once per step whose code ran that
method (``skills_registry``). Both rest on this module.

A skill's own functions are read from its Python files (tests left out) and the Python blocks of
its SKILL.md:

* its calls into the libraries it imports, resolved through its imports (``sc.tl.score_genes`` is
  ``scanpy.tl.score_genes``). Left out: the standard library, reading and writing files
  (``read_h5ad``, ``write``), and the general data and plotting libraries nearly every snippet uses
  (numpy, pandas, matplotlib, seaborn, anndata), which count only for a skill that calls nothing
  else;
* the functions it defines, which a step that copies the skill's helpers calls by name;
* the programs it runs through ``subprocess`` or ``os.system`` (``bcftools``), apart from shells
  and interpreters.

A step's call matches on the library and the function's name, so ``import scanpy as scp`` and
``from scanpy.tl import score_genes`` match ``sc.tl.score_genes``. A program matches where the
step's code names it in a string.
"""
from __future__ import annotations

import ast
import bisect
import re
import sys
from functools import lru_cache
from typing import Any, Iterable

#: Libraries nearly every snippet calls for plumbing: a skill's calls into them say little about its
#: method, so they are its functions only when it calls nothing more specific.
_GENERAL_LIBRARIES = frozenset({"numpy", "pandas", "matplotlib", "seaborn", "anndata"})
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
_FILE_IO = re.compile(r"(?:read|write|save|load)(?:_|$)")
_SUBPROCESS = frozenset({"subprocess.run", "subprocess.call", "subprocess.check_call",
                         "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen"})
#: Programs that only run another program: the program they run is the method, not them.
_RUNNERS = frozenset({"bash", "sh", "zsh", "env", "time", "nohup", "xargs", "python", "python3",
                      "Rscript", "conda", "mamba", "micromamba", "pip", "srun", "apptainer",
                      "singularity"})
_PROGRAM = " (program)"
_FENCE = re.compile(r"```(?:python|py)[ \t]*\n(.*?)```", re.DOTALL)
_GAP = "# [... {n} lines left out ...]"

#: The Critic's budget for a skill step's code: every script in full up to this many characters in
#: all; past it, the short ones in full and the long ones cut to equal shares (``fit_scripts``).
REVIEW_CODE_CHARS = 24_000
#: At most this many scripts are shown, those that call the skill's functions first.
REVIEW_MAX_SCRIPTS = 12


# --- reading code ---------------------------------------------------------------------------------

def _parse(code: str) -> "ast.Module | None":
    try:
        return ast.parse(code)
    except (SyntaxError, ValueError):
        return None


def _aliases(tree: ast.AST) -> dict[str, str]:
    """``{name in the code: what it is}`` from the code's imports (``sc`` -> ``scanpy``)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    names[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    names[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                if a.name != "*":
                    names[a.asname or a.name] = f"{node.module}.{a.name}"
    return names


def _qualified(call: ast.Call, aliases: dict[str, str]) -> "str | None":
    """``scanpy.tl.score_genes`` for ``sc.tl.score_genes(...)``; a bare name for ``helper(...)``;
    ``None`` for a method of an object (``adata.write``), which no import says anything about."""
    parts: list[str] = []
    func: ast.AST = call.func
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    parts.append(func.id)
    parts.reverse()
    head = aliases.get(parts[0])
    if head is None:
        return parts[0] if len(parts) == 1 else None
    return ".".join([head, *parts[1:]])


def _leading_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        text = ""
        for part in node.values:
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                break
            text += part.value
        return text
    return ""


def _programs(call: ast.Call, aliases: dict[str, str]) -> list[str]:
    """The program a ``subprocess`` / ``os.system`` call runs, unless it is a shell or interpreter."""
    if _qualified(call, aliases) not in _SUBPROCESS or not call.args:
        return []
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        first = first.elts[0]
    words = _leading_text(first).split()
    name = words[0].rsplit("/", 1)[-1] if words else ""
    if not re.fullmatch(r"[A-Za-z][\w.+-]*", name) or name in _RUNNERS:
        return []
    return [name]


def _key(function: str) -> "tuple[str, str]":
    """What a call is matched on: the library and the function's name, the program, or the name of
    a function the skill defines."""
    if function.endswith(_PROGRAM):
        return ("$", function[: -len(_PROGRAM)])
    if "." in function:
        return (function.split(".", 1)[0], function.rsplit(".", 1)[1])
    return ("", function)


# --- a skill's own functions ------------------------------------------------------------------------

def _skill_code(skill: Any) -> "tuple[str, ...]":
    files = getattr(skill, "files", None) or {}
    texts = [text for rel, text in sorted(files.items())
             if isinstance(text, str) and rel.endswith(".py") and not rel.startswith("tests/")]
    texts += _FENCE.findall(getattr(skill, "doc", "") or "")
    return tuple(texts)


@lru_cache(maxsize=256)
def _functions_of(texts: "tuple[str, ...]") -> "tuple[str, ...]":
    specific: dict[str, int] = {}
    general: dict[str, int] = {}

    def note(found: "dict[str, int]", name: str, where: int) -> None:
        found[name] = min(found.get(name, where), where)

    for n, text in enumerate(texts):
        tree = _parse(text)
        if tree is None:
            continue
        aliases = _aliases(tree)
        for node in ast.walk(tree):
            # Ordered by where the skill first uses each, file by file.
            where = n * 1_000_000 + getattr(node, "lineno", 0)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                note(specific, node.name, where)
            elif isinstance(node, ast.Call):
                for program in _programs(node, aliases):
                    note(specific, program + _PROGRAM, where)
                name = _qualified(node, aliases)
                if not name or "." not in name:
                    continue
                top, last = name.split(".", 1)[0], name.rsplit(".", 1)[1]
                if top in _STDLIB or _FILE_IO.match(last):
                    continue
                note(general if top in _GENERAL_LIBRARIES else specific, name, where)
    found = specific or general
    return tuple(sorted(found, key=lambda f: (found[f], f)))


def skill_functions(skill: Any) -> "tuple[str, ...]":
    """The skill's own functions (see the module docstring), in the order its code first uses them:
    ``("scanpy.tl.score_genes",)``, ``("bcftools (program)", "_count")``. Empty when its code calls
    nothing that can be told apart, such as a skill that is all shell commands."""
    return _functions_of(_skill_code(skill))


# --- the step's code ---------------------------------------------------------------------------------

def _statement_lines(node: ast.AST, parents: "dict[ast.AST, ast.AST]") -> "tuple[int, int]":
    """The lines (1-based, inclusive) of the statement ``node`` is in; for a compound statement (an
    ``if``, a ``for``, a ``def``), the lines of the header part that holds ``node``."""
    stmt: "ast.AST | None" = node
    while stmt is not None and not isinstance(stmt, ast.stmt):
        stmt = parents.get(stmt)
    if stmt is None:
        return node.lineno, getattr(node, "end_lineno", None) or node.lineno
    if hasattr(stmt, "body"):
        return (min(stmt.lineno, node.lineno),
                getattr(node, "end_lineno", None) or node.lineno)
    return stmt.lineno, stmt.end_lineno or stmt.lineno


def _program_in(program: str, text: str) -> bool:
    return re.search(rf"(?<![\w.-]){re.escape(program)}(?![\w-])", text) is not None


def _matches(code: str, functions: "Iterable[str]") -> "list[tuple[int, int, str]]":
    """``(first line, last line, function)`` for each statement of ``code`` that calls one of the
    skill's ``functions`` (1-based lines). Code that does not parse (a shell command) is searched
    line by line for the functions' names followed by ``(`` and for the programs."""
    wanted = {_key(f): f for f in functions}
    if not wanted or not code:
        return []
    tree = _parse(code)
    if tree is None:
        return _matches_by_text(code, wanted)
    aliases = _aliases(tree)
    programs = [(k[1], f) for k, f in wanted.items() if k[0] == "$"]
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    out: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        hits: list[str] = []
        if isinstance(node, ast.Call):
            name = _qualified(node, aliases)
            hit = wanted.get(_key(name)) if name else None
            if hit is not None:
                hits.append(hit)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and programs:
            hits += [f for program, f in programs if _program_in(program, node.value)]
        for f in hits:
            first, last = _statement_lines(node, parents)
            out.append((first, last, f))
    return sorted(set(out))


def _matches_by_text(code: str, wanted: "dict[tuple[str, str], str]") -> "list[tuple[int, int, str]]":
    out = []
    for n, line in enumerate(code.splitlines(), 1):
        for (kind, name), f in wanted.items():
            found = (_program_in(name, line) if kind == "$"
                     else re.search(rf"(?<![\w]){re.escape(name)}\s*\(", line) is not None)
            if found:
                out.append((n, n, f))
    return out


def calls_to(code: str, functions: "Iterable[str]") -> "list[str]":
    """Which of the skill's ``functions`` ``code`` calls, in the skill's order."""
    functions = tuple(functions)
    found = {f for _first, _last, f in _matches(code, functions)}
    return [f for f in functions if f in found]


def _gap_cost(run: int) -> int:
    return len(_GAP.format(n=run)) + 1 if run > 0 else 0


def excerpt(code: str, functions: "Iterable[str]", limit: int) -> str:
    """``code`` itself when it fits in ``limit`` characters; else the lines that call the skill's
    ``functions``, then the lines nearest them, the first lines and the last lines, added evenly
    until ``limit`` is reached, with ``# [... N lines left out ...]`` where lines are left out."""
    if len(code) <= limit:
        return code
    cap = max(80, limit // 2)
    lines = [ln if len(ln) <= cap else ln[:cap] + "  # [line cut]" for ln in code.splitlines()]
    n = len(lines)
    key: set[int] = set()
    for first, last, _f in _matches(code, functions):
        key.update(range(first - 1, min(last, n)))
    anchors = sorted(key)

    def priority(i: int) -> "tuple[int, int]":
        near = n
        if anchors:
            j = bisect.bisect_left(anchors, i)
            near = min(abs(i - anchors[k]) for k in (j - 1, j) if 0 <= k < len(anchors))
        return (min(near, i + 1, n - i), i)

    chosen: list[int] = []
    size = _gap_cost(n) - 1          # nothing chosen: one gap, no trailing newline
    for i in sorted(key) + sorted((i for i in range(n) if i not in key), key=priority):
        j = bisect.bisect_left(chosen, i)
        before = chosen[j - 1] if j else -1
        after = chosen[j] if j < len(chosen) else n
        grown = (size + len(lines[i]) + 1 - _gap_cost(after - before - 1)
                 + _gap_cost(i - before - 1) + _gap_cost(after - i - 1))
        if grown <= limit:
            chosen.insert(j, i)
            size = grown
    keep = set(chosen)
    out: list[str] = []
    i = 0
    while i < n:
        if i in keep:
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < n and j not in keep:
            j += 1
        out.append(_GAP.format(n=j - i))
        i = j
    return "\n".join(out)


def _shares(lengths: "list[int]", budget: int) -> "list[int]":
    """Each script's limit: the short ones whole, the long ones an equal share of what is left."""
    limits = [0] * len(lengths)
    left = budget
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for done, i in enumerate(order):
        share = left // (len(order) - done)
        limits[i] = min(lengths[i], share)
        left -= limits[i]
    return limits


def fit_scripts(steps: "list[dict[str, Any]]", functions: "Iterable[str]", *,
                succeeded: "Any" = None, budget: int = REVIEW_CODE_CHARS,
                max_scripts: int = REVIEW_MAX_SCRIPTS) -> "tuple[list[dict[str, Any]], int]":
    """The code a step ran, for the Critic: ``(scripts, how many were not shown)``.

    ``steps`` are the harness's step records. Each ``run_code`` script and ``run_in_environment``
    command becomes ``{"tool_call", "tool", "ok", "chars", "shown", "calls_skill_functions",
    "code"}``: ``tool_call`` is its place among the step's tool calls (``finish`` left out), the
    same order as the Critic's ``tool_results``; ``shown`` is ``"full"`` or ``"excerpt"``
    (``excerpt``). ``succeeded(step) -> bool`` decides ``ok`` (default: the harness's ``ok``)."""
    functions = tuple(functions)
    scripts: list[dict[str, Any]] = []
    number = 0
    for s in steps:
        if s.get("tool") == "finish":
            continue
        number += 1
        tool = s.get("tool")
        if tool not in ("run_code", "run_in_environment"):
            continue
        args = s.get("args") or {}
        code = str(args.get("code" if tool == "run_code" else "command") or "")
        if not code.strip():
            continue
        scripts.append({"tool_call": number, "tool": tool,
                        "ok": bool(succeeded(s) if succeeded is not None else s.get("ok")),
                        "chars": len(code), "calls_skill_functions": calls_to(code, functions),
                        "code": code})
    if len(scripts) > max_scripts:
        first = [s for s in scripts if s["calls_skill_functions"]][:max_scripts]
        rest = [s for s in scripts if not s["calls_skill_functions"]][: max_scripts - len(first)]
        kept = {id(s) for s in first + rest}
        shown = [s for s in scripts if id(s) in kept]
    else:
        shown = scripts
    for s, limit in zip(shown, _shares([s["chars"] for s in shown], budget)):
        s["code"] = excerpt(s["code"], functions, limit)
        s["shown"] = "full" if limit >= s["chars"] else "excerpt"
    order = ("tool_call", "tool", "ok", "chars", "shown", "calls_skill_functions", "code")
    return [{k: s[k] for k in order} for s in shown], len(scripts) - len(shown)
