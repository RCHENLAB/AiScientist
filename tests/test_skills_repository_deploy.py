"""Deployment source pin checks run on Bash without GNU-only switching or network."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy_skills.sh"


def environment(tmp_path):
    root = tmp_path / "site"
    release = root / "skills" / "releases" / "0.2.2"
    release.mkdir(parents=True)
    env = {**os.environ, "AISCIENTIST_ROOT": str(root), "AISCIENTIST_SKILLS_PYTHON": sys.executable,
           "AISCIENTIST_HOST": "127.0.0.1", "AISCIENTIST_SKILLS_REPO": "liujs123456/aiscientist-skills"}
    env.pop("AISCIENTIST_SKILLS_REGISTRY_CONFIG", None)
    env.pop("AISCIENTIST_DEPLOY_ENV", None)
    return root, release, env


@pytest.mark.parametrize("metadata", [None, {"tag": "v0.2.2", "repository": "other/skills"},
                                      {"tag": "v0.2.1", "repository": "liujs123456/aiscientist-skills"}])
def test_cached_wrong_or_unknown_origin_refuses_before_switch(tmp_path, metadata):
    root, release, env = environment(tmp_path)
    if metadata is not None:
        release.with_name(release.name + ".install.json").write_text(json.dumps(metadata))
    result = subprocess.run(["bash", str(SCRIPT), "v0.2.2"], env=env, capture_output=True, text=True)
    assert result.returncode == 1 and "origin is missing or mismatched" in result.stderr
    assert not (root / "skills" / "current").exists()


def test_config_repository_fallback_and_environment_precedence(tmp_path):
    root, _, env = environment(tmp_path)
    config = root / "app" / "current" / "configs" / "skills_registry.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"repository": "example/configured-skills"}))
    env.pop("AISCIENTIST_SKILLS_REPO")
    command = 'source "$1" --list; printf "%s\\n" "$SKILLS_REPO"'
    result = subprocess.run(["bash", "-c", command, "check", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.rstrip().endswith("example/configured-skills")
    env["AISCIENTIST_SKILLS_REPO"] = "liujs123456/aiscientist-skills"
    result = subprocess.run(["bash", "-c", command, "check", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.rstrip().endswith("liujs123456/aiscientist-skills")


def test_matching_cached_repository_is_accepted_by_source_check(tmp_path):
    _, release, env = environment(tmp_path)
    release.with_name(release.name + ".install.json").write_text(json.dumps(
        {"tag": "v0.2.2", "repository": "LIUJS123456/AiScientist-Skills"}))
    command = 'source "$1" --list; check_cached_repository "$2"'
    result = subprocess.run(["bash", "-c", command, "check", str(SCRIPT), str(release)],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_rollback_refuses_another_repository_without_changing_current(tmp_path):
    root, release, env = environment(tmp_path)
    current = release.parent / "0.2.3"
    current.mkdir()
    release.with_name(release.name + ".install.json").write_text(json.dumps(
        {"tag": "v0.2.2", "repository": "other/skills"}))
    (root / "skills" / "current").symlink_to("releases/0.2.3")
    (root / "skills" / "previous").symlink_to("releases/0.2.2")
    result = subprocess.run(["bash", str(SCRIPT), "--rollback"], env=env, capture_output=True, text=True)
    assert result.returncode == 1 and "origin is missing or mismatched" in result.stderr
    assert (root / "skills" / "current").resolve() == current


@pytest.mark.parametrize("repository", ["../skills", "owner/repo;echo", "https://github.com/owner/repo", "owner/.."])
def test_invalid_repository_is_refused_before_network(tmp_path, repository):
    _, _, env = environment(tmp_path)
    env["AISCIENTIST_SKILLS_REPO"] = repository
    result = subprocess.run(["bash", str(SCRIPT), "--list"], env=env, capture_output=True, text=True)
    assert result.returncode == 1 and "owner/repository" in result.stderr
