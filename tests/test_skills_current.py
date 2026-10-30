"""skills/current: the skills release the server runs, switched by deploy/deploy_skills.sh.

Without the link the tag configs/skills_registry.json pins is read (a checkout, CI). With it, the
release it points at is read, the running library follows a switch at its next refresh without a
restart, and a link to something that is not an installed release gives no curated skills (fail
closed), with the reason where /health and the System page show it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiscientist.agents import skills as sk
from aiscientist.agents import skills_registry as registry


def _skill(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} does one thing.\n---\n\nBody.\n")
    return root


@pytest.fixture
def two_releases(tmp_path, skill_release, monkeypatch):
    """Releases v0.1.0 (skill alpha) and v0.2.0 (skill beta) installed, and a current link path."""
    skill_release(_skill(tmp_path / "src1", "alpha"), tag="v0.1.0", refresh=False)
    skill_release(_skill(tmp_path / "src2", "beta"), tag="v0.2.0", refresh=False)
    current = tmp_path / "skills-current"
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_CURRENT", str(current))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASE_TAG", "v0.1.0")
    return tmp_path / "skill-releases", current


def _point(link: Path, target: str) -> None:
    tmp = link.with_name(link.name + ".tmp")
    tmp.symlink_to(target)
    os.replace(tmp, link)


def test_without_the_link_the_pinned_tag_is_read(two_releases):
    releases, _ = two_releases
    cfg = registry.config()
    assert (cfg["release_tag"], cfg["pinned_tag"]) == ("v0.1.0", "v0.1.0")
    assert registry.release_dir(cfg) == releases / "0.1.0"
    sk.refresh_skills()
    assert "alpha" in sk.SKILLS and "beta" not in sk.SKILLS


def test_the_library_follows_a_switch_of_the_link_without_a_restart(two_releases):
    releases, current = two_releases
    _point(current, str(releases / "0.2.0"))
    cfg = registry.config()
    assert (cfg["release_tag"], cfg["pinned_tag"]) == ("v0.2.0", "v0.1.0")
    sk.refresh_skills()
    assert "beta" in sk.SKILLS and "alpha" not in sk.SKILLS
    assert sk.SKILLS["beta"].registry_release == "v0.2.0"
    assert registry.STATUS == {"tag": "v0.2.0", "skills": 1, "error": None}

    _point(current, str(releases / "0.1.0"))          # a rollback is the same switch
    assert sk.refresh_skills() is True
    assert "alpha" in sk.SKILLS and "beta" not in sk.SKILLS
    assert registry.STATUS["tag"] == "v0.1.0"


def test_a_link_to_no_installed_release_gives_no_curated_skills(two_releases, tmp_path):
    releases, current = two_releases
    _point(current, str(releases / "9.9.9"))           # dangling
    sk.refresh_skills()
    assert "alpha" not in sk.SKILLS and "beta" not in sk.SKILLS
    assert registry.STATUS["tag"] is None and "not an installed skills release" in registry.STATUS["error"]

    (tmp_path / "not-a-version").mkdir()
    _point(current, str(tmp_path / "not-a-version"))
    sk.refresh_skills()
    assert "not a release version" in registry.STATUS["error"]


def test_a_tampered_release_behind_the_link_is_refused(two_releases):
    releases, current = two_releases
    _point(current, str(releases / "0.2.0"))
    catalog = releases / "0.2.0" / "catalog.json"
    catalog.write_text(catalog.read_text().replace("beta does one thing", "beta does another thing"))
    sk.refresh_skills()
    assert "beta" not in sk.SKILLS
    assert "checksum mismatch" in registry.STATUS["error"]


def _check(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(Path(registry.__file__).resolve().parents[2])}
    return subprocess.run([sys.executable, "-m", "aiscientist.agents.skills_registry", *args],
                          capture_output=True, text=True, env=env)


def test_the_deploy_check_of_a_downloaded_release(two_releases):
    """deploy/deploy_skills.sh runs this before it switches the link."""
    releases, _ = two_releases
    r = _check("check", str(releases / "0.2.0"), "v0.2.0")
    assert r.returncode == 0, r.stderr
    assert "2 files match SHA256SUMS" in r.stdout

    r = _check("check", str(releases / "0.2.0"), "v0.1.0")    # the catalog of another release
    assert r.returncode == 1 and "does not match" in r.stderr

    (releases / "0.2.0" / "SHA256SUMS").write_text(
        (releases / "0.2.0" / "SHA256SUMS").read_text().replace("catalog.json", "other.json"))
    r = _check("check", str(releases / "0.2.0"), "v0.2.0")
    assert r.returncode == 1 and "refused" in r.stderr

    assert _check("check").returncode == 2


def test_health_reports_the_skills_release_the_library_loaded(two_releases, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw_app
    from aiscientist.gateway import release_info

    releases, current = two_releases
    client = TestClient(gw_app.app)
    _point(current, str(releases / "0.2.0"))
    monkeypatch.setattr(release_info, "_REFRESHED_AT", 0.0)
    assert client.get("/health").json()["skills"] == "v0.2.0"
    assert "beta" in sk.SKILLS

    _point(current, str(releases / "0.1.0"))
    monkeypatch.setattr(release_info, "_REFRESHED_AT", 0.0)
    assert client.get("/health").json()["skills"] == "v0.1.0"
    body = client.get("/health").text                   # within 2 s: no rescan, same answer
    assert json.loads(body)["skills"] == "v0.1.0"
