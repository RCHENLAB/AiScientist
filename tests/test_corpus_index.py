"""The Eye Server copy of the RetiGene index (gateway/corpus_index.py) and the build that replaces
the HPC3 index (scripts/build_mechanism_cache.py --replace), with a stand-in for rsync and no model."""
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from aiscientist.gateway import corpus_index as ci  # noqa: E402
from aiscientist.gateway import local_corpus as lc  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _put(src: Path, dst: Path) -> None:
    """What rsync -c -t does with one file: an identical file only gets the source's time; a changed
    one is written beside the destination and renamed over it (a hard link to it is left alone)."""
    st = src.stat()
    if not (dst.is_file() and dst.read_bytes() == src.read_bytes()):
        tmp = dst.with_name(f".{dst.name}.tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def fake_rsync(calls):
    def run(argv, check):
        calls.append(argv)
        assert check is True
        src, dst = argv[-2], Path(argv[-1])
        if src.endswith("/"):
            names = set()
            for p in Path(src).iterdir():
                names.add(p.name)
                if p.is_dir():
                    shutil.copytree(p, dst / p.name, dirs_exist_ok=True)
                else:
                    _put(p, dst / p.name)
            if "--delete" in argv:
                for q in dst.iterdir():
                    if q.name not in names:
                        q.unlink()
        else:
            _put(Path(src), dst)
    return run


def write_cache(cache: Path, n_chunks: int, *, vec_rows: int | None = None) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "chunks.jsonl").write_text("".join(
        json.dumps({"pmid": str(i % 3), "page": 1, "chunk": i, "text": f"chunk {i}"}) + "\n"
        for i in range(n_chunks)))
    (cache / "sentences.jsonl").write_text('{"s": 1}\n' * n_chunks)
    np.save(cache / "chunk_vecs.npy", np.ones((n_chunks if vec_rows is None else vec_rows, 2), dtype="float32"))


def write_index(root: Path, n_chunks: int, *, vec_rows: int | None = None, manifest="doc_id,title\n1,A\n"):
    write_cache(root / "mechanism_cache", n_chunks, vec_rows=vec_rows)
    (root / "paperqa_manifest.csv").write_text(manifest)
    (root / "hgnc_complete_set.txt").write_text("symbol\nPRPF31\n")
    (root / "hf_cache" / "hub").mkdir(parents=True, exist_ok=True)
    (root / "hf_cache" / "hub" / "model.bin").write_text("weights")


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "hpc3" / "retigene"
    write_index(src, 3)
    return src


def test_first_copy_is_a_version_named_by_current(tmp_path, source, monkeypatch):
    calls = []
    root = tmp_path / "eye" / "reference" / "retigene"
    res = ci.sync(str(source), root, with_model=True, with_hgnc=True, run=fake_rsync(calls))
    assert res.changed and res.chunks == 3 and res.papers == 3
    assert (root / "current").is_symlink() and ci.current(root) == res.version
    assert sorted(p.name for p in res.version.iterdir()) == [
        "SOURCE.json", "hgnc_complete_set.txt", "manifest.csv", "mechanism_cache"]
    assert (root / "hf_cache" / "hub" / "model.bin").is_file()       # the model is not versioned
    meta = json.loads((res.version / "SOURCE.json").read_text())
    assert meta["chunks"] == 3 and meta["source"] == str(source)
    # The gateway reads it: the copy is the source's index file by file (rsync keeps the times).
    monkeypatch.setenv("AISCIENTIST_LOCAL_CORPUS_ROOT", str(root))
    assert lc.has_local_index() and lc.index_dir() == res.version
    st = (source / "mechanism_cache" / "chunks.jsonl").stat()
    fp = {"mechanism_cache/chunks.jsonl": f"{st.st_size} {int(st.st_mtime)}"}
    assert lc.differences(fp) == []


def test_rsync_goes_through_the_transfer_node_with_checksums_and_a_key(tmp_path):
    calls = []
    with pytest.raises(ci.SyncError, match="chunks.jsonl is missing"):   # only the commands are looked at
        ci.sync("me@access-hpc3.rcic.uci.edu:/corpus", tmp_path / "c", with_model=True, with_hgnc=True,
                ssh=ci.ssh_command(key="/state/key"), run=lambda argv, check: calls.append(argv))
    dtn = "me@access-hpc3.rcic.uci.edu:/corpus"
    assert [argv[-2] for argv in calls] == [f"{dtn}/mechanism_cache/", f"{dtn}/paperqa_manifest.csv",
                                            f"{dtn}/hgnc_complete_set.txt", f"{dtn}/hf_cache/"]
    assert all(argv[:4] == ["rsync", "-rltc", "--delay-updates", "--protect-args"] for argv in calls)
    # --delete only inside the new version's mechanism_cache/, never in the model or the root.
    assert ["--delete" in argv for argv in calls] == [True, False, False, False]
    ssh = calls[0][calls[0].index("-e") + 1]
    assert "BatchMode=yes" in ssh and "-i /state/key" in ssh
    assert not list((tmp_path / "c" / "versions").iterdir())        # the failed copy left nothing


@pytest.mark.parametrize("source", ["hpc3:/corpus", "me@hpc3.rcic.uci.edu:/corpus",
                                    "login-i15.rcic.uci.edu:/corpus", "HPC3:/corpus"])
def test_an_hpc3_login_node_is_refused(tmp_path, source):
    # RCIC allows no data transfer on the login nodes: the copy goes through access-hpc3.
    with pytest.raises(ValueError, match="access-hpc3.rcic.uci.edu"):
        ci.sync(source, tmp_path, run=lambda *a, **k: pytest.fail("rsync started"))


def test_a_local_path_with_a_colon_is_not_a_host():
    assert ci.source_host("/tmp/hpc3:copy") == ""
    assert ci.source_host("me@access-hpc3.rcic.uci.edu:/corpus") == "access-hpc3.rcic.uci.edu"


def test_an_unchanged_source_switches_nothing(tmp_path, source):
    root = tmp_path / "copy"
    first = ci.sync(str(source), root, run=fake_rsync([]))
    again = ci.sync(str(source), root, run=fake_rsync([]))
    assert not again.changed and again.version == first.version
    assert ci.versions(root) == [first.version]


def test_new_papers_make_a_new_version_and_the_searched_one_is_never_touched(tmp_path, source):
    root = tmp_path / "copy"
    clock = iter([1_000_000_000.0 + 60 * i for i in range(20)])
    tick = lambda: next(clock)  # noqa: E731
    first = ci.sync(str(source), root, run=fake_rsync([]), clock=tick)
    old_chunks = (first.version / "mechanism_cache" / "chunks.jsonl").read_bytes()
    old_manifest_inode = (first.version / "manifest.csv").stat().st_ino
    write_index(source, 5)
    (source / "paperqa_manifest.csv").write_text("doc_id,title\n1,A\n")    # same manifest bytes
    second = ci.sync(str(source), root, run=fake_rsync([]), clock=tick)
    assert second.changed and second.version != first.version and second.chunks == 5
    assert ci.current(root) == second.version
    assert (first.version / "mechanism_cache" / "chunks.jsonl").read_bytes() == old_chunks
    assert "mechanism_cache/chunks.jsonl" in second.files and "manifest.csv" not in second.files
    assert (second.version / "manifest.csv").stat().st_ino == old_manifest_inode    # a link, no copy
    # A third version: the oldest goes, the previous one stays for a rollback.
    write_index(source, 7)
    third = ci.sync(str(source), root, run=fake_rsync([]), clock=tick)
    assert ci.versions(root) == [second.version, third.version]
    assert ci.rollback(root) == second.version and ci.current(root) == second.version
    with pytest.raises(ci.SyncError):
        ci.rollback(root)


def test_a_copy_that_cannot_be_searched_never_becomes_current(tmp_path, source):
    root = tmp_path / "copy"
    first = ci.sync(str(source), root, run=fake_rsync([]))
    write_index(source, 4, vec_rows=2)            # caught between two builds on HPC3
    with pytest.raises(ci.SyncError, match="one row per line"):
        ci.sync(str(source), root, run=fake_rsync([]))
    assert ci.current(root) == first.version and ci.versions(root) == [first.version]
    assert not [p for p in (root / "versions").iterdir() if p.name.startswith(".incoming")]


def test_one_sync_at_a_time(tmp_path, source):
    import fcntl

    root = tmp_path / "copy"
    root.mkdir()
    with open(root / ".sync.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(ci.SyncError, match="another sync"):
            ci.sync(str(source), root, run=fake_rsync([]))


def test_the_rebuild_job_runs_the_build_beside_the_index_inside_the_image():
    cmd = ci.rebuild_inner_command(stage="/dfs3b/sw/Temp/u/corpus_index/1", archive="/dfs3b/sw/Temp/u/corpus_index/1/code.tgz",
                                   image="/dfs3b/sw/retigene/paperqa.sif", root="/dfs3b/sw/retigene",
                                   papers="/dfs3b/sw/retigene/papers",
                                   manifest="/dfs3b/sw/retigene/paperqa_manifest.csv")
    unpack, run = cmd.split("\n")
    assert unpack.startswith("tar xzf /dfs3b/sw/Temp/u/corpus_index/1/code.tgz -C ")
    assert "--containall" in run and "--network none" in run
    assert "-B /dfs3b/sw/retigene:/dfs3b/sw/retigene " in run          # written: the index
    assert "-B /dfs3b/sw/Temp/u/corpus_index/1:/dfs3b/sw/Temp/u/corpus_index/1:ro" in run
    assert "papers:/dfs3b" not in run                                  # inside the root already
    assert "build_mechanism_cache.py --replace --papers /dfs3b/sw/retigene/papers" in run
    assert "HF_HUB_OFFLINE=1" in run
    log = "extracted\nAISCIENTIST_INDEX_BUILD {\"changed\": true, \"chunks\": 9}\n"
    assert ci.parse_rebuild_log(log) == {"changed": True, "chunks": 9}
    assert ci.parse_rebuild_log("Killed\n") is None


# --- build_mechanism_cache.py --replace -------------------------------------------------------

@pytest.fixture
def build_script():
    return _script("build_mechanism_cache")


def test_replace_puts_a_checked_build_in_place_and_keeps_the_old_one(tmp_path, build_script):
    out = tmp_path / "retigene" / "mechanism_cache"
    write_index(out.parent, 3)
    summary = build_script.build_beside(out, lambda nxt: write_cache(nxt, 5))
    assert summary["changed"] and summary["chunks"] == 5 and summary["new_chunks"] == 2
    assert sum(1 for _ in (out / "chunks.jsonl").open()) == 5
    assert sum(1 for _ in (out.with_name("mechanism_cache.prev") / "chunks.jsonl").open()) == 3
    assert not out.with_name("mechanism_cache.next").exists()


def test_replace_without_new_papers_changes_nothing(tmp_path, build_script):
    out = tmp_path / "retigene" / "mechanism_cache"
    write_index(out.parent, 3)
    inode = (out / "chunks.jsonl").stat().st_ino
    summary = build_script.build_beside(out, lambda nxt: None)
    assert not summary["changed"] and (out / "chunks.jsonl").stat().st_ino == inode
    assert not out.with_name("mechanism_cache.next").exists()
    assert not out.with_name("mechanism_cache.prev").exists()


@pytest.mark.parametrize("bad", ["raises", "vectors short"])
def test_a_failed_build_leaves_the_index_alone(tmp_path, build_script, bad):
    out = tmp_path / "retigene" / "mechanism_cache"
    write_index(out.parent, 3)
    before = (out / "chunks.jsonl").read_bytes()

    def build(nxt):
        if bad == "raises":
            raise RuntimeError("model missing")
        (nxt / "chunks.jsonl").write_text((out / "chunks.jsonl").read_text() * 2)

    with pytest.raises(RuntimeError):
        build_script.build_beside(out, build)
    assert (out / "chunks.jsonl").read_bytes() == before
    assert not out.with_name("mechanism_cache.next").exists()


def test_only_new_chunks_are_embedded(tmp_path, build_script):
    cache = tmp_path / "mechanism_cache"
    cache.mkdir()
    chunks = [{"text": f"chunk {i}"} for i in range(6)]

    def embed(texts):
        seen.extend(texts)
        return np.array([[float(t.split()[-1]) + 1.0, 1.0] for t in texts], dtype="float32")

    seen = []
    np.save(cache / "chunk_vecs.npy", build_script.l2(embed([c["text"] for c in chunks[:4]])))
    seen.clear()
    assert build_script.extend_vectors(chunks, cache, embed, lambda s: None)
    vecs = np.load(cache / "chunk_vecs.npy")
    assert vecs.shape == (6, 2)
    assert np.allclose(vecs, build_script.l2(embed([c["text"] for c in chunks])))
    assert seen[:6] == ["chunk 0", "chunk 1", "chunk 2", "chunk 3", "chunk 4", "chunk 5"]  # 4 checked + 2 new
    # Cached vectors of other chunks: everything is embedded again by the caller.
    np.save(cache / "chunk_vecs.npy", np.tile(np.array([[0.0, 1.0]], dtype="float32"), (4, 1)))
    assert not build_script.extend_vectors(chunks, cache, embed, lambda s: None)
    assert np.load(cache / "chunk_vecs.npy").shape == (4, 2)


# --- update_corpus_index.py ----------------------------------------------------------------------

class FakeHPC3:
    """Answers corpus_scan.corpus_fingerprint's one command from a local index."""

    def __init__(self, root: Path):
        self.root = root

    def exec(self, command, timeout=60.0):
        import hashlib
        from types import SimpleNamespace

        lines = [hashlib.sha256((self.root / "paperqa_manifest.csv").read_bytes()).hexdigest()]
        for name in ("mechanism_cache/sentences.jsonl", "mechanism_cache/chunks.jsonl",
                     "mechanism_cache/chunk_vecs.npy", "hgnc_complete_set.txt"):
            st = (self.root / name).stat()
            lines.append(f"{name} {st.st_size} {int(st.st_mtime)}")
        return SimpleNamespace(ok=True, stdout="\n".join(lines) + "\n", stderr="")


def test_check_says_whether_the_copy_is_the_hpc3_index(tmp_path, source, capsys):
    update = _script("update_corpus_index")
    root = tmp_path / "copy"
    hpc3 = FakeHPC3(source)
    papers, manifest = str(source / "papers"), str(source / "paperqa_manifest.csv")
    assert update.check(hpc3, root, papers, manifest) == 1                    # no copy yet
    ci.sync(str(source), root, with_hgnc=True, run=fake_rsync([]))
    assert update.check(hpc3, root, papers, manifest) == 0
    write_index(source, 4)                                                   # papers added on HPC3
    assert update.check(hpc3, root, papers, manifest) == 1
    assert "stale" in capsys.readouterr().out


def test_the_update_needs_an_unattended_hpc3_login(monkeypatch):
    update = _script("update_corpus_index")
    from aiscientist.gateway.settings import HPCSettings
    for var in ("AISCIENTIST_SHARED_LLM_OWNER", "AISCIENTIST_SHARED_LLM_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    assert update.identity("me", "/k", HPCSettings()) == ("me", "/k")
    with pytest.raises(SystemExit, match="together"):
        update.identity("me", "", HPCSettings())
    with pytest.raises(SystemExit, match="No HPC3 login"):
        update.identity("", "", HPCSettings())


def test_the_corpus_settings_come_from_the_deployment(monkeypatch):
    update = _script("update_corpus_index")
    monkeypatch.setenv("AISCIENTIST_PAPERQA_PAPERS", "/dfs3b/r/retigene/papers/")
    monkeypatch.setenv("AISCIENTIST_PAPERQA_MANIFEST", "/dfs3b/r/retigene/paperqa_manifest.csv")
    monkeypatch.setenv("AISCIENTIST_PAPERQA_IMAGE", "/dfs3b/r/retigene/paperqa.sif")
    assert update.corpus_settings() == ("/dfs3b/r/retigene", "/dfs3b/r/retigene/papers",
                                        "/dfs3b/r/retigene/paperqa_manifest.csv", "/dfs3b/r/retigene/paperqa.sif")
    assert update.corpus_settings("/dfs3b/me/test")[:3] == (
        "/dfs3b/me/test", "/dfs3b/me/test/papers", "/dfs3b/me/test/paperqa_manifest.csv")


def test_the_build_job_ships_the_build_script_and_the_package(tmp_path):
    import tarfile

    update = _script("update_corpus_index")
    archive = tmp_path / "code.tgz"
    update.pack_code(archive)
    names = tarfile.open(archive).getnames()
    assert "scripts/build_mechanism_cache.py" in names
    assert "src/aiscientist/tools/extract_haploinsufficient_genes/tool.py" in names
    assert not [n for n in names if "__pycache__" in n]


def test_a_previous_index_another_member_owns_is_set_aside_not_deleted(tmp_path, build_script, monkeypatch):
    out = tmp_path / "retigene" / "mechanism_cache"
    write_index(out.parent, 3)
    build_script.build_beside(out, lambda nxt: write_cache(nxt, 4))      # .prev now exists
    assert (out.stat().st_mode & 0o7777) == 0o2775                     # the next member may replace it
    assert ((out / "chunks.jsonl").stat().st_mode & 0o777) == 0o664
    real_rmtree = build_script.shutil.rmtree

    def rmtree(path, *a, **k):
        if Path(path).name == "mechanism_cache.prev":
            raise PermissionError("owned by another member")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(build_script.shutil, "rmtree", rmtree)
    summary = build_script.build_beside(out, lambda nxt: write_cache(nxt, 5))
    assert summary["changed"] and Path(summary["not_removed"]).name.startswith("mechanism_cache.prev-")
    assert sum(1 for _ in (out / "chunks.jsonl").open()) == 5
    assert sum(1 for _ in (out.with_name("mechanism_cache.prev") / "chunks.jsonl").open()) == 4
