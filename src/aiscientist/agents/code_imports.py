"""What third-party modules does a snippet need? — read statically, before it runs.

Waiting for a runtime ``ModuleNotFoundError`` is the wrong moment to discover a missing
dependency, and the measured behaviour on HPC3 shows why. Today a snippet that pip-installs what
it needs gets one of three outcomes, none of them good:

* the wheel fails to build (the container's 64 MB ``/tmp`` cannot compile an extension), or
* **pip reports success and the very next line still fails** — ``--containall`` gives an empty
  ``$HOME``, so ``~/.local/.../site-packages`` did not exist when the interpreter started and is
  therefore not on ``sys.path``; installing into it mid-process changes nothing, or
* it appears to work and then evaporates, because the next step is a fresh container.

Parsing the imports up front turns all of that into one decision, made *before* any compute is
spent: here is what this code needs, here is what is missing, install it or don't.

Deliberately AST-based rather than regex: a regex over source text sees ``import`` inside strings
and comments, and misses nothing that matters here anyway. Only ABSOLUTE, top-level module names
are reported — relative imports are the snippet's own files, and a submodule (``import a.b``)
resolves through its root distribution.
"""

from __future__ import annotations

import ast
import sys

# Import name -> PyPI distribution, for the cases where they differ. Everything else falls back
# to the module name, which is right far more often than not. Kept short on purpose: a long
# table rots, and a wrong guess is visible to the user in the confirmation prompt anyway.
IMPORT_TO_DISTRIBUTION = {
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "cv2": "opencv-python-headless",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "Bio": "biopython",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "OpenSSL": "pyOpenSSL",
    "attr": "attrs",
    "pkg_resources": "setuptools",
    "mpl_toolkits": "matplotlib",
    "statsmodels.api": "statsmodels",
    "igraph": "python-igraph",
    "louvain": "louvain",
    "pysam": "pysam",
    "cyvcf2": "cyvcf2",
    "anndata": "anndata",
    "scanpy": "scanpy",
    "pyranges": "pyranges",
}

# Modules that ship with the interpreter. `sys.stdlib_module_names` is authoritative for the
# RUNNING interpreter; the container's may differ slightly by version, which is why a name that
# survives this filter is still checked against the image itself before anything is installed.
_STDLIB = set(getattr(sys, "stdlib_module_names", ()))

# Names a snippet may import that are provided by the run itself rather than by PyPI.
_LOCAL = {"__future__", "__main__"}


def top_level_imports(code: str) -> list[str]:
    """Every absolute top-level module a snippet imports, in first-seen order.

    Returns ``[]`` for code that does not parse — a syntax error is the snippet's own problem and
    surfaces on execution with a far better message than anything this could invent.
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []

    seen: list[str] = []

    def add(name: str) -> None:
        root = (name or "").split(".", 1)[0].strip()
        if root and root not in seen:
            seen.append(root)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — the snippet's own package, never installable.
            if not node.level and node.module:
                add(node.module)
    return seen


#: Exception types whose presence in a handler means the snippet has DECIDED it can run without
#: the import. A bare ``except:`` counts too — it tolerates everything, import errors included.
_IMPORT_TOLERANT = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def _is_giving_up(body: "list[ast.stmt]") -> bool:
    """Is this handler doing nothing at all — ``pass`` / ``...`` and nothing else?

    The distinction that matters. ``try: import cyvcf2 except ImportError: cyvcf2 = None`` BINDS a
    fallback, so the snippet plainly intends to use ``cyvcf2`` later and failing to install it
    produces a baffling ``None`` error downstream — preflight should offer. ``try: import torch as
    _bio_t; _bio_t.manual_seed(0) except Exception: pass`` does nothing on failure because there is
    nothing to do: the seeding is best-effort and no later line depends on it.
    """
    return all(isinstance(st, ast.Pass)
               or (isinstance(st, ast.Expr)
                   and isinstance(st.value, ast.Constant) and st.value.value is Ellipsis)
               for st in body) and bool(body)


def _handler_tolerates_import_error(handlers: "list[ast.ExceptHandler]") -> bool:
    """A handler that both CATCHES an import failure and then gives up on it."""
    for h in handlers:
        if not _is_giving_up(h.body):
            continue
        if h.type is None:                                  # bare except: catches everything
            return True
        names = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        for n in names:
            label = n.id if isinstance(n, ast.Name) else (
                n.attr if isinstance(n, ast.Attribute) else "")
            if label in _IMPORT_TOLERANT:
                return True
    return False


def _roots_of(node: "ast.AST") -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name.split(".", 1)[0].strip() for a in node.names]
    if isinstance(node, ast.ImportFrom) and not node.level and node.module:
        return [node.module.split(".", 1)[0].strip()]
    return []


def optional_imports(code: str) -> list[str]:
    """Modules the snippet imports ONLY inside a try/except that tolerates an import failure.

    Such an import is not a dependency: the code has already said, in the only way code can, that
    it runs without it. Treating one as required is how every ``run_code`` call spent ten minutes
    re-discovering that ``torch`` — imported by the determinism preamble inside ``try: … except
    Exception: pass`` purely to seed an RNG that may not exist — was absent from the analysis
    image, asking to install it, and being declined.

    Only a handler that GIVES UP counts (``pass`` / ``...``). A handler that binds a fallback —
    ``except ImportError: cyvcf2 = None`` — says the snippet means to use the module anyway, and
    offering to install it is the helpful thing to do.

    A module imported BOTH ways stays required: the unguarded import is the one that would fail.
    """
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []

    guarded: set[str] = set()
    unguarded: set[str] = set()

    def visit(node: "ast.AST", tolerated: bool) -> None:
        for root in _roots_of(node):
            (guarded if tolerated else unguarded).add(root)
        if isinstance(node, ast.Try):
            body_tolerated = tolerated or _handler_tolerates_import_error(node.handlers)
            for child in node.body:
                visit(child, body_tolerated)
            # handlers/else/finally are NOT covered by this try's own handlers.
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                visit(child, tolerated)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, tolerated)

    visit(tree, False)
    return sorted(guarded - unguarded)


def third_party_imports(code: str) -> list[str]:
    """The subset of :func:`top_level_imports` that could plausibly need installing.

    Excludes imports the snippet itself guards against failing (see :func:`optional_imports`) —
    offering to install those interrupts a step for a package nothing is waiting on.
    """
    tolerated = set(optional_imports(code))
    return [m for m in top_level_imports(code)
            if m not in _STDLIB and m not in _LOCAL and m not in tolerated]


def distribution_for(module: str) -> str:
    """The pip requirement that provides ``module``. A guess for anything not in the table —
    shown to the user in the confirmation prompt, so a wrong guess is correctable rather than
    silent."""
    return IMPORT_TO_DISTRIBUTION.get(module, module)
