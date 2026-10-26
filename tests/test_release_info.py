from aiscientist.gateway import release_info


def test_read_version_file(tmp_path):
    f = tmp_path / "VERSION"
    f.write_text("tag=v0.4.0\nsha=abc1234\nbuilt_at=2026-10-25T00:00:00Z\n")
    assert release_info._read_version_file(f) == {
        "tag": "v0.4.0", "sha": "abc1234", "built_at": "2026-10-25T00:00:00Z"}


def test_missing_version_file_degrades(tmp_path):
    assert release_info._read_version_file(tmp_path / "nope") == {}


def test_skills_release_follows_pinned_registry(tmp_path, monkeypatch):
    cfg = tmp_path / "reg.json"
    cfg.write_text('{"release_tag": "v1.2.3", "allowed_tiers": ["core"], "releases_root": "%s"}' % tmp_path)
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_CONFIG", str(cfg))
    assert release_info.skills_release() == {"tag": "v1.2.3", "installed": False}
    (tmp_path / "1.2.3").mkdir()
    assert release_info.skills_release()["installed"] is True
