"""Eye Server retrieval and index synchronisation, without network or a GPU."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiscientist.gateway import local_corpus as lc


@pytest.fixture
def index(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    root = tmp_path / "reference" / "retigene"     # the default under the data root
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


def test_vectors_for_another_index_are_not_searched_and_never_re_embedded(index, monkeypatch):
    np = pytest.importorskip("numpy")
    np.save(index / "mechanism_cache" / "chunk_vecs.npy", np.zeros((3, 2), dtype="float32"))
    monkeypatch.setattr(lc, "_embedder",
                        lambda root: lambda texts: pytest.fail(f"embedded {len(texts)} texts"))
    assert lc.run_local_search({"queries": ["PRPF31"]}) is None    # the HPC3 fallback answers


def _fingerprint(index, **override):
    """What corpus_scan.corpus_fingerprint reports for an HPC3 index identical to ``index``."""
    import hashlib

    fp = {"root": "/dfs3b/corpus"}
    for name in ("mechanism_cache/chunks.jsonl", "mechanism_cache/chunk_vecs.npy"):
        st = (index / name).stat()
        fp[name] = f"{st.st_size} {int(st.st_mtime)}"
    fp["manifest_sha256"] = hashlib.sha256((index / "manifest.csv").read_bytes()).hexdigest()
    fp.update(override)
    return fp


def test_a_copy_is_compared_with_the_hpc3_index_file_by_file(index):
    assert lc.differences(_fingerprint(index), index) == []
    assert lc.differences(_fingerprint(index, **{"mechanism_cache/chunks.jsonl": "1 2"}), index) == [
        "mechanism_cache/chunks.jsonl"]
    assert lc.differences(_fingerprint(index, manifest_sha256="0" * 64), index) == ["manifest.csv"]
    # HPC3 lists an HGNC table and sentences: the copy has no HGNC table (the search runs without
    # one), so only the missing sentences count.
    fp = _fingerprint(index, **{"hgnc_complete_set.txt": "10 20", "mechanism_cache/sentences.jsonl": "3 4"})
    assert lc.differences(fp, index) == ["mechanism_cache/sentences.jsonl"]


def test_the_copy_in_current_is_the_one_searched(tmp_path, monkeypatch):
    root = tmp_path / "copy"
    version = root / "versions" / "20261029T000000Z"
    (version / "mechanism_cache").mkdir(parents=True)
    for name in ("chunks.jsonl", "chunk_vecs.npy"):
        (version / "mechanism_cache" / name).write_text("x")
    (root / "current").symlink_to(Path("versions") / version.name)
    monkeypatch.setenv("AISCIENTIST_LOCAL_CORPUS_ROOT", str(root))
    assert lc.index_dir() == version.resolve()
    assert lc.has_local_index()
    (root / "current").unlink()
    assert lc.index_dir() == root and not lc.has_local_index()


def test_staleness_is_read_from_hpc3_at_most_once_per_interval(index, monkeypatch):
    from aiscientist.gateway import corpus_scan

    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", "/dfs3b/corpus/papers")
    monkeypatch.setattr(lc, "_FINGERPRINTS", {})
    now = [1000.0]
    monkeypatch.setattr(lc.time, "monotonic", lambda: now[0])
    reads = []
    answer = {"fp": _fingerprint(index)}

    def fingerprint(executor, papers, manifest=""):
        reads.append(papers)
        return answer["fp"]

    monkeypatch.setattr(corpus_scan, "corpus_fingerprint", fingerprint)
    assert lc.stale(object()) == []
    answer["fp"] = _fingerprint(index, **{"mechanism_cache/chunk_vecs.npy": "9 9"})   # papers added on HPC3
    assert lc.stale(object()) == [] and len(reads) == 1                             # still trusted
    now[0] += lc.FINGERPRINT_TTL_S + 1
    assert lc.stale(object()) == ["mechanism_cache/chunk_vecs.npy"] and len(reads) == 2
    # No session on HPC3, or HPC3 cannot say: the copy is searched as it is.
    assert lc.stale(None) == []
    answer["fp"] = None
    now[0] += lc.FINGERPRINT_TTL_S + 1
    assert lc.stale(object()) == []


def test_the_copy_text_is_built_once_per_version(index, monkeypatch):
    from aiscientist.tools.search_corpus import tool as search_tool

    monkeypatch.setattr(lc, "_TEXT", {})
    monkeypatch.setattr(lc, "_embedder", lambda root: lambda texts: [[1., 0.] for _ in texts])
    built = []
    real = search_tool.preload

    def preload(paths):
        built.append(paths.cache)
        return real(paths)

    monkeypatch.setattr(lc, "preload_search_corpus", preload)
    first = lc.run_local_search({"queries": ["PRPF31 disease"]})
    second = lc.run_local_search({"queries": ["What does PRPF31 cause?"]})
    assert first["status"] == second["status"] == "ok" and len(built) == 1
    assert second["contexts"][0]["text"] == "PRPF31 causes disease."
    # A new version of the copy (here: the same folder rewritten) is loaded again.
    (index / "mechanism_cache" / "chunks.jsonl").write_text(json.dumps({
        "pmid": "123", "page": 7, "chunk": 0, "text": "PRPF31 haploinsufficiency."}) + "\n")
    third = lc.run_local_search({"queries": ["PRPF31"]})
    assert len(built) == 2 and third["contexts"][0]["page"] == 7


def test_the_word_index_memo_stays_bounded(monkeypatch):
    from aiscientist.tools._lib.corpus import TextIndex, parse_terms

    idx = TextIndex(["alpha beta", "beta gamma"])
    monkeypatch.setattr(TextIndex, "MEMO_MAX", 3)
    for word in ("alpha", "beta", "gamma", "delta", "alpha"):
        idx.counts(parse_terms([word])[0])
        assert len(idx._counts) <= 3
    assert idx.counts(parse_terms(["beta"])[0]) == {0: 1, 1: 1}
