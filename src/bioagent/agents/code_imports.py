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


def third_party_imports(code: str) -> list[str]:
    """The subset of :func:`top_level_imports` that could plausibly need installing."""
    return [m for m in top_level_imports(code) if m not in _STDLIB and m not in _LOCAL]


def distribution_for(module: str) -> str:
    """The pip requirement that provides ``module``. A guess for anything not in the table —
    shown to the user in the confirmation prompt, so a wrong guess is correctable rather than
    silent."""
    return IMPORT_TO_DISTRIBUTION.get(module, module)
