from aiscientist.gateway import release_info


def test_read_version_file(tmp_path):
    f = tmp_path / "VERSION"
    f.write_text("tag=v0.4.0\nsha=abc1234\nbuilt_at=2026-10-25T00:00:00Z\n")
    assert release_info._read_version_file(f) == {
        "tag": "v0.4.0", "sha": "abc1234", "built_at": "2026-10-25T00:00:00Z"}


def test_missing_version_file_degrades(tmp_path):
    assert release_info._read_version_file(tmp_path / "nope") == {}


def test_skills_release_reads_skills_dir(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("tag=skills-v1.2.0\nsha=def5678\n")
    monkeypatch.setenv("AISCIENTIST_SKILLS_DIR", str(tmp_path))
    assert release_info.skills_release()["tag"] == "skills-v1.2.0"
    assert release_info.release_info()["skills"]["sha"] == "def5678"
