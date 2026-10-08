"""Induced skills reach the models only after an admin approves them (Yijun, 2026-10-07).

An induced skill is one run's run_code frozen into a template, with that dataset's file format and
column names inside it. Listed for planning on every later run, `verify_matrix_provenance` (DDX41's
read_h5ad + nCount_RNA / sampleid / majorclass) was planned onto a 10x .h5 in run f3b8268c4fd4 and
failed three times. Now: pending until approved, invisible to the PI's list, the Scientist's
manifest, search and read; retired stays on disk and stays invisible; curated skills are untouched.
"""
from __future__ import annotations

import importlib
import json

import pytest

from aiscientist.agents import skills as skills_mod
from aiscientist.agents.research_harness import HarnessContext
from aiscientist.agents.skills import (
    Skill, make_search_skills_tool, make_skill_reference_tool, plan_skill_lines, refresh_skills,
    register_skill, review_detail, review_listing, set_review, skill_manifest,
)

_INDUCED_MD = """---
name: {name}
description: Determine whether an AnnData matrix holds raw counts.
induced: true
origin_step: **Matrix provenance & rules** — Use `run_code` to inspect the AnnData matrix
---

## When to use

Run this immediately after loading any single-cell RNA-seq .h5ad.
"""


@pytest.fixture
def libs(tmp_path, monkeypatch):
    """A curated root with one skill, an induced root with two, and module state of its own."""
    curated, induced = tmp_path / "skills", tmp_path / "induced"
    (curated / "score_signature").mkdir(parents=True)
    (curated / "score_signature" / "SKILL.md").write_text(
        "---\nname: score_signature\ndescription: curated scoring\n---\n\nbody\n")
    for name in ("verify_matrix_provenance", "qc_removal_reconciliation"):
        (induced / name).mkdir(parents=True)
        (induced / name / "SKILL.md").write_text(_INDUCED_MD.format(name=name))
        (induced / name / "reference.py").write_text("import scanpy as sc\nsc.read_h5ad(DATASET)\n")
    monkeypatch.setenv("AISCIENTIST_SKILLS_DIR", str(curated))
    monkeypatch.setenv("AISCIENTIST_INDUCED_SKILLS_DIR", str(induced))
    shown: dict = {}
    every: dict = {}
    monkeypatch.setattr(skills_mod, "SKILLS", shown)
    monkeypatch.setattr(skills_mod, "ALL_SKILLS", every)
    monkeypatch.setattr(skills_mod, "_SIGNATURE", ())
    monkeypatch.setattr(skills_mod, "_REGISTERED", {})
    refresh_skills()
    return induced, shown, every


def test_unreviewed_induced_skills_are_on_disk_but_invisible_to_the_models(libs):
    _induced, shown, every = libs
    assert {"score_signature", "verify_matrix_provenance", "qc_removal_reconciliation"} <= set(every)
    assert set(shown) == {"score_signature"}                     # curated only
    assert every["verify_matrix_provenance"].review == "pending"

    lines, _ = plan_skill_lines("matrix provenance h5ad", skills=shown)
    assert all("verify_matrix_provenance" not in ln for ln in lines)
    assert "verify_matrix_provenance" not in skill_manifest(shown)
    read = make_skill_reference_tool(lambda: skills_mod.SKILLS)
    assert "error" in read.executor({"name": "verify_matrix_provenance"}, HarnessContext(decisions={}))
    search = make_search_skills_tool(lambda: skills_mod.SKILLS)
    hits = search.executor({"query": "matrix provenance raw counts"}, HarnessContext(decisions={}))
    assert all(h["name"] != "verify_matrix_provenance" for h in hits["results"])


def test_approve_retire_and_reset_apply_without_a_restart(libs):
    induced, shown, every = libs
    rec = set_review("verify_matrix_provenance", "approved", by="BioAdmin", note="h5ad only")
    assert rec["status"] == "approved" and rec["by"] == "BioAdmin"
    assert "verify_matrix_provenance" in shown                   # the models can use it now

    set_review("verify_matrix_provenance", "retired", by="BioAdmin")
    assert "verify_matrix_provenance" not in shown
    assert every["verify_matrix_provenance"].review == "retired"  # kept on disk, never offered

    set_review("verify_matrix_provenance", "pending", by="Yijun")
    stored = json.loads((induced / "_review.json").read_text())["verify_matrix_provenance"]
    assert stored["status"] == "pending" and stored["by"] == "Yijun"
    assert [h["status"] for h in stored["history"]] == ["approved", "retired"]   # audit trail


def test_set_review_refuses_curated_unknown_and_bad_status(libs):
    with pytest.raises(KeyError):
        set_review("score_signature", "retired")                # curated: not reviewable
    with pytest.raises(KeyError):
        set_review("no_such_skill", "approved")
    with pytest.raises(ValueError):
        set_review("qc_removal_reconciliation", "maybe")


def test_a_folder_dropped_in_the_induced_root_cannot_skip_review(libs):
    induced, shown, every = libs
    (induced / "sneaky").mkdir()
    (induced / "sneaky" / "SKILL.md").write_text(
        "---\nname: sneaky\ndescription: no induced flag\n---\n\nbody\n")
    refresh_skills()
    assert every["sneaky"].induced and every["sneaky"].review == "pending"
    assert "sneaky" not in shown


def test_a_newly_induced_skill_waits_for_review(libs):
    _induced, shown, every = libs
    assert register_skill(Skill("fresh_one", summary="learned", files={"reference.py": "x"}, induced=True))
    assert every["fresh_one"].review == "pending" and "fresh_one" not in shown
    # its name is taken all the same: induction must not learn it twice
    assert register_skill(Skill("fresh_one", summary="again", induced=True)) is False


def test_review_listing_and_detail_give_the_reviewer_provenance_and_code(libs):
    rows = {r["name"]: r for r in review_listing()}
    assert set(rows) == {"verify_matrix_provenance", "qc_removal_reconciliation"}
    assert rows["verify_matrix_provenance"]["status"] == "pending"
    assert rows["verify_matrix_provenance"]["origin_step"].startswith("**Matrix provenance")
    detail = review_detail("verify_matrix_provenance")
    assert "read_h5ad" in detail["files"]["reference.py"] and "induced: true" in detail["skill_md"]
    assert review_detail("score_signature") is None


# --- the admin API -------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch, libs):
    # the auth extra; the offline CI subset lacks it, and must still run the gate tests above
    for mod in ("bcrypt", "itsdangerous", "sqlalchemy", "httpx"):
        pytest.importorskip(mod)
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")
    from aiscientist.gateway import auth, auth_routes, db, models  # noqa: F401
    for mod in (db, models, auth, auth_routes):
        importlib.reload(mod)
    db.reset(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    db.init_db()
    auth_routes.ensure_bootstrap_admin()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)
    return TestClient(app)


def test_only_an_admin_can_list_or_review_skills(client):
    assert client.get("/api/admin/skills").status_code == 401
    assert client.post("/api/auth/login", json={"username": "root", "password": "rootpass1"}).status_code == 200
    client.post("/api/admin/users", json={"username": "bob", "password": "bobpass1", "role": "user"})
    listing = client.get("/api/admin/skills").json()
    assert listing["counts"]["pending"] == 2 and listing["curated"] == 1
    assert listing["induced_dir_configured"] is True

    r = client.post("/api/admin/skills/qc_removal_reconciliation/review",
                    json={"status": "approved", "note": "CellQC metrics only"})
    assert r.status_code == 200 and r.json()["review"]["by"] == "root"
    assert "qc_removal_reconciliation" in skills_mod.SKILLS
    assert client.post("/api/admin/skills/score_signature/review",
                       json={"status": "retired"}).status_code == 404
    assert client.post("/api/admin/skills/qc_removal_reconciliation/review",
                       json={"status": "maybe"}).status_code == 400
    assert "read_h5ad" in client.get("/api/admin/skills/verify_matrix_provenance").json()["files"]["reference.py"]

    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "bob", "password": "bobpass1"})
    assert client.get("/api/admin/skills").status_code == 403
    assert client.post("/api/admin/skills/verify_matrix_provenance/review",
                       json={"status": "approved"}).status_code == 403
