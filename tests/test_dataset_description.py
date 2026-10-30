"""DATASET.md: the PI describes a dataset the first time a run reads it (RCHENLAB/AiScientist issue 10,
part 3).

The description has eight sections (a Scientific Data descriptor with MINSEQE and CELLxGENE fields).
Every number in it is read from the run-start profile or the registry record; the PI's own reading is
marked as inferred and a statement of it with a number the profile does not hold is left out; a field
the data does not establish says so. It is kept with the dataset beside its croissant.json, from one
record, shown below the preview, editable by the owner (an edit is never overwritten), and read by
later runs instead of being written again. Scripted models and an SQLite account database.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aiscientist.metadata import description as dmd

PROFILE = {
    "dataset_kind": "h5ad_single_cell", "format": "h5ad", "cells": 21374, "genes": 32285,
    "obs_keys": ["sampleid", "majorclass", "orig.ident", "nCount_RNA", "percent.mt"],
    "obs_categoricals": {
        "sampleid": {"n": 2, "values": ["DDX41", "WT"]},
        "majorclass": {"n": 4, "values": ["Rod", "Cone", "MG", "Microglia"]},
        "orig.ident": {"n": 1, "values": ["retina"]},
    },
    "design_by_arm": {"condition_column": "sampleid", "cells_scanned": 21374, "cells_total": 21374,
                      "cells_by_arm": {"DDX41": 9876, "WT": 11498},
                      "qc_median_by_arm": {"nCount_RNA": {"DDX41": 3078.0, "WT": 1916.0}},
                      "depth_imbalance": "median nCount_RNA differs 1.6x between arms"},
    "gene_symbols": {"identifiers": "symbols", "symbol_case": "Title-case (e.g. Gfap)",
                     "species_hint": "mouse", "evidence": "Malat1, Actb present"},
    "tissue_hint": "the most abundant genes point to retina (Rho, Sag)",
    "x_encoding": "csr_matrix", "density": 0.0812, "sampled_cells": 500,
    "mean_total_counts_sample": 2512.3, "mean_genes_detected_sample": 1210.0,
    "dataset_path": "/data/aiscientist/state/uploads/alice/Ddx41_DEG.h5ad",
}
FACTS = {"name": "Ddx41_DEG.h5ad", "kind": "file", "size_bytes": 512_000_000,
         "files": [{"path": "Ddx41_DEG.h5ad", "size": 512_000_000, "sha256": "ab" * 32}]}
INFERRED = {
    "summary": "Single-nucleus RNA-seq of mouse retina comparing DDX41 knockout with wild type across "
               "4 major classes.",
    "design": "sampleid holds the two arms, one library each, so there are no biological replicates.",
    "processing": "The mean total of 2512.3 per cell is not integer-like, so X looks normalised.",
    "technology": "likely 10x Chromium 3-prime single-nucleus RNA-seq",
    "caveats": ["Depth differs 1.6x between the arms.", "Only 6 donors were sequenced."],
    "supports": ["Marker analysis per major class."],
    "does_not_support": ["A pseudobulk test: one library per arm."],
}


def _desc(**kw):
    args = dict(name="Ddx41_DEG.h5ad", profile=dmd.profile_snapshot(PROFILE), facts=FACTS,
                inferred=INFERRED, model="DSV4", written_at="2026-10-29T12:00:00+00:00", run_id="abc123")
    args.update(kw)
    return dmd.build(**args)


# --- what the description says -----------------------------------------------------------------------

def test_it_has_the_eight_sections_and_marks_every_statement():
    md = dmd.render(_desc())
    titles = [line[3:] for line in md.splitlines() if line.startswith("## ")]
    assert titles == [f"{i}. {t}" for i, t in enumerate(dmd.SECTIONS, 1)]
    body = md.split("## 8.")[0]
    for line in body.splitlines():
        if line.startswith("- "):
            assert line.endswith(("_(read from the data)_", "_(inferred)_", "_(entered by the owner)_")) \
                or line.rstrip().endswith("_"), line           # or an italic "not recorded"
    assert "21,374 cells × 32,285 genes" in md and "_(read from the data)_" in md
    assert "Single-nucleus RNA-seq of mouse retina" in md and "_(inferred)_" in md
    assert "Written by the PI (DSV4) on 2026-10-29, in run abc123" in md


def test_every_number_is_read_from_the_data_and_an_inferred_one_that_is_not_is_left_out():
    desc = _desc()
    md = dmd.render(desc)
    # "6 donors" is in no field of the profile: that caveat is gone, and the last section says so
    assert "6 donors" not in md and desc["dropped"] == [{"field": "Caveat", "numbers": ["6"]}]
    assert "Left out: 1 inferred statement(s) that gave numbers the profile does not contain" in md
    # the numbers the PI copied from the profile stay (2512.3, 1.6x, 4 major classes)
    assert "2512.3 per cell" in md and "Depth differs 1.6x" in md and "4 major classes" in md
    allowed = dmd.allowed_numbers(dmd.profile_snapshot(PROFILE), FACTS)
    for sec in desc["sections"]:
        for st in sec["statements"]:
            if st["source"] == dmd.INFERRED:
                assert dmd.untraced_numbers(st["text"], allowed) == [], st
    # no server path from the profile reaches the description
    assert "/data/aiscientist" not in json.dumps(desc) and "the gene identifiers" in desc["evidence"]


def test_names_with_digits_are_not_numbers_but_ratios_are():
    assert dmd.numbers_in("10x Genomics 3-prime v3 on GRCm39 with Smart-seq2, CD4 and 3′ kits") == []
    assert dmd.numbers_in("3,078 vs 1,916; 1.6x deeper; -1.25 log2FC; 12.50%") == [
        "3078", "1916", "1.6", "1.25", "12.5"]


def test_a_field_the_data_does_not_establish_says_so_and_nothing_is_guessed():
    md = dmd.render(_desc(inferred={}))
    for field in ("Developmental stage", "Sex", "Reference"):
        assert f"- **{field}:** _not recorded_" in md
    assert "accession" not in md.lower()


def test_organism_tissue_and_cell_types_carry_their_ontology_terms():
    md = dmd.render(_desc())
    assert "mouse (Mus musculus, NCBITaxon:10090) — from the gene identifiers" in md
    assert "retina (UBERON:0000966)" in md
    assert "Rod (CL:0000604)" in md and "Microglia (CL:0000129)" in md and "MG," in md
    assert "`orig.ident` holds a single value, so it separates no samples or groups" in md
    assert "Cells per level of `sampleid`: DDX41 9,876, WT 11,498." in md


def test_the_owners_fields_take_the_place_of_the_drafted_ones():
    uf = {"organism": [{"id": "NCBITaxon:10116", "label": "Rattus norvegicus"}],
          "description": "Retinas from P14 rats."}
    md = dmd.render(_desc(user_fields=uf))
    assert "- **Organism:** Rattus norvegicus (NCBITaxon:10116) _(entered by the owner)_" in md
    assert "Retinas from P14 rats. _(entered by the owner)_" in md
    assert "Mus musculus" not in md


def test_the_reply_is_read_tolerantly():
    raw = "Here it is:\n```json\n" + json.dumps({"summary": "  A  study. ", "caveats": "one caveat",
                                                 "sex": 3, "supports": []}) + "\n```"
    assert dmd.parse_inferred(raw) == {"summary": "A study.", "caveats": ["one caveat"]}
    assert dmd.parse_inferred("not json") == {}


def test_the_summary_is_what_croissant_says_and_the_owners_edit_reads_back():
    desc = _desc()
    assert dmd.summary_text(desc).startswith("Ddx41_DEG.h5ad: 21,374 cells × 32,285 genes")
    edited = dmd.render(desc).replace("Single-nucleus RNA-seq", "Single-nucleus RNA-seq (edited)")
    assert "(edited)" in dmd.owner_summary(edited) and "_(" not in dmd.owner_summary(edited)


# --- one source with croissant.json --------------------------------------------------------------------

def test_croissant_takes_the_organism_and_the_summary_from_the_same_record():
    from aiscientist import metadata as md
    rec = {"profile": dmd.profile_snapshot(PROFILE), "inferred": INFERRED, "model": "DSV4",
           "written_at": "2026-10-29T12:00:00+00:00", "run_id": "abc123"}
    facts = {**FACTS, "uploaded_at": "2026-10-20T00:00:00+00:00", "owner": "alice", "source": "upload"}
    plain = md.build_dataset("11111111-1111-1111-1111-111111111111", facts, {})
    built = md.build_dataset("11111111-1111-1111-1111-111111111111", facts, {}, description=rec)
    doc = built.document
    assert doc["description"] == dmd.summary_text(dmd.from_record(rec, facts, {}, name="Ddx41_DEG.h5ad"))
    assert doc["description"] != plain.document["description"]
    about = {a.get("@id") or a.get("sameAs") or a.get("name") for a in doc.get("about") or []}
    assert any("10090" in str(x) for x in about) and any("0000966" in str(x) for x in about)
    prov = {e["path"]: e for e in built.provenance}
    assert prov["description"]["source"] == "pi-description"
    assert md.own_checks(doc)[0] == []
    # the owner's edit of DATASET.md becomes the croissant description, as the owner's
    owner = {**rec, "owner": {"markdown": dmd.render(dmd.from_record(rec, facts, {})).replace(
        "Single-nucleus", "Owner-checked single-nucleus")}}
    built = md.build_dataset("11111111-1111-1111-1111-111111111111", facts, {}, description=owner)
    assert "Owner-checked single-nucleus" in built.document["description"]
    assert {e["path"]: e for e in built.provenance}["description"]["source"] == "owner"


# --- kept with the dataset -----------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'accounts.db').as_posix()}")
    from aiscientist.gateway import auth_routes, db, metadata_store, models
    db.reset()
    db.init_db()
    with db.session_scope() as s:
        u = models.User(username="alice", password_hash="x", role="user", is_active=True)
        s.add(u)
        s.commit()
        uid = u.id
    dataset_id = auth_routes.record_dataset(uid, "Ddx41_DEG.h5ad", "/hpc/uploads/Ddx41_DEG.h5ad",
                                            512_000_000, "h5ad", sha256="ab" * 32)
    uuid = metadata_store.inputs_for_paths(uid, ["/hpc/uploads/Ddx41_DEG.h5ad"])[0]["uuid"]
    try:
        yield {"store": metadata_store, "uid": uid, "uuid": uuid, "dataset_id": dataset_id, "db": db,
               "models": models}
    finally:
        db.reset()


def _keep(st, **kw):
    args = dict(profile=dmd.profile_snapshot(PROFILE), inferred=INFERRED, model="DSV4",
                written_at="2026-10-29T12:00:00+00:00", run_id="abc123")
    args.update(kw)
    return st["store"].save_pi_description(st["uid"], st["uuid"], **args)


def test_the_description_is_kept_and_an_owners_edit_is_never_overwritten(store):
    st = store["store"]
    uid, ref = store["uid"], str(store["dataset_id"])
    assert st.description_view(uid, ref)["state"] == "none"
    first = st.description_for_run(uid, store["uuid"])
    assert first["state"] == "none" and first["facts"]["files"][0]["path"] == "Ddx41_DEG.h5ad"

    assert _keep(store)
    view = st.description_view(uid, ref)
    assert view["state"] == "pi" and view["markdown"].startswith("# Ddx41_DEG.h5ad")
    assert not view["stale"] and view["model"] == "DSV4"
    croissant = st.document_for(uid, ref, kind="dataset")
    assert croissant["description"].startswith("Ddx41_DEG.h5ad: 21,374 cells")

    edited = view["markdown"].replace("Single-nucleus RNA-seq", "Single-nucleus RNA-seq, P14,")
    view = st.save_owner_description(uid, ref, edited, by="alice")
    assert view["state"] == "owner" and "P14" in view["markdown"] and view["owner_edited_by"] == "alice"
    # a later run writes the PI's part again: the owner's text is still what is shown and read
    assert _keep(store, inferred={"summary": "Something else entirely."})
    view = st.description_view(uid, ref)
    assert view["state"] == "owner" and "P14" in view["markdown"] and "Something else" in view["pi_markdown"]
    assert st.description_for_run(uid, store["uuid"])["markdown"] == view["markdown"]
    assert "P14" in st.document_for(uid, ref, kind="dataset")["description"]
    with pytest.raises(ValueError):
        st.save_owner_description(uid, ref, "   ")
    view = st.reset_owner_description(uid, ref)
    assert view["state"] == "pi" and "Something else" in view["markdown"]
    assert st.description_view(uid + 1, ref) is None            # another account sees nothing


def test_a_description_of_data_that_changed_is_marked_stale(store):
    _keep(store)
    m = store["models"]
    with store["db"].session_scope() as s:
        row = s.query(m.MetadataRecord).filter_by(uuid=store["uuid"]).one()
        facts = row.loads("facts")
        facts["files"][0]["sha256"] = "cd" * 32
        facts["size_bytes"] = 1
        row.facts = json.dumps(facts)
        s.commit()
    view = store["store"].description_view(store["uid"], store["uuid"])
    assert view["state"] == "pi" and view["stale"] is True


def test_the_routes_show_edit_reset_and_download_it_for_the_owner_only(store, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw
    from aiscientist.gateway import auth, models
    with store["db"].session_scope() as s:
        bob = models.User(username="bob", password_hash="x", role="user", is_active=True)
        s.add(bob)
        s.commit()
        bob_id = bob.id
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)

    def client(uid):
        c = TestClient(gw.app)
        c.cookies.set(auth.COOKIE_NAME, auth.make_session_token(uid))
        return c

    base = f"/api/datasets/{store['dataset_id']}"
    alice = client(store["uid"])
    assert alice.get(f"{base}/description").json()["state"] == "none"
    assert alice.get(f"{base}/DATASET.md").status_code == 404
    _keep(store)
    got = alice.get(f"{base}/description").json()
    assert got["state"] == "pi"
    put = alice.put(f"{base}/description", json={"markdown": got["markdown"] + "\nChecked by Alice.\n"})
    assert put.status_code == 200 and put.json()["state"] == "owner"
    md_file = alice.get(f"{base}/DATASET.md")
    assert md_file.status_code == 200 and "Checked by Alice." in md_file.text
    assert md_file.headers["content-type"].startswith("text/markdown")
    assert alice.put(f"{base}/description", json={"markdown": ""}).status_code == 400
    assert alice.put(f"{base}/description", json={"text": "x"}).status_code == 400
    other = client(bob_id)
    assert other.get(f"{base}/description").status_code == 404
    assert other.put(f"{base}/description", json={"markdown": "mine"}).status_code == 404
    assert other.delete(f"{base}/description/edit").status_code == 404
    assert alice.delete(f"{base}/description/edit").json()["state"] == "pi"


# --- the PI writes it, later runs read it ---------------------------------------------------------------

def _lab(tmp_path, dd, record):
    pytest.importorskip("yaml")
    from aiscientist.agents.research_harness import HarnessContext, ResearchHarness
    from aiscientist.agents.research_lab import LabConfig, ResearchLab

    def complete(messages):
        system = messages[0]["content"]
        if system.startswith("You are the lab's PI, reading a dataset"):
            record.setdefault("describe", []).append(messages)
            return json.dumps(INFERRED)
        if "Principal Investigator of a bioinformatics lab" in system:
            record.setdefault("plan", []).append(messages[-1]["content"])
            return json.dumps({"breadth": "specific", "agenda": ["**Look** — Summarise the data."],
                               "later": []})
        if "rigorous scientific Critic" in system:
            return json.dumps({"verdict": "accept", "score": 0.9, "critique": "ok"})
        return "[]"

    def chat(messages, _tools):
        return {"content": "", "tool_calls": [{"id": "f", "type": "function", "function": {
            "name": "finish", "arguments": json.dumps({"answer": "Summarised."})}}]}

    workspace = tmp_path / "run"
    workspace.mkdir()
    decisions = {"dataset_result": dict(PROFILE), "dataset_description": dd}
    return ResearchLab(HarnessContext(decisions=decisions, tunnel_port=1, model="DSV4", workspace=workspace),
                       LabConfig(plan_review=False, auto_select_skill=False, mode="single",
                                 planner="linear"),
                       complete_fn=complete, scientist=ResearchHarness(catalog=[], chat_fn=chat)), workspace


def _first_read(state="none", markdown="", stale=False):
    return {"uuid": "u-1", "name": "Ddx41_DEG.h5ad", "state": state, "markdown": markdown,
            "stale": stale, "facts": FACTS, "user_fields": {}, "run_id": "abc123"}


def test_the_first_run_writes_it_before_planning_and_the_plan_reads_it(tmp_path):
    record: dict = {}
    lab, ws = _lab(tmp_path, _first_read(), record)
    events: list[dict] = []
    lab.run("What does this data show?", events.append)
    assert len(record["describe"]) == 1
    sent = record["describe"][0][-1]["content"]
    assert "21374" in sent and "/data/aiscientist" not in sent      # the profile, without server paths
    done = next(e for e in events if e["type"] == "dataset_described")
    assert done["uuid"] == "u-1" and done["dropped"] == 1 and done["model"] == "DSV4"
    assert done["inferred"]["summary"].startswith("Single-nucleus")
    # the planner read it, and the bundle carries it for the report
    assert "DATASET DESCRIPTION (DATASET.md, written by the PI" in record["plan"][0]
    assert "do not plan a step whose only purpose is to find it out again" in record["plan"][0]
    kept = (ws / "artifacts" / "data" / "DATASET.md").read_text()
    assert kept == done["markdown"] and "## 8. About this description" in kept


def test_a_later_run_reads_the_kept_description_instead_of_writing_it_again(tmp_path):
    record: dict = {}
    owner_md = "# Ddx41_DEG.h5ad\n\n## 1. Summary\n\nP14 mouse retinas, checked by the owner.\n"
    lab, ws = _lab(tmp_path, _first_read("owner", owner_md, stale=True), record)
    events: list[dict] = []
    lab.run("What does this data show?", events.append)
    assert "describe" not in record                           # no model call: it was read
    used = next(e for e in events if e["type"] == "dataset_description_used")
    assert used["state"] == "owner"
    assert "edited by its owner" in record["plan"][0] and "checked by the owner" in record["plan"][0]
    assert (ws / "artifacts" / "data" / "DATASET.md").read_text() == owner_md


def test_a_stale_description_by_the_pi_is_written_again(tmp_path):
    record: dict = {}
    lab, _ws = _lab(tmp_path, _first_read("pi", "# old\n", stale=True), record)
    lab.run("What does this data show?", lambda e: None)
    assert len(record["describe"]) == 1


def test_a_run_on_data_that_is_not_a_registry_dataset_describes_nothing(tmp_path):
    record: dict = {}
    lab, ws = _lab(tmp_path, None, record)
    lab.run("What does this data show?", lambda e: None)
    assert "describe" not in record and not (ws / "artifacts" / "data" / "DATASET.md").exists()
    assert "DATASET DESCRIPTION" not in record["plan"][0]


def test_the_report_points_at_the_bundled_description(tmp_path):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw
    art = tmp_path / "artifacts"
    (art / "data").mkdir(parents=True)
    (art / "data" / "dataset_results.json").write_text(json.dumps(PROFILE))
    assert "data/DATASET.md" not in gw._dataset_section(art)
    (art / "data" / "DATASET.md").write_text("# x\n")
    assert "`data/DATASET.md` in this bundle" in gw._dataset_section(art)


def test_the_run_start_hands_the_lab_the_primary_datasets_description(store, monkeypatch):
    pytest.importorskip("fastapi")
    from aiscientist.gateway import app as gw
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    conn = type("C", (), {"app_user_id": store["uid"]})()
    bound = [{"path": "/hpc/uploads/Ddx41_DEG.h5ad", "name": "Ddx41_DEG.h5ad"}]
    got = gw._dataset_description_input(conn, bound, False)
    assert got["uuid"] == store["uuid"] and got["state"] == "none"
    assert gw._dataset_description_input(conn, bound, True) is None          # an earlier run's result
    assert gw._dataset_description_input(conn, [{"path": "/elsewhere/x.h5ad"}], False) is None
    asyncio.run(asyncio.sleep(0))
    assert Path(bound[0]["path"]).name == "Ddx41_DEG.h5ad"
