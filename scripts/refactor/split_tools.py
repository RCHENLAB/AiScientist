#!/usr/bin/env python3
"""Reorganise ``src/bioagent/tools`` into one folder per model-callable tool.

This is a program, not a diff, on purpose. The tools are being fixed on ``main`` while the
repository split is prepared on a branch, and a hand-made move of 2.7k-line files into twenty
folders would conflict with every one of those fixes. Instead this script DERIVES the new layout
from whatever the old files contain when it runs, so the procedure at merge time is: take main's
versions of the old files, re-run the script, run the tests. See ``docs/architecture/REPO_SPLIT.md``.

What it does
------------
* **Whole-file moves** (``git mv``, so history follows): a module that holds one tool becomes
  ``tools/<tool>/tool.py`` (or keeps a descriptive name inside the folder, e.g. ``offline.py``).
* **Splits**: ``scrna_pack.py`` + ``scrna_advanced.py`` (eleven tools) and ``phenotype_dx.py`` (two
  tools) are cut by symbol. Every top-level function/constant is placed by who uses it: reachable
  from exactly one tool -> that tool's ``tool.py``; from several (or from none) -> the shared
  ``tools/_lib/<line>.py``. Each tool's ``HarnessTool(...)`` record is lifted out of the old
  ``*_catalog()`` list into a ``make_tool()`` in its own folder. Source text is copied verbatim
  (comments included); only import statements are rewritten.
* **Importers** across src/, tests/, scripts/ and experiments/ are rewritten to the defining module:
  ``from X import a, b`` is split by destination, ``mod.attr`` on a split module is re-pointed, and a
  ``monkeypatch.setattr(mod, "helper", ...)`` on a shared helper is fanned out to every module that
  imports the helper by name (that is where the tools look it up).
* ``tools/api.py``'s name table is re-pointed the same way. ``TOOL.md`` files are NOT written here:
  they are hand-written documentation and survive a re-run untouched.

Usage::

    python scripts/refactor/split_tools.py --plan     # print the placement, change nothing
    python scripts/refactor/split_tools.py --apply    # perform it (needs a clean git tree)
    python scripts/refactor/split_tools.py --regenerate-from main   # after merging main: rebuild
        # the split modules from main's old files (see docs/architecture/REPO_SPLIT.md)

The script refuses to guess: a symbol defined twice in one destination, an import it cannot
resolve, or a module-object reference it cannot map is an error, not a silent skip.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import textwrap
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
TOOLS = SRC / "bioagent" / "tools"
PKG = "bioagent.tools"

# ------------------------------------------------------------------------------------------------
# The plan. Everything below is derived from these tables.

# Whole-file moves: old module -> new module (inside the tools package).
MODULE_MOVES: dict[str, str] = {
    "dataset_inspect": "inspect_dataset.tool",
    "literature_search": "literature_search.tool",
    "paperqa_search": "deep_literature.tool",
    "variant_annotation": "annotate_variants.tool",
    "vcf_offline": "annotate_variants.offline",
    "ird_annotate": "annotate_variants.ird_annotate",
    "ird_prioritize": "annotate_variants.ird_prioritize",
    "phenotype_evidence": "diagnose_disease.evidence",
    "schematic": "make_schematic.tool",
    "scgpt_annotate": "scgpt_annotate.tool",
}
# Platform modules that left tools/ in an earlier, hand-made commit (they are not tools): old module
# -> new ABSOLUTE module. Listed so that code merged in from main that still imports them by the old
# name is re-pointed, and so the coverage check knows them.
PLATFORM_MOVES: dict[str, str] = {
    "report": "bioagent.reporting.report",
    "research_bundle": "bioagent.reporting.research_bundle",
    "visual_review": "bioagent.reporting.visual_review",
    "vlreview_run": "bioagent.reporting.vlreview_run",
    "literature_references": "bioagent.reporting.literature_references",
    "hpc_shell": "bioagent.hpc.shell",
}
# Whole-package moves (directory renames): old package -> new package. Submodule renames inside.
PACKAGE_MOVES: dict[str, tuple[str, dict[str, str]]] = {
    "hpo_terms": ("map_phenotype_to_hpo", {"mapper": "tool"}),
}


@dataclass
class Split:
    """A module that holds several tools and is cut by symbol."""

    sources: list[str]                       # old modules (bioagent.tools.<name>), analysed together
    lib: str                                 # shared module for symbols several tools use
    roots: dict[str, list[str]]              # tool -> the symbols that ARE the tool
    catalogs: dict[str, str] = field(default_factory=dict)   # old module -> its *_catalog() function
    factories: dict[str, str] = field(default_factory=dict)  # tool -> existing factory to keep (no catalog entry)
    drop: set[str] = field(default_factory=set)               # symbols replaced by the catalog
    # composite tool -> the tools it composes. Code a composite shares with one of its components
    # belongs to the component (the composite imports it from there), not to the shared lib.
    components: dict[str, list[str]] = field(default_factory=dict)


SPLITS = [
    Split(
        sources=["scrna_pack", "scrna_advanced"],
        lib="_lib.scrna",
        roots={
            "run_scanpy_qc": ["run_scanpy_qc"],
            "run_clustering": ["run_clustering"],
            "run_de": ["run_de"],
            "run_enrichment": ["run_enrichment"],
            "run_depth_matched_de": ["run_depth_matched_de"],
            "run_gsea_prerank": ["run_gsea_prerank"],
            "run_doublet_detection": ["run_doublet_detection"],
            "run_integration": ["run_integration"],
            "run_pseudobulk_de": ["run_pseudobulk_de"],
            "run_composition": ["run_composition"],
            "run_marker_annotation": ["run_marker_annotation"],
        },
        catalogs={"scrna_pack": "scrna_catalog", "scrna_advanced": "scrna_advanced_catalog"},
        # the catalog functions are replaced by tools/catalog.py; ExecutorFn is an unused alias that
        # the two modules define differently (and its comment describes a cycle sdk.py removed)
        drop={"scrna_catalog", "scrna_advanced_catalog", "ExecutorFn"},
    ),
    Split(
        sources=["phenotype_dx"],
        lib="_lib.phenotype",
        roots={
            "run_lirical": ["make_phenotype_differential_tool", "run_lirical"],
            "diagnose_disease": ["make_diagnose_disease_tool", "diagnose"],
        },
        components={"diagnose_disease": ["run_lirical"]},
    ),
]

# Modules that stay where they are (job entry points baked into the images' runscripts, runtime,
# shared data packages, the contract). Listed so an unplanned module is an error.
STAY = {"__init__", "sdk", "api", "catalog", "manifest", "datasets", "execution", "run_deps",
        "scrna_cli", "variant_cli", "phenotype_cli", "paperqa_cli", "gene_panels"}

FUTURE = "from __future__ import annotations"

# Exact text edits for files that assert the OLD layout (a path or module name in a test).
# Applied after the rewrite; a missing `old` text is reported, not ignored.
POST_EDITS: list[tuple[str, str, str]] = [
    ("tests/test_tool_source.py",
     '    assert out["module"] == "bioagent.tools.scrna_pack"\n'
     '    assert out["file"].endswith("scrna_pack.py") and out["first_line"] > 0\n',
     '    assert out["module"] == "bioagent.tools.run_de.tool"\n'
     '    assert out["file"].endswith("run_de/tool.py") and out["first_line"] > 0\n'),
    ("tests/test_environment_manifest.py",
     '    assert "src/bioagent/tools/scrna_pack.py:" in md\n',
     '    assert "src/bioagent/tools/run_de/tool.py:" in md\n'),
]

# Exact text edits for GENERATED modules: code that locates files relative to ``__file__`` and so
# changes meaning when it moves. (new module, old text, new text)
GENERATED_EDITS: list[tuple[str, str, str]] = [
    ("_lib.scrna",
     '''    """Where the local ``.gmt`` gene-set files live. ``BIOAGENT_GENESETS_DIR`` overrides;
    otherwise a ``genesets/`` dir next to this module — which rides along with the dfs3b
    source bind, so the network-OFF analysis container finds it with no extra plumbing."""
    d = os.environ.get("BIOAGENT_GENESETS_DIR")
    return Path(d) if d else Path(__file__).resolve().parent / "genesets"''',
     '''    """Where the local ``.gmt`` gene-set files live. ``BIOAGENT_GENESETS_DIR`` overrides;
    otherwise ``tools/genesets/`` — which rides along with the dfs3b source bind, so the
    network-OFF analysis container finds it with no extra plumbing. (This module sits in
    ``tools/_lib/``; the deploy keeps the .gmt files in ``tools/genesets/``.)"""
    d = os.environ.get("BIOAGENT_GENESETS_DIR")
    return Path(d) if d else Path(__file__).resolve().parents[1] / "genesets"'''),
    ("_lib.scrna",
     "same graceful-degrade contract as ``tools/report.py`` for pandoc",
     "same graceful-degrade contract as ``reporting/report.py`` for pandoc"),
]

# Symbols the split drops but that importers still use: (old module, name) -> new module.
REPLACED = {
    ("scrna_pack", "scrna_catalog"): "catalog",
}

# ------------------------------------------------------------------------------------------------
# Source analysis


def _abs(mod: str) -> str:
    return mod if mod.startswith("bioagent.") else f"{PKG}.{mod}"


def module_path(mod: str) -> Path:
    """File of an absolute module name (``bioagent.tools.x.y`` -> src/bioagent/tools/x/y.py)."""
    parts = mod.split(".")
    p = SRC.joinpath(*parts)
    if p.is_dir():
        return p / "__init__.py"
    return p.with_suffix(".py")


@dataclass
class Seg:
    node: ast.stmt
    start: int                     # 0-based first line (comments directly above included)
    end: int                       # 0-based exclusive
    names: set[str]                # names it binds at module level
    refs: set[str]                 # names it references
    module: str                    # old module it came from


def _names_bound(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        out: set[str] = set()
        for t in node.targets:
            out |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        return out
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in node.names}
    return set()


def _refs(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.arg) and n.annotation is not None:
            out |= _string_annotation_refs(n.annotation)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None:
            out |= _string_annotation_refs(n.returns)
        elif isinstance(n, ast.AnnAssign):
            out |= _string_annotation_refs(n.annotation)
    return out


def _string_annotation_refs(ann: ast.AST) -> set[str]:
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            return {n.id for n in ast.walk(ast.parse(ann.value, mode="eval")) if isinstance(n, ast.Name)}
        except SyntaxError:
            return set()
    return set()


def segments(mod: str, text: str) -> tuple[str, list[Seg]]:
    """(module docstring source or "", top-level segments) with exact line ranges."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    body = list(tree.body)
    doc = ""
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        doc = "".join(lines[body[0].lineno - 1: body[0].end_lineno])
        prev_end = body[0].end_lineno
        body = body[1:]
    else:
        prev_end = 0
    segs: list[Seg] = []
    for node in body:
        first = node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
            first = min(d.lineno for d in node.decorator_list)
        # attach the comment block directly above (no blank line in between) to this statement
        start = first - 1
        while start - 1 >= prev_end and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        segs.append(Seg(node, start, node.end_lineno, _names_bound(node), _refs(node), mod))
        prev_end = node.end_lineno
    return doc, segs


# ------------------------------------------------------------------------------------------------
# Placement for the split modules


@dataclass
class Placement:
    dest: dict[tuple[str, str], str]           # (old module, symbol) -> new module (relative to tools)
    tool_of: dict[str, str]                    # new module -> tool name (None for lib)
    catalog_elems: dict[str, tuple[str, ast.Call]]   # tool -> (old module, HarnessTool(...) call)
    duplicates: set[tuple[str, str]] = field(default_factory=set)   # identical re-definitions to skip


def _catalog_elements(segs: list[Seg], fn: str, text: str) -> dict[str, ast.Call]:
    for s in segs:
        if isinstance(s.node, ast.FunctionDef) and s.node.name == fn:
            ret = [n for n in ast.walk(s.node) if isinstance(n, ast.Return)]
            if not ret or not isinstance(ret[0].value, ast.List):
                raise SystemExit(f"{fn}: expected `return [HarnessTool(...), ...]`")
            out = {}
            for elt in ret[0].value.elts:
                if isinstance(elt, ast.Starred):
                    continue                          # *other_catalog() — handled by its own module
                if not (isinstance(elt, ast.Call) and getattr(elt.func, "id", "") == "HarnessTool"):
                    raise SystemExit(f"{fn}: unexpected catalog element at line {elt.lineno}")
                first = elt.args[0] if elt.args else next(
                    (k.value for k in elt.keywords if k.arg == "name"), None)
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    raise SystemExit(f"{fn}: catalog element at line {elt.lineno} has no literal name")
                out[first.value] = elt
            return out
    raise SystemExit(f"catalog function {fn} not found")


def place(split: Split, texts: dict[str, str]) -> Placement:
    seg_by_mod = {m: segments(m, texts[m])[1] for m in split.sources}
    # symbol table across the split's modules; cross-module `from .scrna_pack import x` resolves here
    defined: dict[str, tuple[str, Seg]] = {}
    duplicates: dict[tuple[str, str], str] = {}    # (module, name) -> module of the identical original
    for m, segs in seg_by_mod.items():
        lines = texts[m].splitlines(keepends=True)
        for s in segs:
            if isinstance(s.node, (ast.Import, ast.ImportFrom)):
                continue
            for n in s.names:
                if n in split.drop:
                    continue
                if n in defined and defined[n][0] != m:
                    m0, s0 = defined[n]
                    t0 = "".join(texts[m0].splitlines(keepends=True)[s0.node.lineno - 1:s0.node.end_lineno])
                    t1 = "".join(lines[s.node.lineno - 1:s.node.end_lineno])
                    if t0.strip() != t1.strip():
                        raise SystemExit(f"{n} is defined differently in {m0} and {m}; rename one first")
                    duplicates[(m, n)] = m0
                    continue
                defined[n] = (m, s)

    elems: dict[str, tuple[str, ast.Call]] = {}
    for m, fn in split.catalogs.items():
        for tool, call in _catalog_elements(seg_by_mod[m], fn, texts[m]).items():
            elems[tool] = (m, call)
    for tool in split.roots:
        if split.catalogs and tool not in elems:
            raise SystemExit(f"no catalog entry for {tool}")
    extra = set(elems) - set(split.roots)
    if extra:
        raise SystemExit(f"catalog has tools the plan does not know: {sorted(extra)}")

    def closure(start: set[str]) -> set[str]:
        seen: set[str] = set()
        todo = [n for n in start if n in defined]
        while todo:
            n = todo.pop()
            if n in seen or n in split.drop:
                continue
            seen.add(n)
            todo.extend(r for r in defined[n][1].refs if r in defined and r not in seen)
        return seen

    users: dict[str, set[str]] = defaultdict(set)
    for tool, roots in split.roots.items():
        start = set(roots)
        if tool in elems:
            start |= {n.id for n in ast.walk(elems[tool][1]) if isinstance(n, ast.Name)}
        for n in closure(start):
            users[n].add(tool)

    dest: dict[tuple[str, str], str] = {}
    tool_of: dict[str, str] = {}
    for n, (m, _s) in defined.items():
        if n in split.drop:
            continue
        u = users.get(n, set())
        if len(u) > 1:
            owners = [c for c in u if all(x == c or c in split.components.get(x, []) for x in u)]
            if len(owners) == 1:
                u = {owners[0]}
        if len(u) == 1:
            tool = next(iter(u))
            d = f"{tool}.tool"
            tool_of[d] = tool
        else:
            d = split.lib
        dest[(m, n)] = d
    for (m, n), m0 in duplicates.items():
        dest[(m, n)] = dest[(m0, n)]
    # names one split module imports from another (``from .scrna_pack import _p`` in scrna_advanced):
    # ``scrna_advanced._p`` must resolve to wherever scrna_pack's ``_p`` went
    for m, segs in seg_by_mod.items():
        for s in segs:
            if isinstance(s.node, ast.ImportFrom) and s.node.level == 1 and s.node.module in split.sources:
                for a in s.node.names:
                    if (s.node.module, a.name) in dest:
                        dest.setdefault((m, a.asname or a.name), dest[(s.node.module, a.name)])
    for tool in split.roots:
        tool_of[f"{tool}.tool"] = tool
    return Placement(dest, tool_of, elems, set(duplicates))


# ------------------------------------------------------------------------------------------------
# Import rewriting


def resolve_from(importer_pkg: str, level: int, module: str | None) -> str:
    if not level:
        return module or ""
    base = importer_pkg.split(".")
    base = base[: len(base) - (level - 1)]
    return ".".join(base + ([module] if module else []))


def relative(from_pkg: str, target: str) -> str:
    """``from <relative> import``: the dotted relative path from package ``from_pkg`` to module ``target``."""
    a, b = from_pkg.split("."), target.split(".")
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    dots = "." * (len(a) - i + 1)
    return dots + ".".join(b[i:])


class Mapper:
    """Maps (old absolute module, symbol) -> new absolute module."""

    def __init__(self, placements: dict[str, Placement], ref: str | None = None):
        self.sym: dict[tuple[str, str], str] = {}
        self.split_mods: set[str] = set()
        for split in SPLITS:
            pl = placements[split.lib]
            for (m, n), d in pl.dest.items():
                self.sym[(_abs(m), n)] = _abs(d)
            self.split_mods |= {_abs(m) for m in split.sources}
        for (m, n), d in REPLACED.items():
            self.sym[(_abs(m), n)] = _abs(d)
        # The tool contract moved to tools/sdk.py (HarnessContext's tool-facing view is ToolContext).
        # Old files regenerated from main still import it from the agents package.
        self.redirect: dict[tuple[str, str], tuple[str, str]] = {
            ("bioagent.agents.research_harness", "HarnessTool"): ("bioagent.tools.sdk", "HarnessTool"),
            ("bioagent.agents.research_harness", "HarnessContext"): ("bioagent.tools.sdk", "ToolContext"),
        }
        self.mod: dict[str, str] = {_abs(k): _abs(v) for k, v in MODULE_MOVES.items()}
        self.mod.update({_abs(k): v for k, v in PLATFORM_MOVES.items()})
        for old, (new, subs) in PACKAGE_MOVES.items():
            self.mod[_abs(old)] = _abs(new)
            stems = ([Path(f).stem for f in _ls_tree(ref, f"src/bioagent/tools/{old}/") if f.endswith(".py")]
                     if ref else [p.stem for p in (TOOLS / old).glob("*.py")])
            for stem in stems:
                if stem == "__init__":
                    continue
                self.mod[_abs(f"{old}.{stem}")] = _abs(f"{new}.{subs.get(stem, stem)}")

    def new_module(self, old_mod: str, name: str | None = None) -> str | None:
        """Where ``name`` imported from ``old_mod`` lives now (None: unchanged)."""
        if old_mod in self.split_mods:
            if name is None:
                raise KeyError(f"{old_mod} was split; it cannot be imported as a whole")
            try:
                return self.sym[(old_mod, name)]
            except KeyError:
                raise KeyError(f"{old_mod}.{name} has no destination (dropped or unknown)") from None
        return self.mod.get(old_mod)

    def module_object(self, old_mod: str) -> str | None:
        """New location when a whole module is imported as an object (``from x import mod``)."""
        if old_mod in self.split_mods:
            return None                     # handled attribute by attribute
        return self.mod.get(old_mod)


def exists_now(dotted: str) -> bool:
    """True when ``dotted`` names a module or package of the NEW layout that exists in the working
    tree. Makes the rewrites idempotent: a tool folder can carry its old module's name
    (``literature_search.py`` became ``literature_search/``), and a reference that already points
    into the new layout must not be moved again. Old files still present before --apply (a module
    that is about to move, the old ``hpo_terms/`` package) do not count."""
    if not dotted.startswith(PKG + "."):
        return False
    rel = dotted[len(PKG) + 1:]
    old_names = set(MODULE_MOVES) | set(PLATFORM_MOVES) | {m for x in SPLITS for m in x.sources}
    p = SRC.joinpath(*dotted.split("."))
    if (p / "__init__.py").is_file():
        return rel.split(".")[0] not in PACKAGE_MOVES
    if p.with_suffix(".py").is_file():
        return rel not in old_names and rel.split(".")[0] not in PACKAGE_MOVES
    return False


def rewrite_import_from(node: ast.ImportFrom, importer_pkg_old: str, importer_pkg_new: str,
                        mapper: Mapper, indent: str, keep_relative: bool) -> str | None:
    """New source for one ``from ... import ...`` statement, or None when it is unchanged."""
    target = resolve_from(importer_pkg_old, node.level, node.module)
    by_dest: dict[str, list[ast.alias]] = defaultdict(list)
    changed = importer_pkg_old != importer_pkg_new and node.level > 0
    if node.module is None or target == PKG and not node.module:
        # `from . import x` / `from .. import tools`: x may be a module that moved or was split, or
        # a name defined in the package itself (whose package may have moved)
        base_new = mapper.mod.get(target, target)
        for a in node.names:
            sub = f"{target}.{a.name}"
            if sub in mapper.split_mods:
                return None               # module-object import of a split module: caller handles
            new = mapper.module_object(sub)
            if new:
                parent, leaf = new.rsplit(".", 1)
                by_dest[f"{parent}::{leaf}"].append(ast.alias(name=leaf, asname=a.asname or (a.name if a.name != leaf else None)))
                changed = True
            else:
                by_dest[f"{base_new}::"].append(a)
                changed = changed or base_new != target
        out = []
        for key, aliases in by_dest.items():
            mod, leaf = key.split("::")
            names = ", ".join(_alias_src(x) for x in aliases)
            out.append(f"{indent}from {_spell(importer_pkg_new, mod, node.level > 0 or keep_relative)} import {names}")
        return "\n".join(out) if changed else None
    from_tools = importer_pkg_new.startswith(PKG)
    for a in node.names:
        if a.name == "*":
            raise SystemExit(f"star import from {target} is not supported")
        if from_tools and (target, a.name) in mapper.redirect:
            mod, name = mapper.redirect[(target, a.name)]
            alias = a.asname or (a.name if name != a.name else None)
            by_dest[mod].append(ast.alias(name=name, asname=alias))
            changed = True
            continue
        sub = f"{target}.{a.name}"
        if exists_now(sub):               # `from bioagent.tools.run_de import tool`: already new layout
            by_dest[target].append(a)
            continue
        if sub in mapper.split_mods:
            return None                   # module-object import of a split module: caller handles
        moved_sub = mapper.module_object(sub)
        if moved_sub:                     # `from bioagent.tools import schematic`: a moved module
            parent, leaf = moved_sub.rsplit(".", 1)
            by_dest[parent].append(ast.alias(name=leaf, asname=a.asname or (a.name if a.name != leaf else None)))
            changed = True
            continue
        new = mapper.new_module(target, a.name) if (target in mapper.split_mods or target in mapper.mod) else None
        if new is None and target in mapper.mod:
            new = mapper.mod[target]
        by_dest[new or target].append(a)
        if new and new != target:
            changed = True
    if not changed:
        return None
    rel = node.level > 0 or keep_relative
    lines = []
    for mod, aliases in by_dest.items():
        names = ", ".join(_alias_src(x) for x in aliases)
        stmt = f"{indent}from {_spell(importer_pkg_new, mod, rel)} import {names}"
        if len(stmt) > 100 and len(aliases) > 1:
            inner = "".join(f"{indent}    {_alias_src(x)},\n" for x in aliases)
            stmt = f"{indent}from {_spell(importer_pkg_new, mod, rel)} import (\n{inner}{indent})"
        lines.append(stmt)
    return "\n".join(lines)


def _alias_src(a: ast.alias) -> str:
    return a.name + (f" as {a.asname}" if a.asname else "")


def _spell(importer_pkg: str, target: str, rel: bool) -> str:
    if rel and target.startswith("bioagent.") and importer_pkg.startswith("bioagent."):
        return relative(importer_pkg, target)
    return target


def rewrite_imports_in(text: str, importer_old: str, importer_new: str, mapper: Mapper,
                       is_pkg_init: bool = False) -> str:
    """Rewrite every ``from ... import`` in ``text`` (any nesting) for a module that moves from
    ``importer_old`` to ``importer_new`` (both absolute module names; equal when it stays)."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    pkg_old = importer_old if is_pkg_init else importer_old.rsplit(".", 1)[0]
    pkg_new = importer_new if is_pkg_init else importer_new.rsplit(".", 1)[0]
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            indent = re.match(r"\s*", lines[node.lineno - 1]).group(0)
            new = rewrite_import_from(node, pkg_old, pkg_new, mapper, indent, keep_relative=node.level > 0)
            if new is not None:
                tail = _trailing_comment(lines[node.end_lineno - 1])
                edits.append((node.lineno - 1, node.end_lineno, new + tail + "\n"))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in mapper.split_mods:
                    raise SystemExit(f"`import {a.name}` of a split module at line {node.lineno}: rewrite by hand")
                new = mapper.module_object(a.name)
                if new:
                    indent = re.match(r"\s*", lines[node.lineno - 1]).group(0)
                    if len(node.names) != 1:
                        raise SystemExit(f"multi-name import at line {node.lineno}: rewrite by hand")
                    alias = a.asname or a.name
                    if a.asname is None:
                        raise SystemExit(f"`import {a.name}` without alias at line {node.lineno}: rewrite by hand")
                    edits.append((node.lineno - 1, node.end_lineno, f"{indent}import {new} as {alias}\n"))
    for start, end, new in sorted(edits, reverse=True):
        lines[start:end] = [new]
    return "".join(lines)


def _trailing_comment(line: str) -> str:
    m = re.search(r"\s+#.*$", line.rstrip("\n"))
    return m.group(0) if m and ")" not in m.group(0) else ""


# ------------------------------------------------------------------------------------------------
# Module-object uses of split modules (``from bioagent.tools import scrna_pack`` then ``scrna_pack.x``)


def _alias_for(new_mod: str) -> str:
    """Local name for a new module in a rewritten importer: run_de/tool -> run_de_tool,
    _lib/scrna -> scrna_lib."""
    rel = new_mod[len(PKG) + 1:]
    if rel.startswith("_lib."):
        return rel.split(".", 1)[1] + "_lib"
    if "." not in rel:                      # a top-level module such as catalog: avoid a bare name
        return f"tools_{rel}"
    return rel.replace(".", "_")


def rewrite_module_objects(text: str, path: Path, mapper: Mapper, importers: dict[str, set[str]]) -> str:
    """Replace module-object imports of split modules and their ``alias.attr`` uses."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    me = _module_of(path)
    pkg = me if path.name == "__init__.py" else me.rsplit(".", 1)[0]
    found: list[tuple[ast.stmt, str, str]] = []    # (import node, local alias, old split module)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = resolve_from(pkg, node.level, node.module)
            for a in node.names:
                sub = f"{target}.{a.name}"
                if sub in mapper.split_mods:
                    found.append((node, a.asname or a.name, sub))
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in mapper.split_mods:
                    found.append((node, a.asname or a.name, a.name))
    if not found:
        return text
    # group by import statement so a multi-name `from . import scrna_advanced, scrna_pack` is one edit
    by_node: dict[int, list[tuple[str, str]]] = defaultdict(list)
    nodes: dict[int, ast.stmt] = {}
    for node, alias, old in found:
        by_node[id(node)].append((alias, old))
        nodes[id(node)] = node
    for nid, pairs in by_node.items():
        node = nodes[nid]
        others = [a for a in node.names if (a.asname or a.name) not in {p[0] for p in pairs}]
        if others:
            raise SystemExit(f"{path}:{node.lineno}: split module imported together with other names; "
                             "split the import by hand first")
    body = "".join(lines)
    needed_by_alias: dict[str, set[str]] = defaultdict(set)   # alias -> new modules its uses need
    done_aliases: set[str] = set()
    for nid, pairs in by_node.items():
        for alias, old in pairs:
            if alias in done_aliases:        # the same alias imported again (e.g. per test function)
                continue
            done_aliases.add(alias)
            needed = needed_by_alias      # noqa: F841 - read below through the alias key
            # monkeypatch.setattr(alias, "name", value) on one line: fan out to every module that
            # looks the name up (a shared helper is imported BY NAME into each tool module)
            def fan(m: re.Match, old=old, alias=alias) -> str:
                indent, name, rest = m.group(1), m.group(2), m.group(3)
                new = mapper.new_module(old, name)
                targets = sorted(importers.get(f"{new}::{name}", set()) | {new})
                for t in targets:
                    needed_by_alias[alias].add(t)
                if len(targets) == 1:
                    return f'{indent}monkeypatch.setattr({_alias_for(targets[0])}, "{name}", {rest})'
                mods = ", ".join(_alias_for(t) for t in targets)
                return (f"{indent}for _mod in ({mods}):  # every module that looks `{name}` up\n"
                        f'{indent}    monkeypatch.setattr(_mod, "{name}", {rest})')
            body = re.sub(rf'^(\s*)monkeypatch\.setattr\({re.escape(alias)}, "(\w+)", (.*)\)\s*$', fan, body,
                          flags=re.M)
            # alias.attr in CODE (tokens, so docstrings and comments are left alone)
            uses, bare = _name_uses(body, alias)
            if bare:
                row, col = bare[0]
                ctx = body.splitlines()[row - 1]
                raise SystemExit(f"{path}:{row}: bare use of module object `{alias}` I cannot map: {ctx.strip()}")
            offsets = _line_offsets(body)
            for (r0, c0), (r1, c1), attr in sorted(uses, reverse=True):
                new = mapper.new_module(old, attr)
                needed_by_alias[alias].add(new)
                i, j = offsets[r0 - 1] + c0, offsets[r1 - 1] + c1
                body = body[:i] + f"{_alias_for(new)}.{attr}" + body[j:]
    # replace the import statements (re-split lines because the body changed lengths elsewhere)
    lines = body.splitlines(keepends=True)
    tree = ast.parse(body)
    edits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            for nid, pairs in by_node.items():
                old_node = nodes[nid]
                if node.lineno == old_node.lineno and type(node) is type(old_node) and \
                        [a.name for a in node.names] == [a.name for a in old_node.names]:
                    indent = re.match(r"\s*", lines[node.lineno - 1]).group(0)
                    tail = _trailing_comment(lines[node.end_lineno - 1])
                    rel = isinstance(node, ast.ImportFrom) and node.level > 0
                    new_lines = []
                    mods = set().union(*(needed_by_alias[a] for a, _o in pairs))
                    for mod in sorted(mods):
                        parent, leaf = mod.rsplit(".", 1)
                        new_lines.append(f"{indent}from {_spell(pkg, parent, rel)} import {leaf} as {_alias_for(mod)}")
                    if not new_lines:
                        new_lines = [f"{indent}pass  # split_tools.py: nothing left to import"]
                    new_lines[0] += tail
                    edits.append((node.lineno - 1, node.end_lineno, "\n".join(new_lines) + "\n"))
    for start, end, new in sorted(edits, reverse=True):
        lines[start:end] = [new]
    return "".join(lines)


def _name_uses(body: str, alias: str) -> tuple[list[tuple[tuple[int, int], tuple[int, int], str]],
                                               list[tuple[int, int]]]:
    """``alias.attr`` uses (start, end, attr) and bare uses (start) of NAME tokens, skipping
    import lines, strings and comments."""
    toks = list(tokenize.generate_tokens(io.StringIO(body).readline))
    uses, bare = [], []
    for i, t in enumerate(toks):
        if t.type != tokenize.NAME or t.string != alias:
            continue
        if i and toks[i - 1].type == tokenize.OP and toks[i - 1].string == ".":
            continue                                   # something.alias: not our module object
        if t.line.lstrip().startswith(("from ", "import ")):
            continue
        if i + 2 < len(toks) and toks[i + 1].string == "." and toks[i + 2].type == tokenize.NAME:
            uses.append((t.start, toks[i + 2].end, toks[i + 2].string))
        else:
            bare.append(t.start)
    return uses, bare


def _line_offsets(text: str) -> list[int]:
    out, pos = [], 0
    for ln in text.splitlines(keepends=True):
        out.append(pos)
        pos += len(ln)
    out.append(pos)
    return out


def _strip_imports(body: str, alias: str) -> str:
    return "\n".join(ln for ln in body.splitlines()
                     if not re.match(r"\s*(from\s+\S+\s+)?import\b", ln) and not ln.lstrip().startswith("#"))


def _module_of(path: Path) -> str:
    rel = path.relative_to(SRC) if SRC in path.parents else None
    if rel is None:
        return "__external__." + path.stem
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# ------------------------------------------------------------------------------------------------
# Generating the split modules


def _para(text: str) -> str:
    return textwrap.fill(" ".join(text.split()), width=98)


def header_tool(tool: str, origin: str, lib: str | None) -> str:
    shared = f" Helpers that several tools use live in ``bioagent.tools.{lib}``." if lib else ""
    return ('"""' + f"The ``{tool}`` tool. Documentation: ``TOOL.md`` in this folder.\n\n"
            + _para(f"Split out of {origin} by ``scripts/refactor/split_tools.py``: the code is the old "
                    f"module's text, verbatim, with only the imports rewritten.{shared}") + '\n"""\n')


def header_lib(line: str, origin: str, tools: list[str], doc: str) -> str:
    return ('"""' + f"Code the {line} tools share.\n\n"
            + _para(f"Split out of {origin} by ``scripts/refactor/split_tools.py``: each top-level helper "
                    "or constant that more than one tool uses moved here verbatim. A tool's own code is "
                    f"in ``bioagent.tools.<tool>.tool``. The tools: {', '.join(tools)}.")
            + "\n\nThe original module docstring:\n\n" + doc + '\n"""\n')


def _doc_body(doc_src: str) -> str:
    try:
        val = ast.literal_eval(doc_src.strip())
    except (ValueError, SyntaxError):
        return ""
    return str(val).strip().replace('"""', "'''")


def generate_split(split: Split, pl: Placement, texts: dict[str, str], mapper: Mapper) -> dict[str, str]:
    """new module (relative) -> source text, for every module this split produces."""
    seg_by_mod: dict[str, list[Seg]] = {}
    docs: dict[str, str] = {}
    for m in split.sources:
        docs[m], seg_by_mod[m] = segments(m, texts[m])
    line = split.lib.split(".")[-1]
    out: dict[str, list[tuple[int, str, Seg]]] = defaultdict(list)   # dest -> [(order, text, seg)]
    imports_by_mod: dict[str, list[Seg]] = {m: [s for s in segs if isinstance(s.node, (ast.Import, ast.ImportFrom))]
                                            for m, segs in seg_by_mod.items()}
    order = 0
    for m in split.sources:
        lines = texts[m].splitlines(keepends=True)
        for s in seg_by_mod[m]:
            order += 1
            if isinstance(s.node, (ast.Import, ast.ImportFrom)):
                continue
            names = {n for n in s.names - split.drop if (m, n) not in pl.duplicates}
            if not names and s.names:
                continue
            if not s.names:
                raise SystemExit(f"{m}:{s.node.lineno}: top-level statement binds no name; place it by hand")
            dests = {pl.dest[(m, n)] for n in names}
            if len(dests) != 1:
                raise SystemExit(f"{m}:{s.node.lineno}: one statement, several destinations {dests}")
            out[dests.pop()].append((order, "".join(lines[s.start:s.end]), s))

    results: dict[str, str] = {}
    all_dests = set(out) | {f"{t}.tool" for t in split.roots}
    defined_in: dict[str, set[str]] = defaultdict(set)
    for d, items in out.items():
        for _o, _t, s in items:
            defined_in[d] |= s.names - split.drop
    for d in sorted(all_dests):
        items = sorted(out.get(d, []), key=lambda x: x[0])
        tool = pl.tool_of.get(d)
        new_abs = _abs(d)
        new_pkg = new_abs.rsplit(".", 1)[0]
        body_parts: list[str] = []
        refs: set[str] = set()
        for _o, txt, s in items:
            body_parts.append(_retarget_relative_imports(txt, s, new_abs, mapper))
            refs |= s.refs
        make_tool = ""
        if tool and tool in pl.catalog_elems:
            m, call = pl.catalog_elems[tool]
            src_lines = texts[m].splitlines(keepends=True)
            seg = ast.get_source_segment(texts[m], call, padded=False)
            ind = re.match(r"\s*", src_lines[call.lineno - 1]).group(0)
            seg_lines = seg.splitlines()
            fixed = [seg_lines[0]] + [ln[len(ind):] if ln.startswith(ind) else ln for ln in seg_lines[1:]]
            body_text = "\n".join("    " + ln if ln else ln for ln in fixed)
            make_tool = (f"def make_tool() -> HarnessTool:\n"
                         f'    """The ``{tool}`` record for the Scientist\'s catalog (see ``TOOL.md``)."""\n'
                         f"    return {body_text.lstrip()}\n")
            refs |= {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
        # imports this module needs: original import statements whose names it references
        header_imports: list[str] = [FUTURE]
        seen_stmt: set[str] = set()
        for m in split.sources:
            for s in imports_by_mod[m]:
                if s.node.__class__ is ast.ImportFrom and s.node.module == "__future__":
                    continue
                if isinstance(s.node, ast.ImportFrom):
                    target = resolve_from(f"{PKG}", s.node.level, s.node.module)
                    if target in mapper.split_mods:
                        continue                   # cross-imports inside the split: resolved below
                used = s.names & refs
                if not used:
                    continue
                txt = "".join(texts[m].splitlines(keepends=True)[s.node.lineno - 1:s.node.end_lineno])
                if isinstance(s.node, ast.ImportFrom):
                    keep = [a for a in s.node.names if (a.asname or a.name).split(".")[0] in used]
                    node = ast.ImportFrom(module=s.node.module, names=keep, level=s.node.level)
                    new = rewrite_import_from(node, PKG, new_pkg, mapper, "", keep_relative=True)
                    if new is None:
                        names = ", ".join(_alias_src(a) for a in keep)
                        new = f"from {_spell(new_pkg, resolve_from(PKG, s.node.level, s.node.module), s.node.level > 0)} import {names}"
                    txt = new + "\n"
                if txt not in seen_stmt:
                    header_imports.append(txt.rstrip("\n"))
                    seen_stmt.add(txt)
        # symbols from sibling destinations of this split
        cross: dict[str, set[str]] = defaultdict(set)
        for other, names in defined_in.items():
            if other == d:
                continue
            for n in names & refs:
                cross[other].add(n)
        for other in sorted(cross):
            if pl.tool_of.get(other) and pl.tool_of.get(d) is None:
                raise SystemExit(f"shared module {d} would import tool module {other} ({sorted(cross[other])})")
            names = ", ".join(sorted(cross[other]))
            stmt = f"from {relative(new_pkg, _abs(other))} import {names}"
            if len(stmt) > 100:
                inner = "".join(f"    {n},\n" for n in sorted(cross[other]))
                stmt = f"from {relative(new_pkg, _abs(other))} import (\n{inner})"
            header_imports.append(stmt)
        if make_tool:
            header_imports.append(f"from {relative(new_pkg, PKG + '.sdk')} import HarnessTool")
        srcs = sorted({s.module for _o, _t, s in items} | ({pl.catalog_elems[tool][0]} if make_tool else set()))
        origin = " and ".join(f"``tools/{m}.py``" for m in (srcs or split.sources))
        if tool:
            head = header_tool(tool, origin, split.lib if split.lib in out else None)
        else:
            head = header_lib(line, origin, list(split.roots),
                              "\n\n".join(_doc_body(docs[m]) for m in split.sources if docs[m]))
        rest = [x for x in header_imports if x != FUTURE]
        text = head + "\n" + FUTURE + "\n\n" + "\n".join(rest) + "\n\n\n" + \
            "\n\n\n".join(p.strip("\n") for p in body_parts if p.strip()) + "\n"
        if make_tool:
            text += "\n\n" + make_tool
        results[d] = text
    return results


def _retarget_relative_imports(txt: str, seg: Seg, new_abs: str, mapper: Mapper) -> str:
    """Rewrite relative imports INSIDE a moved segment (local imports in function bodies)."""
    old_pkg = _abs(seg.module).rsplit(".", 1)[0]
    new_pkg = new_abs.rsplit(".", 1)[0]
    tree = ast.parse(_dedent_block(txt))
    lines = txt.splitlines(keepends=True)
    offset = 0
    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            indent = re.match(r"\s*", lines[node.lineno - 1 + offset]).group(0)
            new = rewrite_import_from(node, old_pkg, new_pkg, mapper, indent, keep_relative=True)
            if new is None:
                target = resolve_from(old_pkg, node.level, node.module)
                new = f"{indent}from {relative(new_pkg, target)} import " + ", ".join(_alias_src(a) for a in node.names)
            tail = _trailing_comment(lines[node.end_lineno - 1 + offset])
            edits.append((node.lineno - 1 + offset, node.end_lineno + offset, new + tail + "\n"))
    for start, end, new in sorted(edits, reverse=True):
        lines[start:end] = [new]
    return "".join(lines)


def _dedent_block(txt: str) -> str:
    return txt


# ------------------------------------------------------------------------------------------------
# Driver


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def _ls_tree(ref: str, path: str) -> list[str]:
    return [line.rsplit("/", 1)[-1] for line in git("ls-tree", "--name-only", ref, path).splitlines()]


def plan(ref: str | None = None) -> tuple[dict[str, Placement], dict[str, str]]:
    """Placement of every split module's symbols, from the working tree or from ``ref``."""
    texts = {}
    for split in SPLITS:
        for m in split.sources:
            texts[m] = (git("show", f"{ref}:src/bioagent/tools/{m}.py") if ref
                        else module_path(_abs(m)).read_text(encoding="utf-8"))
    placements = {split.lib: place(split, texts) for split in SPLITS}
    return placements, texts


def check_coverage() -> None:
    known = set(MODULE_MOVES) | set(PACKAGE_MOVES) | STAY | {m for s in SPLITS for m in s.sources}
    for p in TOOLS.iterdir():
        name = p.stem if p.suffix == ".py" else p.name
        if p.name.startswith((".", "__pycache__")) or p.suffix in {".md", ".txt"}:
            continue
        if p.is_dir() and (p / "TOOL.md").exists() or name in {"_lib", "genesets"}:
            continue
        if name not in known:
            raise SystemExit(f"tools/{p.name} is not in the plan (MODULE_MOVES / SPLITS / STAY)")


def print_plan(placements: dict[str, Placement]) -> None:
    for split in SPLITS:
        pl = placements[split.lib]
        by_dest: dict[str, list[str]] = defaultdict(list)
        for (m, n), d in sorted(pl.dest.items()):
            by_dest[d].append(f"{n}  [{m}]")
        print(f"\n=== split {' + '.join(split.sources)} ===")
        for d in sorted(by_dest):
            print(f"  {d}:")
            for n in by_dest[d]:
                print(f"      {n}")
    print("\n=== whole-file moves ===")
    for k, v in MODULE_MOVES.items():
        print(f"  tools/{k}.py -> tools/{v.replace('.', '/')}.py")
    for k, (v, subs) in PACKAGE_MOVES.items():
        print(f"  tools/{k}/ -> tools/{v}/ (renames: {subs})")


def importer_files() -> list[Path]:
    out = []
    for f in git("ls-files", "src", "tests", "scripts", "experiments").split():
        p = ROOT / f
        if p.suffix == ".py" and p.exists() and "scripts/refactor/" not in f:
            out.append(p)
    return out


def apply() -> None:
    if git("status", "--porcelain").strip():
        raise SystemExit("the working tree is not clean; commit or stash first")
    check_coverage()
    placements, texts = plan()
    mapper = Mapper(placements)

    # ---- phase 1: compute every new file in memory; nothing is touched if anything fails ----
    generated: dict[str, str] = {}
    for split in SPLITS:
        for d, txt in generate_split(split, placements[split.lib], texts, mapper).items():
            if d in generated:
                raise SystemExit(f"{d} generated twice")
            ast.parse(txt)
            generated[d] = txt
    for d, old_text, new_text in GENERATED_EDITS:
        if old_text not in generated.get(d, ""):
            raise SystemExit(f"GENERATED_EDITS: expected text not found in {d}; update the table")
        generated[d] = generated[d].replace(old_text, new_text)

    # which new modules import a shared name by name (for monkeypatch fan-out)
    importers: dict[str, set[str]] = defaultdict(set)
    for d, txt in generated.items():
        for node in ast.walk(ast.parse(txt)):
            if isinstance(node, ast.ImportFrom) and node.level:
                target = resolve_from(_abs(d).rsplit(".", 1)[0], node.level, node.module)
                for a in node.names:
                    importers[f"{target}::{a.name}"].add(_abs(d))

    moves: list[tuple[Path, Path]] = []                  # (old path, new path), files only
    moved_text: dict[Path, str] = {}                     # new path -> rewritten text
    for old, new in MODULE_MOVES.items():
        src, dst = TOOLS / f"{old}.py", TOOLS / (new.replace(".", "/") + ".py")
        moves.append((src, dst))
        moved_text[dst] = rewrite_imports_in(src.read_text(encoding="utf-8"), _abs(old), _abs(new), mapper)
    for old, (new, subs) in PACKAGE_MOVES.items():
        for src in sorted((TOOLS / old).iterdir()):
            if src.name == "__pycache__":
                continue
            stem = subs.get(src.stem, src.stem) if src.suffix == ".py" else src.stem
            dst = TOOLS / new / (stem + src.suffix)
            moves.append((src, dst))
            if src.suffix == ".py":
                is_init = src.stem == "__init__"
                o = _abs(old) if is_init else _abs(f"{old}.{src.stem}")
                n = _abs(new) if is_init else _abs(f"{new}.{stem}")
                moved_text[dst] = rewrite_imports_in(src.read_text(encoding="utf-8"), o, n, mapper,
                                                     is_pkg_init=is_init)

    old_paths = {src for src, _dst in moves} | {TOOLS / f"{m}.py" for s in SPLITS for m in s.sources}
    rewritten: dict[Path, str] = {}
    for p in importer_files():
        if p in old_paths:
            continue
        txt = p.read_text(encoding="utf-8")
        new_txt = rewrite_module_objects(txt, p, mapper, importers)
        me = _module_of(p)
        new_txt = rewrite_imports_in(new_txt, me, me, mapper, is_pkg_init=p.name == "__init__.py")
        new_txt = rewrite_string_targets(new_txt, mapper)
        if new_txt != txt:
            ast.parse(new_txt)
            rewritten[p] = new_txt

    for rel, old_text, new_text in POST_EDITS:
        p = ROOT / rel
        txt = rewritten.get(p, p.read_text(encoding="utf-8"))
        if old_text not in txt:
            raise SystemExit(f"POST_EDITS: expected text not found in {rel}; update the table")
        rewritten[p] = txt.replace(old_text, new_text)

    api = TOOLS / "api.py"
    api_txt = rewritten.get(api, api.read_text(encoding="utf-8"))

    def api_repl(m: re.Match) -> str:
        mod, attr = m.group(1), m.group(2)
        if exists_now(mod) and not _is_old_module_file(mod) and mod not in mapper.split_mods:
            return m.group(0)
        try:
            new = (mapper.new_module(mod, attr) if mod in mapper.split_mods else mapper.mod.get(mod)) or mod
        except KeyError:
            print(f"api.py: {mod}.{attr} was dropped; point its entry somewhere by hand")
            return m.group(0)
        return f'("{new}", "{attr}")'
    rewritten[api] = re.sub(r'\("(bioagent\.tools\.[\w.]+)", "(\w+)"\)', api_repl, api_txt)

    # ---- phase 2: write ----
    for src, dst in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        git("mv", str(src), str(dst))
    for dst, txt in moved_text.items():
        dst.write_text(txt, encoding="utf-8")
    for d, txt in generated.items():
        p = TOOLS / (d.replace(".", "/") + ".py")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    for split in SPLITS:
        for m in split.sources:
            git("rm", "-q", str(TOOLS / f"{m}.py"))
    for old in PACKAGE_MOVES:
        leftover = TOOLS / old
        if leftover.exists():
            subprocess.run(["rm", "-rf", str(leftover)], check=True)     # only __pycache__ remains
    lib_init = TOOLS / "_lib" / "__init__.py"
    if (TOOLS / "_lib").exists() and not lib_init.exists():
        lib_init.write_text('"""Code shared by several tools of one line. Private to the tools package: the\n'
                            'platform must not import it (tests/test_repo_boundaries.py)."""\n', encoding="utf-8")
    folders = {d.split(".")[0] for d in generated if not d.startswith("_lib")} | \
              {v.split(".")[0] for v in MODULE_MOVES.values()}
    for f in sorted(folders):
        init = TOOLS / f / "__init__.py"
        if not init.exists():
            init.write_text(f'"""The ``{f}`` tool: see ``TOOL.md`` here. Import a name from the module that\n'
                            f'defines it (e.g. ``bioagent.tools.{f}.tool``), not from this package."""\n',
                            encoding="utf-8")
    for p, txt in rewritten.items():
        p.write_text(txt, encoding="utf-8")
    git("add", "-A", "src", "tests", "scripts", "experiments")
    print(f"applied: {len(moves)} moves, {len(generated)} generated modules, {len(rewritten)} importers rewritten")


def regenerate(ref: str) -> None:
    """After merging ``ref`` (normally main) into the split branch: rebuild the split modules from
    ``ref``'s versions of the old files, and re-point any import of an old module that the merge
    brought in. Whole-file moves need nothing here: git's rename detection carries main's edits to
    the moved files during the merge."""
    known = set(MODULE_MOVES) | set(PACKAGE_MOVES) | set(PLATFORM_MOVES) | STAY | \
        {m for x in SPLITS for m in x.sources} | {"_lib", "genesets", "README.md"}
    new_on_ref = [n for n in _ls_tree(ref, "src/bioagent/tools/")
                  if (n[:-3] if n.endswith(".py") else n) not in known]
    if new_on_ref:
        raise SystemExit(f"{ref} has tools/ entries the plan does not place: {new_on_ref}; "
                         "add them to MODULE_MOVES / SPLITS / STAY first")
    placements, texts = plan(ref)
    mapper = Mapper(placements, ref=ref)
    generated: dict[str, str] = {}
    for split in SPLITS:
        generated.update(generate_split(split, placements[split.lib], texts, mapper))
    for d, old_text, new_text in GENERATED_EDITS:
        if old_text not in generated.get(d, ""):
            raise SystemExit(f"GENERATED_EDITS: expected text not found in {d}; update the table")
        generated[d] = generated[d].replace(old_text, new_text)
    importers: dict[str, set[str]] = defaultdict(set)
    for d, txt in generated.items():
        for node in ast.walk(ast.parse(txt)):
            if isinstance(node, ast.ImportFrom) and node.level:
                target = resolve_from(_abs(d).rsplit(".", 1)[0], node.level, node.module)
                for a in node.names:
                    importers[f"{target}::{a.name}"].add(_abs(d))
    changed = []
    for d, txt in generated.items():
        p = TOOLS / (d.replace(".", "/") + ".py")
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists() or p.read_text(encoding="utf-8") != txt:
            p.write_text(txt, encoding="utf-8")
            changed.append(str(p.relative_to(ROOT)))
    for p in importer_files():
        if TOOLS / "_lib" in p.parents or p.parent.parent == TOOLS and p.name == "tool.py" and \
                p.parent.name in {t for x in SPLITS for t in x.roots}:
            continue                                     # just generated
        txt = p.read_text(encoding="utf-8")
        new_txt = rewrite_module_objects(txt, p, mapper, importers)
        me = _module_of(p)
        new_txt = rewrite_imports_in(new_txt, me, me, mapper, is_pkg_init=p.name == "__init__.py")
        new_txt = rewrite_string_targets(new_txt, mapper)
        if new_txt != txt:
            ast.parse(new_txt)
            p.write_text(new_txt, encoding="utf-8")
            changed.append(str(p.relative_to(ROOT)))
    print("regenerated from", ref, "- changed:")
    for c in changed:
        print("  ", c)
    print("next: python scripts/tool_docs.py && python -m pytest, then commit")


def _is_old_module_file(dotted: str) -> bool:
    """The OLD single-file module still exists (``tools/literature_search.py``): before --apply."""
    return SRC.joinpath(*dotted.split(".")).with_suffix(".py").is_file()


def rewrite_string_targets(text: str, mapper: Mapper) -> str:
    """Dotted strings that name a moved module (``"bioagent.tools.hpo_terms.mapper"``) or an
    attribute of one (``"bioagent.tools.scrna_pack._p"``, a monkeypatch/mock target)."""
    def repl(m: re.Match) -> str:
        full = m.group(1)
        if exists_now(full):
            return m.group(0)             # already names a module of the new layout
        if full in mapper.mod:
            return f'"{mapper.mod[full]}"'
        mod, attr = full.rsplit(".", 1)
        if exists_now(mod) and mod not in mapper.split_mods and not _is_old_module_file(mod):
            return m.group(0)             # an attribute of a new-layout module
        if mod in mapper.split_mods:
            return f'"{mapper.new_module(mod, attr)}.{attr}"'
        new = mapper.mod.get(mod)
        return f'"{new}.{attr}"' if new else m.group(0)
    return re.sub(r'"(bioagent\.tools(?:\.\w+)+)"', repl, text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="print the placement and exit")
    g.add_argument("--apply", action="store_true", help="perform the reorganisation")
    g.add_argument("--preview", metavar="DIR", help="write only the generated split modules into DIR")
    g.add_argument("--regenerate-from", metavar="REF",
                   help="after merging REF into the split branch, rebuild the split modules from REF's "
                        "old files and re-point imports the merge brought in")
    a = ap.parse_args(argv)
    if a.regenerate_from:
        regenerate(a.regenerate_from)
        return 0
    if a.plan:
        check_coverage()
        placements, _ = plan()
        print_plan(placements)
        return 0
    if a.preview:
        check_coverage()
        placements, texts = plan()
        mapper = Mapper(placements)
        out = Path(a.preview)
        for split in SPLITS:
            for d, txt in generate_split(split, placements[split.lib], texts, mapper).items():
                ast.parse(txt)
                p = out / (d.replace(".", "/") + ".py")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(txt, encoding="utf-8")
                print(f"{p}  ({len(txt.splitlines())} lines)")
        return 0
    apply()
    return 0


if __name__ == "__main__":
    sys.exit(main())
