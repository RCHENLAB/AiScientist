"""Eye Server retrieval and index synchronisation, without network or a GPU."""
import json
from pathlib import Path

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
    return root












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












def test_vector_transfer_check_uses_only_bounded_metadata(tmp_path, monkeypatch):
    import builtins
    import struct
    cache = tmp_path / "cache"
    cache.mkdir()
    header = repr({"descr": "<f4", "fortran_order": False, "shape": (2, 3)}).encode() + b"\n"
    (cache / "chunk_vecs.npy").write_bytes(b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header + b"\0" * 24)
    (cache / "chunks.jsonl").write_text('{}\n{}\n')
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == "numpy":
            raise AssertionError("gateway metadata check imported NumPy")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    assert lc.vectors_match_chunks(cache)
    (cache / "chunks.jsonl").write_text('{}\n')
    assert not lc.vectors_match_chunks(cache)
    path = cache / "chunk_vecs.npy"
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match="Truncated"):
        lc.vectors_match_chunks(cache)
