"""Planning and operator entry points survive removal of scientific tool folders."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from aiscientist import literature_contract as contract
from aiscientist.gateway import corpus_index

REPO = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def draft():
    return {"target": "  dosage   mechanism ", "definition": "One functional copy is insufficient.",
            "scope": "Human retinal disease.", "counts_when": ["  Explicit   dosage effect "],
            "does_not_count": ["Association alone"], "classes": ["Dosage-Effect", "other"],
            "requests": [f"Authors report a dosage mechanism in retinal disease {i}" for i in range(4)],
            "terms": ["dosage sensitivity", " "]}


def test_draft_contract_and_stable_output_folder():
    data = draft()
    parsed = contract.parse_spec("Draft:\n```json\n" + json.dumps(data) + "\n```")
    assert parsed["target"] == "dosage mechanism"
    assert parsed["classes"] == ["dosage_effect", "other", "not_relevant"]
    assert parsed["counts_when"] == ["Explicit dosage effect"]
    assert parsed["terms"] == ["dosage sensitivity"]
    assert contract.spec_folder(parsed) == contract.spec_folder(dict(reversed(list(parsed.items()))))
    different = dict(parsed, scope="Mouse only")
    assert contract.spec_folder(parsed) != contract.spec_folder(different)
    with pytest.raises(ValueError, match="usable search requests"):
        contract.parse_spec(dict(data, requests=["too short"]))
    with pytest.raises(ValueError, match="target class"):
        contract.parse_spec(dict(data, classes=["other", "not_relevant"]))



def test_platform_query_excludes_upload_and_planner_instructions():
    query = contract.focus_literature_query(
        "Analyze the uploaded DDX41_DEG.h5ad single-cell RNA-seq dataset from Ddx41 "
        "conditional knockout and wild-type mouse retina. Use the existing sampleid, "
        "majorclass, and celltype annotations to compare Ddx41 KO and WT within each "
        "majorclass. Summarize shared and majorclass-specific expression changes, and "
        "include relevant literature context.")
    assert query == "DDX41 conditional knockout mouse retina"
    assert contract.focus_literature_query("Search DDX41 retina Return citations evidence") == "DDX41 retina"


def test_app_and_native_operator_modules_import_without_legacy_science(tmp_path):
    # A fresh interpreter prevents already-imported legacy modules masking a dependency.
    code = r'''
import importlib.abc, importlib.util, os, sys
from pathlib import Path
class BlockScience(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("aiscientist.tools.literature_search", "aiscientist.tools.deep_literature",
            "aiscientist.tools.search_corpus", "aiscientist.tools.extract_haploinsufficient_genes",
            "aiscientist.tools._lib.corpus", "aiscientist_mcp.literature")):
            raise ImportError("Scientific tool module unavailable: " + fullname)
sys.meta_path.insert(0, BlockScience())
from aiscientist.gateway import app, local_corpus, local_literature_mcp
from aiscientist.agents import research_lab
from aiscientist import literature_contract
local_literature_mcp.enabled = lambda: True
sentinel = object()
local_literature_mcp.make_tool = lambda: sentinel
assert local_corpus.make_local_tool() is sentinel
assert literature_contract.focus_literature_query("PRPF31 haploinsufficiency") == "PRPF31 haploinsufficiency"
for name in ("build_mechanism_cache", "eval_corpus_retrieval", "measure_pubmed_depth", "update_corpus_index"):
    spec = importlib.util.spec_from_file_location(name, Path(sys.argv[1]) / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
print("native platform imported without scientific tool folders")
'''
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME="mcp")
    result = subprocess.run([sys.executable, "-c", code, str(REPO)], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "imported without scientific tool folders" in result.stdout


def test_native_index_bundle_contains_only_operator_scripts(tmp_path, monkeypatch):
    import tarfile
    monkeypatch.setenv("AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME", "mcp")
    update = script("update_corpus_index")
    archive = tmp_path / "code.tgz"
    update.pack_code(archive)
    with tarfile.open(archive) as tar:
        assert set(tar.getnames()) == {"scripts/build_mechanism_cache.py", "scripts/_literature_runtime.py"}
    image = "docker://ghcr.io/example/mcp-literature@sha256:" + "a" * 64
    monkeypatch.setenv("AISCIENTIST_CORPUS_MAINTENANCE_IMAGE", image)
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", "/refs/retigene/papers")
    assert update.corpus_settings()[-1] == image
    monkeypatch.setenv("AISCIENTIST_CORPUS_MAINTENANCE_IMAGE", "docker://example/literature:latest")
    with pytest.raises(SystemExit, match="digest-pinned"):
        update.corpus_settings()
    monkeypatch.setenv("AISCIENTIST_CORPUS_MAINTENANCE_IMAGE", image)
    command = corpus_index.rebuild_inner_command(stage="/stage", archive="/stage/code.tgz", image=image,
        root="/refs/retigene", papers="/refs/retigene/papers", manifest="/refs/retigene/manifest.csv", runtime="mcp")
    assert "AISCIENTIST_CORPUS_MAINTENANCE_RUNTIME=mcp" in command
    assert "HF_HUB_OFFLINE=1" in command and image in command
    assert "LITERATURE_CORPUS_ROOT=/refs/retigene" in command
    assert "LITERATURE_ALLOWED_ROOTS=/refs/retigene:/refs/retigene/papers" in command
    assert "python3 /stage/scripts/build_mechanism_cache.py --replace" in command


def test_native_operator_loader_cannot_silently_fall_back(monkeypatch):
    runtime = script("_literature_runtime")
    monkeypatch.setenv(runtime.ENV, "mcp")
    calls = []
    def absent(name):
        calls.append(name)
        raise ModuleNotFoundError(name)
    monkeypatch.setattr(runtime.importlib, "import_module", absent)
    with pytest.raises(RuntimeError, match="published literature image"):
        runtime.Component("corpus").load_chunks
    assert calls == ["aiscientist_mcp.literature.corpus"]
    monkeypatch.setenv(runtime.ENV, "typo")
    with pytest.raises(ValueError, match="mcp or legacy"):
        runtime.runtime()


def test_native_publication_measurement_uses_real_mcp_contract(monkeypatch, tmp_path):
    runtime = script("_literature_runtime")
    monkeypatch.setenv(runtime.ENV, "mcp")
    events = []
    class Parameters:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
    class Client:
        def __init__(self, parameters, **kwargs):
            self.root = Path(parameters.kwargs["env"]["AISCI_DATA_DIR"])
            assert parameters.kwargs["command"] == "literature-mcp"
            assert parameters.kwargs["env"]["AISCI_EXECUTOR"] == "inline"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def list_tools(self):
            events.append("list")
            return types.SimpleNamespace(tools=[types.SimpleNamespace(name="literature_search")])
        async def call_tool(self, name, args):
            events.append((name, args))
            full = self.root / "full.json"
            full.write_text(json.dumps({"status": "ok", "results": [{"pmid": "123", "abstract": "full evidence"}]}))
            return types.SimpleNamespace(is_error=False, structured_content={"status": "ok", "response_omitted": True,
                "results": [], "full_result": str(full)})
    monkeypatch.setitem(sys.modules, "mcp", types.SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", types.SimpleNamespace(StdioServerParameters=Parameters))
    result = runtime.search_publications("PRPF31 deletion", 12)
    assert result["results"][0]["abstract"] == "full evidence"
    assert events[0] == "list"
    assert events[1] == ("literature_search", {"query": "PRPF31 deletion", "limit": 12, "abstracts": True,
        "pubmed_only": True, "analysis_id": "pubmed-depth", "rerun": True})


def test_depth_operator_bundle_retains_application_selection_without_app_imports(tmp_path):
    import shutil
    files = ["scripts/measure_pubmed_depth.py", "scripts/_literature_runtime.py",
             "src/aiscientist/literature_contract.py", "src/aiscientist/agents/literature_answer.py",
             "src/aiscientist/agents/loop_utils.py"]
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, target)
    code = r'''
import importlib.abc, runpy, sys
class NoApp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "aiscientist" or fullname.startswith("aiscientist."):
            raise ImportError("No application package in operator image")
sys.meta_path.insert(0, NoApp())
module = runpy.run_path(sys.argv[1], run_name="operator_import")
calls = []
def search(query, limit):
    calls.append((query,limit))
    return {"status":"ok", "results":[
        {"title":"First", "doi":"10.1/SAME", "pmid":"21529959"},
        {"title":"Duplicate DOI", "doi":"10.1/same", "pmid":"other"},
        {"title":"Second", "pmid":"12906118"},
        *[{"title":"Paper "+str(i), "pmid":str(i)} for i in range(20)],
    ]}
result = module["measure"]("PRPF31", search)
assert calls == [("PRPF31",12)]
assert result["kept"] == 12
assert result["reference_pmids"] == {"21529959":True,"12906118":True,"35433063":False}
print("bounded application selection retained")
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / files[0])],
                            cwd=tmp_path, env=dict(os.environ, PYTHONPATH=""),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "application selection retained" in result.stdout
