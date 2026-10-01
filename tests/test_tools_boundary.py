"""The tools' side of the repository boundary (moves with AiScientist-tools).

The tools ship as their own package, so they import nothing from the platform (``bioagent.agents``,
``bioagent.gateway``, ...): the one shared module is ``bioagent.tools.sdk``, and everything the
platform may use from the tools is named in ``bioagent.tools.api``. The platform's side of the same
boundary is ``tests/test_repo_boundaries.py`` in the platform repository.

The checks read the source with ``ast`` (a docstring that mentions the gateway is fine; an import is
not), resolving relative imports to absolute module names.
"""
from __future__ import annotations

import ast
from pathlib import Path

from bioagent.tools import catalog

TOOLS = Path(catalog.TOOLS_DIR)
SRC = TOOLS.parents[1]


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> list[tuple[str, list[str], int]]:
    """(absolute module, imported names, line) for every import statement in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    me = _module_name(path)
    package = me if path.name == "__init__.py" else me.rsplit(".", 1)[0]
    out: list[tuple[str, list[str], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, [], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            names = [a.name for a in node.names]
            if not node.module:
                # `from . import x` / `from .. import x`: each name may itself be a module
                for n in names:
                    out.append((f"{mod}.{n}", [], node.lineno))
            else:
                out.append((mod, names, node.lineno))
    return out


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_tools_never_import_the_platform():
    offenders = []
    for path in _py_files(TOOLS):
        for mod, _names, line in _imports(path):
            if mod == "bioagent" or (mod.startswith("bioagent.") and not mod.startswith("bioagent.tools")):
                offenders.append(f"{path.relative_to(SRC)}:{line} imports {mod}")
    assert not offenders, (
        "tools must not import the platform (use bioagent.tools.sdk, or take the dependency as a "
        "factory argument):\n" + "\n".join(offenders))


def test_api_exports_resolve():
    from bioagent.tools import api

    missing = []
    for name in api._EXPORTS:
        try:
            getattr(api, name)
        except (AttributeError, ImportError) as exc:   # a moved tool left the table stale
            missing.append(f"{name}: {exc}")
    assert not missing, "\n".join(missing)


def test_the_sdk_is_dependency_free():
    # The contract module is imported by every tool, in the gateway and inside HPC3 containers: it
    # must stay importable with nothing but the standard library.
    for mod, _names, line in _imports(TOOLS / "sdk.py"):
        root = mod.split(".")[0]
        assert root in {"__future__", "dataclasses", "pathlib", "typing"}, f"sdk.py:{line} imports {mod}"
