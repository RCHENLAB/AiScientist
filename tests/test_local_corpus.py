"""Eye Server retrieval and index synchronisation, without network or a GPU."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiscientist.gateway import local_corpus as lc


@pytest.fixture
def index(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    root = tmp_path / "literature"
    cache = root / "mechanism_cache"
    cache.mkdir(parents=True)
    (cache / "chunks.jsonl").write_text(json.dumps({
        "pmid": "123", "page": 6, "chunk": 0, "text": "PRPF31 causes disease."}) + "\n")
    np.save(cache / "chunk_vecs.npy", np.array([[1., 0.]], dtype="float32"))
    (root / "manifest.csv").write_text("doc_id,title,citation\n123,PRPF31 paper,Smith 2020\n")
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("AISCIENTIST_LOCAL_CORPUS_ROOT", raising=False)
    monkeypatch.setattr(lc, "_EMBEDDERS", {})
    monkeypatch.setattr(lc, "_LOAD_FAILURES", {})
    return root


def test_offline_cpu_model_is_loaded_once_and_result_matches_tool_contract(index, monkeypatch):
    calls = []

    class Model:
        def __init__(self, name, **kwargs):
            calls.append((name, kwargs))

        def encode(self, texts, **kwargs):
            return [[1., 0.] for _ in texts]

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model))
    before = dict(__import__("os").environ)
    tool = lc.make_local_tool()
    out = tool.executor({"queries": ["What does PRPF31 cause?"]}, None)
    again = lc.run_local_search({"queries": ["PRPF31 disease"]})
    assert len(calls) == 1
    assert calls[0][1]["local_files_only"] is True
    assert calls[0][1]["device"] == "cpu"
    assert dict(__import__("os").environ) == before
    assert out["status"] == again["status"] == "ok"
    assert out["contexts"][0]["text"] == "PRPF31 causes disease."
    assert out["contexts"][0]["page"] == 6
    assert out["contexts"][0]["citation"] == "Smith 2020"
    assert out["raw_data_to_llm"] is False


def test_missing_model_or_index_requests_hpc_fallback(index, monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert lc.make_local_tool() is None
    assert lc.run_local_search({"queries": ["PRPF31"]}) is None
    (index / "mechanism_cache" / "chunk_vecs.npy").unlink()
    assert not lc.has_local_index()
    assert lc.run_local_search({"queries": ["PRPF31"]}) is None


def test_explicit_local_root_and_broken_index_fall_back(index, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_LOCAL_CORPUS_ROOT", str(index))
    monkeypatch.setenv("AISCIENTIST_DATA_ROOT", "unused")
    monkeypatch.setattr(lc, "_embedder", lambda root: lambda texts: [[1., 0.]])
    (index / "mechanism_cache" / "chunks.jsonl").write_text("broken json")
    assert lc.has_local_index()
    assert lc.run_local_search({"queries": ["PRPF31"]}) is None


def test_sync_uses_checksum_incremental_transfer_and_includes_manifest(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_corpus_index.py"
    spec = importlib.util.spec_from_file_location("sync_corpus_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    module.sync("me@access-hpc3.rcic.uci.edu:/corpus", tmp_path, with_model=True, with_hgnc=True)
    assert len(calls) == 4
    dtn = "me@access-hpc3.rcic.uci.edu:/corpus"
    assert [call[0][-2] for call in calls] == [
        f"{dtn}/mechanism_cache/", f"{dtn}/paperqa_manifest.csv",
        f"{dtn}/hf_cache/", f"{dtn}/hgnc_complete_set.txt"]
    assert calls[1][0][-1] == str(tmp_path / "manifest.csv")
    assert all("-rltc" in argv and "--delay-updates" in argv and kw["check"] for argv, kw in calls)
    assert all("--delete" not in argv for argv, _ in calls)


def test_failed_model_load_is_cached_until_retry_window(index, monkeypatch):
    attempts = []
    now = [100.0]

    class MissingModel:
        def __init__(self, *args, **kwargs):
            attempts.append(now[0])
            raise OSError("offline model is missing")

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=MissingModel))
    monkeypatch.setattr(lc.time, "monotonic", lambda: now[0])
    assert lc.make_local_tool() is None
    assert lc.make_local_tool() is None
    assert len(attempts) == 1
    now[0] += lc._LOAD_RETRY_S + 1
    assert lc.make_local_tool() is None
    assert len(attempts) == 2


def _sync_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_corpus_index.py"
    spec = importlib.util.spec_from_file_location("sync_corpus_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("source", ["hpc3:/corpus", "me@hpc3.rcic.uci.edu:/corpus",
                                    "login-i15.rcic.uci.edu:/corpus", "HPC3:/corpus"])
def test_sync_refuses_an_hpc3_login_node(tmp_path, monkeypatch, source):
    # RCIC allows no data transfer on the login nodes: the copy goes through access-hpc3.
    module = _sync_module()
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: pytest.fail("rsync started"))
    with pytest.raises(ValueError, match="access-hpc3.rcic.uci.edu"):
        module.sync(source, tmp_path)


def test_a_local_source_path_is_not_a_host(tmp_path, monkeypatch):
    module = _sync_module()
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kw: calls.append(argv))
    module.sync(str(tmp_path / "hpc3:copy"), tmp_path / "out")
    assert len(calls) == 2


def test_vectors_for_another_index_are_not_searched_and_never_re_embedded(index, monkeypatch):
    np = pytest.importorskip("numpy")
    np.save(index / "mechanism_cache" / "chunk_vecs.npy", np.zeros((3, 2), dtype="float32"))
    monkeypatch.setattr(lc, "_embedder",
                        lambda root: lambda texts: pytest.fail(f"embedded {len(texts)} texts"))
    assert lc.run_local_search({"queries": ["PRPF31"]}) is None    # the HPC3 fallback answers
