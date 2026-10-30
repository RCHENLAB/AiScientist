"""requirements.lock pins every package the Eye Server installs, and the release package carries it.

deploy/deploy_release.sh installs a release with `pip install --require-hashes -r requirements.lock`,
so a requirement pyproject.toml adds without regenerating the lock (scripts/lock_requirements.sh)
would make that install fail on the server. These tests fail first.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

packaging = pytest.importorskip("packaging")
from packaging.markers import default_environment  # noqa: E402
from packaging.requirements import Requirement  # noqa: E402
from packaging.utils import canonicalize_name  # noqa: E402

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = pytest.importorskip("tomli")

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "requirements.lock"
SERVER_EXTRAS = ("gateway", "auth", "anthropic", "langgraph", "analysis")


def _lock() -> tuple[str, dict[str, str], dict[str, int]]:
    text = LOCK.read_text(encoding="utf-8")
    python = re.search(r"^# python: (\d+\.\d+)", text, re.M).group(1)
    pins, hashes, name = {}, {}, None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if m:
            name = canonicalize_name(m.group(1))
            pins[name] = m.group(2)
            hashes[name] = 0
        elif name and "--hash=sha256:" in line:
            hashes[name] += 1
    return python, pins, hashes


def test_the_lock_names_the_python_it_was_made_for_and_hashes_every_package():
    python, pins, hashes = _lock()
    assert re.fullmatch(r"3\.\d+", python)
    assert len(pins) > 50
    assert [n for n, k in hashes.items() if k == 0] == []
    assert {"pip", "setuptools"} <= set(pins)        # what builds the app is pinned too


def test_every_requirement_of_the_server_extras_is_pinned_at_a_version_it_accepts():
    python, pins, _ = _lock()
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wanted = list(project["dependencies"])
    for extra in SERVER_EXTRAS:
        wanted += project["optional-dependencies"][extra]
    env = {**default_environment(), "python_version": python, "python_full_version": python + ".0",
           "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
           "platform_machine": "x86_64", "implementation_name": "cpython"}
    problems = []
    for spec in wanted:
        req = Requirement(spec)
        if req.marker is not None and not req.marker.evaluate(env):
            continue
        name = canonicalize_name(req.name)
        if name not in pins:
            problems.append(f"{spec}: not in requirements.lock")
        elif not req.specifier.contains(pins[name], prereleases=True):
            problems.append(f"{spec}: the lock has {pins[name]}")
    assert not problems, ("pyproject.toml and requirements.lock disagree; run scripts/lock_requirements.sh:\n"
                          + "\n".join(problems))


@pytest.mark.skipif(not (shutil.which("git") and shutil.which("sha256sum")),
                    reason="deploy/build_package.sh needs git and sha256sum")
def test_the_package_holds_the_commit_s_runtime_files_only(tmp_path):
    """build_package.sh packs the commit, not the working tree, and refuses one without a lock."""
    repo = tmp_path / "repo"
    for rel in ("src/aiscientist/__init__.py", "frontend/index.html", "preset_pipelines/p.yaml",
                "configs/skills_registry.json", "deploy/deploy_release.sh", "pyproject.toml", "LICENSE",
                "README.md", "requirements.txt", "requirements-gateway.txt", "start.sh",
                "scripts/update_corpus_index.py", "scripts/build_mechanism_cache.py",
                "tests/test_x.py", "docs/x.md", "handoff/h.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(rel)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "no lock"], check=True)
    script = REPO / "deploy" / "build_package.sh"
    run = lambda *a: subprocess.run(["bash", str(script), *a], cwd=repo, capture_output=True, text=True)  # noqa: E731

    r = run("v1.0.0", "HEAD", str(tmp_path / "out"))
    assert r.returncode == 1 and "has no requirements.lock" in r.stderr

    (repo / "requirements.lock").write_text("# python: 3.11\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "lock"], check=True)
    (repo / "src" / "aiscientist" / "untracked.py").write_text("not committed")
    (repo / "README.md").write_text("edited, not committed")
    r = run("v1.0.0", "HEAD", str(tmp_path / "out"))
    assert r.returncode == 0, r.stderr

    with tarfile.open(tmp_path / "out" / "aiscientist-v1.0.0.tar.gz") as tf:
        names = set(tf.getnames())
        assert tf.extractfile("aiscientist-v1.0.0/README.md").read() == b"README.md"
        version = tf.extractfile("aiscientist-v1.0.0/VERSION").read().decode()
    sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert f"tag=v1.0.0\nsha={sha}\n" in version
    assert "aiscientist-v1.0.0/requirements.lock" in names
    # The operator scripts that run on the server, from the release (scripts/README.md).
    assert {"aiscientist-v1.0.0/scripts/update_corpus_index.py",
            "aiscientist-v1.0.0/scripts/build_mechanism_cache.py"} <= names
    assert "aiscientist-v1.0.0/src/aiscientist/untracked.py" not in names
    assert not [n for n in names if "/._" in n]                 # no AppleDouble files from a Mac build
    with tarfile.open(tmp_path / "out" / "aiscientist-v1.0.0.tar.gz") as tf:
        assert not [m.name for m in tf.getmembers() if any("xattr" in k for k in m.pax_headers)]
    assert not any(n.startswith(("aiscientist-v1.0.0/tests", "aiscientist-v1.0.0/docs",
                                 "aiscientist-v1.0.0/handoff")) for n in names)
    sums = (tmp_path / "out" / "SHA256SUMS").read_text()
    assert sums.endswith("  aiscientist-v1.0.0.tar.gz\n")
    assert subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=tmp_path / "out",
                          capture_output=True, env={**os.environ}).returncode == 0
