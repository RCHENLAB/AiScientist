"""A tool's declared dependency, installed for one run (tools/run_deps.py).

pip is faked (it writes a ``--target`` tree); the import check runs for real, in a fresh
interpreter, through the same sitecustomize the tool job uses.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aiscientist.tools import run_deps

_real_run = subprocess.run


def _fake_pip(monkeypatch, write_tree, *, returncode=0, stderr=""):
    """Replace only the pip call; every other subprocess (the import check) runs for real."""
    calls: list[list[str]] = []

    def run(cmd, *a, **kw):
        if cmd[1:4] == ["-m", "pip", "install"]:
            calls.append(cmd)
            if returncode == 0:
                write_tree(Path(cmd[cmd.index("--target") + 1]))
            return subprocess.CompletedProcess(cmd, returncode, "", stderr)
        return _real_run(cmd, *a, **kw)

    monkeypatch.setattr(run_deps.subprocess, "run", run)
    return calls


@pytest.fixture
def fake_dep(monkeypatch):
    monkeypatch.setitem(run_deps.ALLOWED, "fakedep", ("fakedep==1.0", "aisci_fakedep_mod"))
    return "fakedep"


def _tree_with_image_collisions(tree: Path) -> None:
    (tree / "aisci_fakedep_mod").mkdir(parents=True)
    (tree / "aisci_fakedep_mod" / "__init__.py").write_text("VALUE = 42\n")
    (tree / "numpy").mkdir()                                   # the image has numpy
    (tree / "numpy" / "__init__.py").write_text("raise ImportError('shadowed!')\n")
    (tree / "json.py").write_text("raise ImportError('shadowed!')\n")   # stdlib
    (tree / "numpy.libs").mkdir()                              # vendored libs: not a module
    (tree / "numpy.libs" / "libopenblas.so").write_text("")
    (tree / "aisci_fakedep_mod.libs").mkdir()                  # the kept package's own libs
    (tree / "aisci_fakedep_mod.libs" / "libfake.so").write_text("")
    (tree / "bin").mkdir()
    (tree / "bin" / "f2py").write_text("#!/bin/sh\n")
    for dist, top, libs in (("fakedep-1.0.dist-info", "aisci_fakedep_mod", "aisci_fakedep_mod.libs/libfake.so"),
                            ("numpy-9.9.dist-info", "numpy", "numpy.libs/libopenblas.so")):
        (tree / dist).mkdir()
        (tree / dist / "top_level.txt").write_text(top + "\n")
        (tree / dist / "RECORD").write_text(f"{top}/__init__.py,,\n{libs},,\n")


def test_an_undeclared_dependency_is_refused_without_running_pip(tmp_path, monkeypatch):
    calls = _fake_pip(monkeypatch, lambda tree: None)
    out = run_deps.install(tmp_path, "requests")
    assert out["status"] == "refused"
    assert "not a dependency any tool declares" in out["error"]
    assert calls == [] and not (tmp_path / "_deps").exists()


def test_a_declared_dependency_installs_without_shadowing_the_image(tmp_path, monkeypatch, fake_dep):
    calls = _fake_pip(monkeypatch, _tree_with_image_collisions)

    out = run_deps.install(tmp_path, fake_dep)

    assert out["status"] == "ok", out
    pkg = Path(out["path"])
    assert pkg == tmp_path / "_deps" / "pkgs" / "fakedep-1.0"
    # What the image already provides is gone, so it can never outrank the image's copy.
    assert {"numpy", "json"} <= set(out["pruned_image_modules"])
    assert not (pkg / "numpy").exists() and not (pkg / "json.py").exists()
    assert not (pkg / "numpy-9.9.dist-info").exists() and not (pkg / "bin").exists()
    assert not (pkg / "numpy.libs").exists()             # the pruned package's vendored libs go too
    assert (pkg / "aisci_fakedep_mod").is_dir() and (pkg / "fakedep-1.0.dist-info").is_dir()
    assert (pkg / "aisci_fakedep_mod.libs").is_dir()     # the kept package's own libs stay
    # Wheels only, into the run's own directory, with pip's scratch off the 64 MB container /tmp.
    cmd = calls[0]
    assert "--only-binary=:all:" in cmd and cmd[-1] == "fakedep==1.0"
    assert not list((tmp_path / "_deps").glob(".staging-*"))
    assert (Path(out["site"]) / "sitecustomize.py").is_file()
    assert "run only" in out["scope"]


def test_the_install_is_appended_after_the_image(tmp_path, monkeypatch, fake_dep):
    """A fresh interpreter set up like the tool job imports the package from the run's install,
    while stdlib and image modules still resolve to their own copies."""
    _fake_pip(monkeypatch, _tree_with_image_collisions)
    out = run_deps.install(tmp_path, fake_dep)
    env = run_deps.job_env([out["path"]], out["site"])
    code = ("import sys, json, aisci_fakedep_mod as m; "
            "print(m.VALUE, m.__file__.startswith(sys.argv[1]), "
            "sys.path.index(sys.argv[1]) > max(i for i, p in enumerate(sys.path) if 'site-packages' in p))")
    proc = _real_run([sys.executable, "-c", code, out["path"]], env=env, capture_output=True,
                     text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["42", "True", "True"]


def test_a_failed_pip_is_reported_and_leaves_nothing(tmp_path, monkeypatch, fake_dep):
    _fake_pip(monkeypatch, lambda tree: None, returncode=1,
              stderr="ERROR: No matching distribution found for fakedep==1.0")
    out = run_deps.install(tmp_path, fake_dep)
    assert out["status"] == "error"
    assert "No matching distribution" in out["error"]
    assert not (tmp_path / "_deps" / "pkgs" / "fakedep-1.0").exists()
    assert not list((tmp_path / "_deps").glob(".staging-*"))


def test_an_install_that_does_not_import_is_removed(tmp_path, monkeypatch, fake_dep):
    def broken(tree: Path) -> None:
        (tree / "aisci_fakedep_mod").mkdir(parents=True)
        (tree / "aisci_fakedep_mod" / "__init__.py").write_text("raise RuntimeError('needs a newer numpy')\n")

    _fake_pip(monkeypatch, broken)
    out = run_deps.install(tmp_path, fake_dep)
    assert out["status"] == "error"
    assert "needs a newer numpy" in out["error"]
    assert not (tmp_path / "_deps" / "pkgs" / "fakedep-1.0").exists()


def test_a_second_request_in_the_same_run_reuses_the_install(tmp_path, monkeypatch, fake_dep):
    calls = _fake_pip(monkeypatch, _tree_with_image_collisions)
    assert run_deps.install(tmp_path, fake_dep)["status"] == "ok"
    again = run_deps.install(tmp_path, fake_dep)
    assert again["status"] == "already_available"
    assert len(calls) == 1


def test_both_scanpy_spellings_map_to_one_pinned_package():
    assert run_deps.resolve("scikit-image") == run_deps.resolve("skimage")
    requirement, module = run_deps.resolve("skimage")
    assert requirement.startswith("scikit-image==") and module == "skimage"
