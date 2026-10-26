"""The archive unpacker that runs on an HPC3 compute node (src/aiscientist/hpc/unpack_archive.py).

It is executed as a FILE there, so it is executed as a file here: a subprocess, real zip and tar
archives, a real directory tree. An uploaded archive is untrusted input, and what is pinned is what
that must never be able to do — write outside its destination, fill the disk, or leave a half-unpacked
folder that looks complete.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "aiscientist" / "hpc" / "unpack_archive.py"


def run(archive: Path, dest: Path, *extra: str) -> tuple[dict, int]:
    proc = subprocess.run([sys.executable, str(SCRIPT), "--archive", str(archive), "--dest", str(dest), *extra],
                          capture_output=True, text=True)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("AISCIENTIST_UNPACK_JSON ")]
    assert len(lines) == 1, (proc.stdout, proc.stderr)
    return json.loads(lines[0].split(" ", 1)[1]), proc.returncode


def make_zip(path: Path, entries: dict[str, bytes], compress: int = zipfile.ZIP_DEFLATED) -> Path:
    with zipfile.ZipFile(path, "w", compress) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def make_tar(path: Path, entries: dict[str, bytes], mode: str = "w:gz") -> Path:
    with tarfile.open(path, mode) as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# -- the normal cases ----------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["zip", "tar.gz", "tar", "tar.bz2"])
def test_an_archive_unpacks_to_its_files_and_is_removed(tmp_path, kind):
    entries = {"a.txt": b"alpha", "sub/b.bin": b"\x00" * 1000, "sub/deep/c.csv": "é,ü\n1,2\n".encode()}
    archive = tmp_path / f"data.{kind}"
    if kind == "zip":
        make_zip(archive, entries)
    else:
        make_tar(archive, entries, {"tar.gz": "w:gz", "tar": "w", "tar.bz2": "w:bz2"}[kind])
    result, code = run(archive, tmp_path / "data")
    assert code == 0 and result["status"] == "ok"
    assert result["files"] == 3 and result["bytes"] == 5 + 1000 + len(entries["sub/deep/c.csv"])
    assert tree(tmp_path / "data") == entries
    assert result["archive_removed"] is True and not archive.exists()
    assert not (tmp_path / "data.unpacking").exists()


def test_a_single_top_level_folder_is_lifted(tmp_path):
    """``run1.zip`` holding ``run1/…`` must not become ``run1/run1/…``."""
    archive = make_zip(tmp_path / "run1.zip", {"run1/outs/matrix.h5": b"m", "run1/web_summary.html": b"w"})
    result, _ = run(archive, tmp_path / "run1")
    assert result["flattened"] is True
    assert tree(tmp_path / "run1") == {"outs/matrix.h5": b"m", "web_summary.html": b"w"}


def test_two_top_level_entries_are_kept_as_they_are(tmp_path):
    archive = make_zip(tmp_path / "x.zip", {"a/1.txt": b"1", "b/2.txt": b"2"})
    result, _ = run(archive, tmp_path / "x")
    assert result["flattened"] is False and set(tree(tmp_path / "x")) == {"a/1.txt", "b/2.txt"}


def test_macos_resource_forks_are_left_out(tmp_path):
    archive = make_zip(tmp_path / "m.zip", {"d/real.csv": b"1", "__MACOSX/d/._real.csv": b"junk",
                                            "d/.DS_Store": b"junk", "d/._real.csv": b"junk"})
    result, _ = run(archive, tmp_path / "m")
    assert result["skipped_junk"] == 3 and result["files"] == 1
    assert tree(tmp_path / "m") == {"real.csv": b"1"}             # and the single root was lifted


def test_keep_archive_leaves_the_archive(tmp_path):
    archive = make_zip(tmp_path / "k.zip", {"a.txt": b"a", "b.txt": b"b"})
    result, _ = run(archive, tmp_path / "k", "--keep-archive")
    assert result["archive_removed"] is False and archive.exists()


# -- what an archive must not be able to do ---------------------------------------------------------

@pytest.mark.parametrize("name", ["../evil.txt", "ok/../../evil.txt", "/etc/evil.txt", "..\\evil.txt"])
def test_an_entry_that_leaves_the_destination_refuses_the_whole_archive(tmp_path, name):
    work = tmp_path / "work"
    work.mkdir()
    archive = make_zip(work / "slip.zip", {"good.txt": b"g", name: b"evil"})
    result, code = run(archive, work / "slip")
    assert code == 3 and result["status"] == "refused"
    assert "absolute path" in result["reason"] or "'..'" in result["reason"]
    assert not (work / "slip").exists() and not (work / "slip.unpacking").exists()   # nothing half-done
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["slip.zip"]   # nothing escaped
    assert archive.exists()                                        # a refused archive is kept


def test_a_tar_entry_that_leaves_the_destination_is_refused(tmp_path):
    archive = make_tar(tmp_path / "slip.tar.gz", {"good.txt": b"g", "../../evil.txt": b"evil"})
    result, code = run(archive, tmp_path / "out")
    assert code == 3 and "'..'" in result["reason"] and not (tmp_path / "out").exists()


def test_links_and_devices_in_a_tar_are_skipped_never_created(tmp_path):
    archive = tmp_path / "links.tar"
    with tarfile.open(archive, "w") as tf:
        data = b"real"
        info = tarfile.TarInfo("d/real.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        for name, kind, linkname in (("d/sym", tarfile.SYMTYPE, "/etc/passwd"), ("d/hard", tarfile.LNKTYPE, "d/real.txt"),
                                     ("d/dev", tarfile.CHRTYPE, ""), ("d/fifo", tarfile.FIFOTYPE, "")):
            link = tarfile.TarInfo(name)
            link.type, link.linkname = kind, linkname
            tf.addfile(link)
        # a file "through" the symlink: with the link skipped this is an ordinary path, inside dest
        through = tarfile.TarInfo("d/sym2/x.txt")
        through.size = 1
        tf.addfile(through, io.BytesIO(b"x"))
    result, code = run(archive, tmp_path / "links")
    assert code == 0 and result["skipped_links"] == 4 and result["files"] == 2
    assert tree(tmp_path / "links") == {"real.txt": b"real", "sym2/x.txt": b"x"}
    assert not any(p.is_symlink() for p in (tmp_path / "links").rglob("*"))


def test_a_symlink_in_a_zip_is_skipped(tmp_path):
    archive = tmp_path / "s.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("d/real.txt", b"r")
        zf.writestr("d/other.txt", b"o")
        link = zipfile.ZipInfo("d/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "/etc/passwd")
    result, _ = run(archive, tmp_path / "s")
    assert result["skipped_links"] == 1 and set(tree(tmp_path / "s")) == {"real.txt", "other.txt"}


def test_more_files_than_the_limit_is_refused(tmp_path):
    archive = make_zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(30)})
    result, code = run(archive, tmp_path / "many", "--max-files", "10")
    assert code == 3 and "more than 10 files" in result["reason"]
    assert not (tmp_path / "many").exists() and not (tmp_path / "many.unpacking").exists()


def test_more_bytes_than_the_limit_is_refused_and_cleaned_up(tmp_path):
    archive = make_zip(tmp_path / "big.zip", {"a.bin": os.urandom(300_000), "b.bin": os.urandom(300_000)},
                       zipfile.ZIP_STORED)
    result, code = run(archive, tmp_path / "big", "--max-bytes", "400000")
    assert code == 3 and "unpacks to more than" in result["reason"]
    assert not (tmp_path / "big").exists() and not (tmp_path / "big.unpacking").exists()


def test_a_decompression_bomb_is_refused(tmp_path):
    """80 MB of zeros in a ~80 KB zip: the bytes are counted as written, so it stops early."""
    archive = make_zip(tmp_path / "bomb.zip", {"zeros.bin": b"\x00" * (80 * 1024 * 1024)})
    assert archive.stat().st_size < 200_000
    result, code = run(archive, tmp_path / "bomb", "--max-ratio", "100")
    assert code == 3 and "decompression bomb" in result["reason"]
    assert not (tmp_path / "bomb").exists() and not (tmp_path / "bomb.unpacking").exists()


def test_something_that_is_not_an_archive_is_refused(tmp_path):
    fake = tmp_path / "notes.zip"
    fake.write_bytes(b"just some text, named .zip")
    result, code = run(fake, tmp_path / "notes")
    assert code == 3 and "not a zip or tar archive" in result["reason"]


def test_a_truncated_archive_is_refused_not_half_unpacked(tmp_path):
    whole = make_tar(tmp_path / "whole.tar.gz", {f"f{i}.bin": os.urandom(50_000) for i in range(8)})
    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(whole.read_bytes()[:150_000])
    result, code = run(cut, tmp_path / "cut")
    assert code == 3 and "damaged" in result["reason"]
    assert not (tmp_path / "cut").exists() and not (tmp_path / "cut.unpacking").exists()


def test_an_archive_of_empty_folders_is_refused(tmp_path):
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("only/a/dir/", "")
    result, code = run(archive, tmp_path / "empty")
    assert code == 3 and "no files" in result["reason"]


# -- after an interruption ------------------------------------------------------------------------

def test_a_rerun_after_a_finished_unpack_reports_it_instead_of_unpacking_again(tmp_path):
    """The gateway died between the unpack and recording it: the retry must find a complete result."""
    archive = make_zip(tmp_path / "r.zip", {"a.txt": b"aaa", "b.txt": b"bb"})
    first, _ = run(archive, tmp_path / "r", "--keep-archive")
    (tmp_path / "r" / "a.txt").write_bytes(b"changed by the user since")
    again, code = run(archive, tmp_path / "r")
    assert code == 0 and again["resumed"] is True and again["files"] == 2
    assert (tmp_path / "r" / "a.txt").read_bytes() == b"changed by the user since"   # not unpacked over
    assert again["archive_removed"] is True and not archive.exists()                  # and the archive goes now
    assert first["files"] == 2


def test_what_an_interrupted_run_left_behind_is_replaced(tmp_path):
    archive = make_zip(tmp_path / "i.zip", {"a.txt": b"a", "b.txt": b"b"})
    stale = tmp_path / "i.unpacking"
    (stale / "half").mkdir(parents=True)
    (stale / "half" / "written.txt").write_bytes(b"partial")
    result, code = run(archive, tmp_path / "i")
    assert code == 0 and tree(tmp_path / "i") == {"a.txt": b"a", "b.txt": b"b"}
    assert not stale.exists()


def test_the_script_needs_nothing_but_the_standard_library():
    """It runs as a bare file on a compute node; an ``aiscientist`` import would break it there."""
    src = SCRIPT.read_text()
    imported = {line.split()[1].split(".")[0] for line in src.splitlines()
                if line.startswith(("import ", "from ")) and not line.startswith("from __future__")}
    assert imported <= {"argparse", "json", "os", "shutil", "stat", "sys", "tarfile", "zipfile"}
