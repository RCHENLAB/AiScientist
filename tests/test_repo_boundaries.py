"""The platform's side of the repository boundary (stays in AiScientist).

The platform may import the tools, but only through their doors: ``bioagent.tools.sdk`` (the tool
contract), ``bioagent.tools.catalog`` (discovery) and ``bioagent.tools.api`` (everything else), and
only public names, so a tool's internals can move without the platform noticing. The tools' side of
the boundary is ``tests/test_tools_boundary.py`` (it moves with AiScientist-tools).

The checks read the source with ``ast``, resolving relative imports to absolute module names.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PKG = SRC / "bioagent"
TOOLS = PKG / "tools"          # absent once the tools live in their own repository


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


PUBLIC_TOOL_MODULES = {"bioagent.tools", "bioagent.tools.sdk", "bioagent.tools.api", "bioagent.tools.catalog"}


def _platform_files() -> list[Path]:
    return [p for p in _py_files(PKG) if TOOLS not in p.parents]


def test_platform_imports_only_public_tool_names():
    offenders = []
    for path in _platform_files():
        for mod, names, line in _imports(path):
            if not mod.startswith("bioagent.tools"):
                continue
            private_mod = [p for p in mod.split(".")[2:] if p.startswith("_")]
            private_names = [n for n in names if n.startswith("_") and n != "__version__"]
            if private_mod or private_names:
                offenders.append(f"{path.relative_to(SRC)}:{line} {mod} {private_names or private_mod}")
    assert not offenders, (
        "the platform may import only public names from bioagent.tools (make it public in the tool, "
        "or move the helper to the platform):\n" + "\n".join(offenders))


def test_platform_reaches_tools_only_through_sdk_catalog_and_api():
    # Stronger than "public names only": the platform names no tool module at all, so the tools
    # can be reorganised (one folder per tool) without editing a line of the platform.
    offenders = []
    for path in _platform_files():
        for mod, names, line in _imports(path):
            if not mod.startswith("bioagent.tools"):
                continue
            if mod == "bioagent.tools" and set(names) <= {"api", "sdk", "catalog"}:
                continue
            if mod not in PUBLIC_TOOL_MODULES:
                offenders.append(f"{path.relative_to(SRC)}:{line} imports {mod}")
    assert not offenders, (
        "import tools through bioagent.tools.api (add the name to its table), .catalog or .sdk:\n"
        + "\n".join(offenders))
