"""Operator-only runtime selection for corpus maintenance scripts.

The gateway stages these scripts into its configured literature image. It never
imports the external scientific package. The legacy option remains available
until the deployment's native-tool acceptance gate is complete.
"""
from __future__ import annotations

import importlib
import os
from typing import Any

ENV = "AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME"
_MODULES = {
    "mcp": {"corpus": "aiscientist_mcp.literature.corpus", "extract": "aiscientist_mcp.literature.extract",
            "search": "aiscientist_mcp.literature.search", "public_search": "aiscientist_mcp.literature.public_search"},
    "legacy": {"corpus": "aiscientist.tools._lib.corpus",
               "extract": "aiscientist.tools.extract_haploinsufficient_genes.tool",
               "search": "aiscientist.tools.search_corpus.tool",
               "public_search": "aiscientist.tools.literature_search.tool"},
}


def runtime() -> str:
    choice = os.environ.get(ENV, "legacy")
    if choice not in _MODULES:
        raise ValueError(f"{ENV} must be mcp or legacy")
    return choice


class Component:
    """Lazy access: importing an operator script never loads a scientific runtime."""
    def __init__(self, name: str):
        if name not in _MODULES["mcp"]:
            raise ValueError("unknown literature maintenance component")
        self.name = name

    def module(self) -> Any:
        """Load only the selected operator runtime, with no cross-runtime fallback."""
        mode = runtime()
        try:
            module = importlib.import_module(_MODULES[mode][self.name])
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Corpus maintenance runtime is unavailable. Run this script inside the configured "
                "published literature image with AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp; "
                "do not install scientific server packages in the gateway."
            ) from exc
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self.module(), name)


def search_publications(query: str, limit: int, *, timeout_s: float = 20.0,
                        abstracts: bool = True, pubmed_only: bool = True) -> dict:
    """Benchmark the published MCP tool, or the explicitly retained legacy baseline.

    Native mode runs only inside the operator's literature image, on the current
    compute node. Fresh scratch prevents result caching from distorting timings.
    """
    if runtime() == "legacy":
        return Component("public_search").search_europepmc(
            query, limit, timeout_s=timeout_s, abstracts=abstracts, pubmed_only=pubmed_only)
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
