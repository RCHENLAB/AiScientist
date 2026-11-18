"""scripts/pr_review_gate.py: which files a pull request's policy check reads."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pr_review_gate.py"


@pytest.fixture()
def gate():
    spec = importlib.util.spec_from_file_location("pr_review_gate", _PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module            # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.org")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "old.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_only_the_files_the_pull_request_changes_are_read(gate, repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "new.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    assert gate.changed_files(base, _git(repo, "rev-parse", "HEAD")) == [Path("new.py")]


def test_a_pull_request_that_changes_no_file_has_nothing_to_check(gate, repo):
    # e.g. a branch that main was merged into after all of its work had reached main
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "nothing")
    assert gate.changed_files(base, _git(repo, "rev-parse", "HEAD")) == []


def test_commits_git_cannot_compare_fall_back_to_every_tracked_file(gate, repo):
    assert gate.changed_files("0" * 40, _git(repo, "rev-parse", "HEAD")) == [Path("old.py")]
