"""Operator-only runtime selection for corpus maintenance scripts.

The gateway stages these scripts into its configured literature image. It never
imports the external scientific package. The scientific runtime is the independently published MCP image.
"""
from __future__ import annotations

import os

ENV = "AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME"
SCRIPTS_ENV = "AISCIENTIST_CORPUS_MAINTENANCE_SCRIPTS"
_OPERATORS = frozenset({"build_mechanism_cache", "eval_corpus_retrieval"})


def runtime() -> str:
    choice = os.environ.get(ENV, "mcp")
    if choice != "mcp":
        raise ValueError(f"{ENV} must be mcp (the app scientific runtime has been retired)")
    return choice


def operator_script(name: str):
    """Resolve one fixed operator file from an explicitly configured trusted checkout."""
    from pathlib import Path

    runtime()
    if name not in _OPERATORS:
        raise ValueError("Unknown corpus maintenance operator")
    root = Path(os.environ.get(SCRIPTS_ENV, ""))
    if not root.is_absolute() or ".." in root.parts:
        raise RuntimeError(f"Set {SCRIPTS_ENV} to the absolute scripts directory of a reviewed aiscientist-mcp checkout")
    if any(part.is_symlink() for part in [root, *root.parents]):
        raise RuntimeError("Corpus maintenance scripts directory must not traverse a symlink")
    path = root / (name + ".py")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("Configured corpus maintenance operator must be a regular file no larger than 2 MiB")
    return path


def operator_module(name: str):
    """Run the reviewed external operator inside its native image, never in the gateway."""
    import hashlib
    import importlib.util
    import sys

    path = operator_script(name)
    module_name = "_native_corpus_operator_" + hashlib.sha256(path.read_bytes()).hexdigest()
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    return sys.modules[module_name]


def search_publications(query: str, limit: int, *, timeout_s: float = 20.0,
                        abstracts: bool = True, pubmed_only: bool = True) -> dict:
    """Benchmark the published MCP tool.

    Native mode runs only inside the operator's literature image, on the current
    compute node. Fresh scratch prevents result caching from distorting timings.
    """
    runtime()  # reject a stale operator setting before launching a job
    import asyncio
    import json
    from pathlib import Path
    import tempfile

    async def call(folder: str) -> dict:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters
        env = dict(os.environ, AISCI_DATA_DIR=folder, AISCI_EXECUTOR="inline", AISCI_JOB_PREFIX="")
        async with Client(StdioServerParameters(command="literature-mcp", env=env),
                          read_timeout_seconds=timeout_s + 60) as client:
            listed = (await client.list_tools()).tools
            if "literature_search" not in {tool.name for tool in listed}:
                raise RuntimeError("Published literature runtime does not expose literature_search")
            result = await client.call_tool("literature_search", {
                "query": query, "limit": limit, "abstracts": abstracts, "pubmed_only": pubmed_only,
                "analysis_id": "pubmed-depth", "rerun": True})
            if result.is_error:
                return {"status": "error", "error": " ".join(getattr(c, "text", "") for c in result.content)}
            out = result.structured_content
            if not isinstance(out, dict):
                raise RuntimeError("Published literature tool returned no structured result")
            if out.get("response_omitted"):
                path, root = Path(out["full_result"]), Path(folder).resolve()
                if not path.is_absolute() or not path.resolve().is_relative_to(root):
                    raise RuntimeError("Literature result escaped its private benchmark directory")
                if any(p.is_symlink() for p in [path, *path.parents] if p != root and p.is_relative_to(root)):
                    raise RuntimeError("Literature result must not be a symlink")
                if path.stat().st_size > 32 * 1024 * 1024:
                    raise RuntimeError("Literature result exceeds the benchmark limit")
                out = json.loads(path.read_text())
            return out

    with tempfile.TemporaryDirectory(prefix="literature-depth-") as folder:
        return asyncio.run(call(folder))


def platform_helpers():
    """Load the operator's fixed, pure platform sources without package startup.

    Keep these files from the same checkout beside scripts in a read-only bundle:
    literature_contract.py, agents/literature_answer.py and agents/loop_utils.py.
    Selection uses the application's actual implementation, not a copied baseline.
    """
    import hashlib
    import importlib.util
    from pathlib import Path
    import sys
    import types

    root = Path(__file__).resolve().parents[1] / "src" / "aiscientist"
    sources = {"contract": root / "literature_contract.py",
               "loop_utils": root / "agents" / "loop_utils.py",
               "literature_answer": root / "agents" / "literature_answer.py"}
    missing = [str(path.relative_to(root)) for path in sources.values() if not path.is_file()]
    if missing:
        raise RuntimeError("The read-only operator bundle is missing platform helpers: " + ", ".join(missing))
    package = "_aisci_literature_operator_" + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = []
        sys.modules[package] = module
    loaded = {}
    for label, path in sources.items():
        name = package + "." + label
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        loaded[label] = sys.modules[name]
    return loaded["literature_answer"].collect_papers, loaded["contract"]
