"""Shared test fixtures.

Curated skills come only from the pinned aiscientist-skills release (``configs/skills_registry.json``),
which is installed on the server and not in git. A test never reads an installed release:
``skill_release`` publishes one from skill folders the test writes, the way the registry's release
workflow does, and points the loader at it. ``tests/fixtures/skills/`` holds the skills several
test files share (the CellQC skill ``run_cellqc`` reads its environment and settings from).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

FIXTURE_SKILLS = Path(__file__).resolve().parent / "fixtures" / "skills"


def write_release(releases_root: Path, source: Path, *, tag: str = "v0.1.0",
                  status: str = "community") -> Path:
    """Publish every skill under ``source`` (a folder holding a ``SKILL.md``, at any depth) as
    release ``tag`` in ``releases_root/<version>/``, replacing what was there:
    ``aiscientist-skills-<version>.zip`` with each skill at ``skills/<category>/<name>/``,
    ``catalog.json`` built from the SKILL.md front matter, and ``SHA256SUMS`` over the two. A skill's
    tier is its ``metadata.status``, else ``status``. Returns the release folder."""
    from aiscientist.agents.skills import _parse_front_matter, _skill_md_files

    version = tag[1:]
    root = releases_root / version
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    rows, members = [], {}
    for skill_md in _skill_md_files(source):
        folder = skill_md.parent
        meta, body = _parse_front_matter(skill_md.read_text(encoding="utf-8"))
        md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        name = str(meta.get("name") or folder.name)
        parent = folder.parent.relative_to(source).as_posix()
        category = str(md.get("category") or ("general" if parent == "." else parent))
        path = f"skills/{category}/{name}"
        for file in sorted(folder.rglob("*")):
            if file.is_file():
                members[f"{path}/{file.relative_to(folder).as_posix()}"] = file.read_bytes()
        secrets = [s.strip() for s in str(md.get("requires-secrets") or "").split(",")]
        rows.append({
            "name": name, "category": category, "path": path,
            "description": str(meta.get("description") or body.strip().splitlines()[0]),
            "version": str(md.get("version") or "1.0.0"), "status": str(md.get("status") or status),
            "tags": [t.strip() for t in str(md.get("tags") or "").split(",") if t.strip()],
            "requirements": {"compute": str(md.get("compute") or ""),
                             "gpu": str(md.get("requires-gpu") or "false").lower() == "true",
                             "scheduler": str(md.get("scheduler") or "none"),
                             "network": str(md.get("requires-network") or "none"),
                             "secrets": [s for s in secrets if s and s != "none"],
                             "allowed_tools": str(meta.get("allowed-tools") or "")}})
    bundle = f"aiscientist-skills-{version}.zip"
    with zipfile.ZipFile(root / bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in sorted(members.items()):
            info = zipfile.ZipInfo(rel, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    (root / "catalog.json").write_text(json.dumps({
        "schema_version": 1, "registry": "aiscientist-skills", "registry_version": version,
        "skills": rows}, indent=2) + "\n", encoding="utf-8")
    (root / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((root / n).read_bytes()).hexdigest()}  {n}\n"
        for n in (bundle, "catalog.json")), encoding="utf-8")
    return root


@pytest.fixture
def skill_release(tmp_path, monkeypatch):
    """``skill_release(source, **kw)`` installs a test release of the skills under ``source`` (see
    :func:`write_release`), points the loader at it, and refreshes the library (``refresh=False``
    leaves that to the test). The releases root starts empty, so the library is empty until a test
    installs one. Everything is restored after the test."""
    from aiscientist.agents import skills as sk

    releases = tmp_path / "skill-releases"
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT", str(releases))
    # No skills/current link unless the test makes one: the pinned tag decides.
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_CURRENT", str(tmp_path / "skills-current"))
    monkeypatch.setenv("AISCIENTIST_SKILLS_REGISTRY_STATS", str(tmp_path / "skill_versions.sqlite3"))

    def install(source: Path, *, refresh: bool = True, **kw) -> Path:
        root = write_release(releases, source, **kw)
        if refresh:
            sk.refresh_skills()
        return root

    yield install
    monkeypatch.undo()
    sk.refresh_skills()


@pytest.fixture
def cellqc_release(skill_release):
    """A release holding the fixture CellQC skill, the one ``run_cellqc`` names in its TOOL.md."""
    return skill_release(FIXTURE_SKILLS)


@pytest.fixture(autouse=True)
def no_account_storage_limit(monkeypatch):
    """The per-account limit (issue 74) is off unless a test turns it on: it reads the account's
    datasets, which most tests save or upload without a database."""
    monkeypatch.setenv("AISCIENTIST_ACCOUNT_STORAGE_GB", "0")
