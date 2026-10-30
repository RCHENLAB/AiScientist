from aiscientist.gateway import release_info


def test_read_version_file(tmp_path):
    f = tmp_path / "VERSION"
    f.write_text("tag=v0.4.0\nsha=abc1234\nbuilt_at=2026-10-25T00:00:00Z\n")
    assert release_info._read_version_file(f) == {
        "tag": "v0.4.0", "sha": "abc1234", "built_at": "2026-10-25T00:00:00Z"}


def test_missing_version_file_degrades(tmp_path):
    assert release_info._read_version_file(tmp_path / "nope") == {}


def test_skills_release_follows_pinned_registry(tmp_path, monkeypatch):
    from aiscientist.agents import skills_registry

    cfg = tmp_path / "reg.json"
    cfg.write_text('{"release_tag": "v1.2.3", "allowed_tiers": ["core"], "releases_root": "%s"}' % tmp_path)
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_CONFIG", str(cfg))
    monkeypatch.setattr(skills_registry, "STATUS", {"tag": None, "skills": 0, "error": None})
    assert release_info.skills_release() == {"tag": "v1.2.3", "installed": False}
    (tmp_path / "1.2.3").mkdir()
    assert release_info.skills_release()["installed"] is True


def test_skills_release_follows_the_current_link(tmp_path, monkeypatch):
    """skills/current decides once it exists; the app's own pin, the install record's commit and
    what the library loaded are shown beside it."""
    from aiscientist.agents import skills_registry

    releases = tmp_path / "skills" / "releases"
    (releases / "1.3.0").mkdir(parents=True)
    (releases / "1.3.0.install.json").write_text('{"tag": "v1.3.0", "commit": "%s"}' % ("d" * 40))
    (tmp_path / "skills" / "current").symlink_to("releases/1.3.0")
    cfg = tmp_path / "reg.json"
    cfg.write_text('{"release_tag": "v1.2.3", "allowed_tiers": ["core"], "releases_root": "%s", '
                   '"current": "%s"}' % (releases, tmp_path / "skills" / "current"))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_CONFIG", str(cfg))
    monkeypatch.setattr(skills_registry, "STATUS", {"tag": "v1.3.0", "skills": 4, "error": None})
    assert release_info.skills_release() == {"tag": "v1.3.0", "installed": True, "pinned": "v1.2.3",
                                             "sha": "d" * 40, "loaded": "v1.3.0", "skills": 4}


def test_app_release_reads_the_package_version(tmp_path, monkeypatch):
    from aiscientist.agents import provenance

    (tmp_path / "VERSION").write_text("tag=v0.4.0\nsha=" + "b" * 40 + "\nbuilt_at=2026-10-25T00:00:00Z\n")
    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    assert release_info.app_release() == {"tag": "v0.4.0", "sha": "b" * 40, "built_at": "2026-10-25T00:00:00Z"}
    assert release_info.app_tag() == "v0.4.0"


def test_an_rsync_deploy_is_untagged_with_its_commit(tmp_path, monkeypatch):
    """No VERSION file (rsync deploy or checkout): no tag, and the commit provenance knows."""
    from aiscientist.agents import provenance

    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(provenance, "git_sha", lambda: "c" * 40)
    assert release_info.app_release() == {"tag": None, "sha": "c" * 40, "built_at": None}
    assert release_info.app_tag() is None


def test_health_names_the_release_and_whether_accounts_are_on(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from aiscientist.agents import provenance, skills_registry
    from aiscientist.gateway import app as gw_app

    (tmp_path / "VERSION").write_text("tag=v0.4.0\nsha=abc\n")
    monkeypatch.setattr(provenance, "_REPO_ROOT", tmp_path)
    client = TestClient(gw_app.app)
    for accounts in (True, False):
        monkeypatch.setattr(gw_app, "_AUTH_ENABLED", accounts)
        r = client.get("/health")              # no login: the deploy script and systemd call it
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": "v0.4.0", "accounts": accounts,
                            "skills": skills_registry.STATUS["tag"]}
        # The deploy script greps the compact JSON, so the encoding is part of the contract.
        assert '"version":"v0.4.0"' in r.text and f'"accounts":{str(accounts).lower()}' in r.text
