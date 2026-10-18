"""Croissant metadata in the registry, the review API and the MCP server (RCHENLAB/AiScientist issue 9).

Driven through the real FastAPI app on an SQLite account database, with a mock HPC3 executor for the
upload: no SSH, no network. Pins the acceptance criteria end to end — an upload gets a valid
croissant.json with its checksum and formats; the owner reviews it in the form; a result records its
inputs and run; lineage runs both ways; the MCP server answers with the web interface's permission
checks (one account never sees another's datasets).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "croissant"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", f"sqlite:///{(tmp_path / 'accounts.db').as_posix()}")
    monkeypatch.delenv("AISCIENTIST_PUBLIC_URL", raising=False)
    from fastapi.testclient import TestClient

    from aiscientist.gateway import app as gw
    from aiscientist.gateway import auth, db, models
    from aiscientist.gateway.mock_host import MockExecutor
    from aiscientist.gateway.settings import HPCSettings

    db.reset()
    db.init_db()
    ids = {}
    with db.session_scope() as s:
        for name in ("alice", "bob"):
            u = models.User(username=name, password_hash="x", role="user", is_active=True)
            s.add(u)
            s.commit()
            ids[name] = u.id
    monkeypatch.setattr(gw, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw, "_conn_owner_error", lambda conn, request: None)
    monkeypatch.setattr(gw, "CONSOLE_RUNS_DIR", tmp_path / "runs")
    loop = asyncio.new_event_loop()
    conn = gw.Connection(HPCSettings(), mock=True, loop=loop, username="alice")
    conn.workspace = tmp_path / "runs" / "alice"
    conn.settings.uploads_on_hpc = True
    conn.executor = MockExecutor(username="alice")
    conn.app_user_id = ids["alice"]
    gw.CONNECTIONS[conn.id] = conn
    gw._STAGING.pop(conn.owner, None)

    def client_for(name: str | None):
        c = TestClient(gw.app)
        if name:
            c.cookies.set(auth.COOKIE_NAME, auth.make_session_token(ids[name]))
        return c

    try:
        yield {"gw": gw, "conn": conn, "ids": ids, "client": client_for, "tmp": tmp_path}
    finally:
        queue = gw._STAGING.pop(conn.owner, None)
        if queue:
            for job in queue.jobs():
                queue.wait(job.id, 5.0)
            queue.shutdown()
        gw.CONNECTIONS.pop(conn.id, None)
        loop.close()
        db.reset()


def _tus(client, conn, data: bytes, **fields) -> str:
    meta = ",".join(f"{k} {base64.b64encode(str(v).encode()).decode()}" for k, v in
                    {"connection_id": conn.id, **fields}.items())
    made = client.post("/api/tus", headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(len(data)),
                                            "Upload-Metadata": meta})
    assert made.status_code == 201, made.text
    url = made.headers["location"]
    sent = client.patch(url, content=data, headers={
        "Tus-Resumable": "1.0.0", "Content-Type": "application/offset+octet-stream", "Upload-Offset": "0",
        "Upload-Checksum": "sha256 " + base64.b64encode(hashlib.sha256(data).digest()).decode(),
        "Upload-Final-SHA256": hashlib.sha256(data).hexdigest()})
    assert sent.status_code == 204, sent.text
    return url


def _settle(gw, conn) -> None:
    queue = gw._staging_queue(conn)
    for job in queue.jobs():
        queue.wait(job.id, 5.0)


def _records(want: int = 1, timeout: float = 5.0) -> list:
    from aiscientist.gateway import db, models
    end = time.time() + timeout
    while True:
        with db.session_scope() as s:
            rows = list(s.query(models.MetadataRecord).order_by(models.MetadataRecord.id))
            for r in rows:
                s.expunge(r)
        if len(rows) >= want or time.time() > end:
            return rows
        time.sleep(0.02)


def _dataset(user_id: int, name: str = "retina_counts.csv", **kw) -> int:
    from aiscientist.gateway import auth_routes
    from aiscientist.tools.api import peek_dataset
    path = FIXTURES / name
    data = path.read_bytes()
    return auth_routes.record_dataset(user_id, name, f"/hpc/uploads/{name}", len(data), "csv",
                                      sha256=hashlib.sha256(data).hexdigest(),
                                      peek=peek_dataset(str(path), name=name), **kw)


# --- uploads ----------------------------------------------------------------------------------------------


def test_an_upload_copied_to_hpc3_gets_a_valid_croissant_json_with_its_checksum(env):
    gw, conn = env["gw"], env["conn"]
    data = (FIXTURES / "retina_counts.csv").read_bytes()
    _tus(env["client"]("alice"), conn, data, filename="retina_counts.csv")
    _settle(gw, conn)
    (rec,) = _records()
    doc = json.loads(rec.document)
    (entry,) = doc["distribution"]
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert entry["encodingFormat"] == ["text/csv", "http://edamontology.org/format_3752"]
    assert [f["name"] for f in doc["recordSet"][0]["field"]][:3] == ["gene", "cell_type", "count"]
    assert json.loads(rec.validation)["ok"], rec.validation
    sha_entry = next(p for p in json.loads(rec.provenance) if p["path"] == "distribution[0].sha256")
    assert (sha_entry["source"], sha_entry["confidence"]) == ("upload-check", 1.0)
    from aiscientist.gateway import db, models
    with db.session_scope() as s:
        d = s.query(models.Dataset).one()
        assert rec.uuid == d.uuid and doc["@id"] == f"urn:uuid:{d.uuid}" and rec.dataset_id == d.id
    assert "/hpc/" not in rec.document and str(conn.workspace) not in rec.document


def test_a_folder_upload_lists_each_file_with_its_checksum_and_its_tables(env):
    gw, conn = env["gw"], env["conn"]
    client = env["client"]("alice")
    folder = client.post("/api/upload/reserve-folder", json={"connection_id": conn.id, "name": "run1"}).json()["folder"]
    sent = {"tables/retina_counts.csv": (FIXTURES / "retina_counts.csv").read_bytes(),
            "libraries.tsv": (FIXTURES / "libraries.tsv").read_bytes(), "notes.txt": b"synthetic\n"}
    for rel, data in sent.items():
        _tus(client, conn, data, filename=rel.rsplit("/", 1)[-1], folder=folder, rel_path=f"{folder}/{rel}")
    reg = client.post("/api/upload/register-folder", json={"connection_id": conn.id, "folder": folder, "count": 3})
    assert reg.status_code == 200, reg.text
    _settle(gw, conn)
    (rec,) = _records()
    doc = json.loads(rec.document)
    got = {d["contentUrl"]: d.get("sha256") for d in doc["distribution"]}
    assert got == {rel: hashlib.sha256(data).hexdigest() for rel, data in sent.items()}
    assert sorted(rs["name"] for rs in doc["recordSet"]) == ["libraries", "tables_retina_counts"]
    assert json.loads(rec.validation)["ok"], rec.validation


# --- the review form --------------------------------------------------------------------------------------


def test_the_owner_reviews_the_form_and_nobody_else_can_see_it(env):
    dataset_id = _dataset(env["ids"]["alice"])
    alice, bob = env["client"]("alice"), env["client"]("bob")
    view = alice.get(f"/api/datasets/{dataset_id}/metadata").json()
    assert view["status"] == "draft" and "license" in view["form"]["needs_review"]
    assert view["form"]["values"]["tissue"] == ["retina"]                 # drafted from the file name
    assert view["form"]["sources"]["tissue"]["source"] == "file-name"
    assert bob.get(f"/api/datasets/{dataset_id}/metadata").status_code == 404
    assert bob.get(f"/api/datasets/{view['uuid']}/croissant.json").status_code == 404
    assert env["client"](None).get(f"/api/datasets/{dataset_id}/metadata").status_code == 401

    saved = alice.put(f"/api/datasets/{view['uuid']}/metadata", json={
        "name": "Retina counts", "description": "Synthetic counts.", "organism": "Homo sapiens",
        "tissue": "retina", "assay": "single-cell RNA sequencing", "license": "CC-BY-4.0",
        "accessions": "GSM5676874", "citation": ""})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["status"] == "reviewed" and body["form"]["needs_review"] == []
    doc = alice.get(f"/api/datasets/{dataset_id}/croissant.json")
    assert doc.headers["content-type"].startswith("application/ld+json")
    doc = doc.json()
    assert doc["name"] == "Retina counts" and doc["license"].endswith("CC-BY-4.0.html")
    assert doc["about"][0]["termCode"] == "NCBITaxon:9606"
    assert doc["distribution"][0]["sha256"]                           # a review never loses a checksum
    assert alice.get("/api/datasets").json()["datasets"][0]["name"] == "Retina counts"
    bad = alice.put(f"/api/datasets/{dataset_id}/metadata", json={"description": "at /data/Users/alice/x"})
    assert bad.status_code == 400 and "server path" in bad.json()["detail"]
    assert bob.put(f"/api/datasets/{dataset_id}/metadata", json={"name": "mine"}).status_code == 404


def test_deleting_a_dataset_removes_its_metadata_in_the_same_operation(env):
    from aiscientist.gateway import auth_routes
    dataset_id = _dataset(env["ids"]["alice"])
    assert len(_records()) == 1
    assert auth_routes.delete_dataset_record(env["ids"]["bob"], dataset_id) is None   # not bob's
    assert len(_records()) == 1
    assert auth_routes.delete_dataset_record(env["ids"]["alice"], dataset_id)
    assert _records(want=0, timeout=0) == []


def test_a_dataset_older_than_its_metadata_gets_a_draft_under_its_own_uuid(env):
    """A registry row written before metadata existed (its UUID comes from issue 5's backfill in
    ``init_db``) is drafted by the start-up / weekly re-check, under that same UUID."""
    from sqlalchemy import text

    from aiscientist.gateway import db, metadata_store
    old_uuid = "0b0e6a8e-1f0c-4a8a-9d55-5d1f3b1c2e7a"
    with db.get_engine().begin() as c:
        c.execute(text("INSERT INTO datasets (user_id, uuid, status, name, path, size_bytes, kind, sha256) "
                       "VALUES (:u, :uuid, 'complete', 'old.h5ad', '/hpc/old.h5ad', 10, 'h5ad', NULL)"),
                  {"u": env["ids"]["alice"], "uuid": old_uuid})
    assert _records(want=0, timeout=0) == []
    assert metadata_store.ensure_dataset_records() == 1
    assert metadata_store.ensure_dataset_records() == 0           # drafted once
    (rec,) = _records()
    assert rec.uuid == old_uuid and json.loads(rec.document)["@id"] == f"urn:uuid:{old_uuid}"
    doc = json.loads(rec.document)
    assert doc["distribution"][0]["@type"] == "cr:FileSet"         # no checksum was ever measured
    assert json.loads(rec.validation)["ok"]
    assert next(p for p in json.loads(rec.provenance) if p["path"] == "distribution[0].contentSize")["source"] == "registry"


# --- results and lineage -----------------------------------------------------------------------------------


def _result(env, run_id: str, inputs: list[dict]) -> dict:
    from aiscientist import metadata as md
    from aiscientist.gateway import metadata_store
    art = env["tmp"] / "runs" / "alice" / run_id / "artifacts"
    (art / "tables").mkdir(parents=True, exist_ok=True)
    (art / "tables" / "markers.csv").write_bytes((FIXTURES / "retina_counts.csv").read_bytes())
    facts = {"run_id": run_id, "owner": "alice", "status": "done", "question": "Which genes mark rods?",
             "finished_at": "2026-10-16T10:00:00+00:00", "software": {"version": "0.2.0"},
             "parameters": [{"step": "DE", "tool": "run_de", "args": {"groupby": "cell_type"}}],
             "files": md.result_files(art)}
    return metadata_store.record_result(env["ids"]["alice"], run_id, facts, inputs)


def test_a_result_links_to_its_inputs_and_lineage_runs_both_ways(env):
    from aiscientist import metadata as md
    from aiscientist.gateway import auth_routes, db, metadata_store, models
    alice = env["ids"]["alice"]
    dataset_id = _dataset(alice)
    with db.session_scope() as s:
        uuid = s.get(models.Dataset, dataset_id).uuid
    doc = _result(env, "run000000001", [{"uuid": uuid, "name": "retina_counts.csv", "kind": "dataset"}])
    rid = md.result_uuid("run000000001")
    assert doc["prov:wasDerivedFrom"] == [{"@id": f"urn:uuid:{uuid}"}]
    # a dataset saved from that result, then a second result made from the saved dataset
    saved = auth_routes.record_dataset(alice, "x.processed.h5ad", "/hpc/AiScientist_results/x.processed.h5ad",
                                       100, "h5ad", source="result",
                                       derived_from={"uuid": rid, "run_id": "run000000001"})
    with db.session_scope() as s:
        saved_uuid = s.get(models.Dataset, saved).uuid
    _result(env, "run000000002", [{"uuid": saved_uuid, "name": "x.processed.h5ad", "kind": "dataset"}])

    down = metadata_store.lineage(alice, uuid, direction="downstream", depth=5)
    assert {(e["from"], e["to"], e["relation"]) for e in down["edges"]} == {
        (uuid, rid, "input"), (rid, saved_uuid, "saved_from"),
        (saved_uuid, md.result_uuid("run000000002"), "input")}
    up = metadata_store.lineage(alice, md.result_uuid("run000000002"), direction="upstream", depth=5)
    assert {n["uuid"] for n in up["nodes"]} == {uuid, rid, saved_uuid, md.result_uuid("run000000002")}
    run_node = next(n for n in up["nodes"] if n["uuid"] == rid)
    assert run_node["run_id"] == "run000000001" and run_node["parameters"][0]["tool"] == "run_de"
    assert metadata_store.lineage(env["ids"]["bob"], uuid) is None             # not bob's
    # the result's metadata over HTTP, by run id or by UUID, for its owner only
    client = env["client"]("alice")
    assert client.get("/api/results/run000000001/croissant.json").json()["@id"] == f"urn:uuid:{rid}"
    assert client.get(f"/api/metadata/{rid}/lineage?direction=downstream").json()["edges"]
    assert env["client"]("bob").get("/api/results/run000000001/metadata").status_code == 404
    # deleting the input leaves the result's provenance, now naming a deleted input
    auth_routes.delete_dataset_record(alice, dataset_id)
    gone = metadata_store.lineage(alice, rid, direction="upstream")
    assert next(n for n in gone["nodes"] if n["uuid"] == uuid)["deleted"] is True


def test_search_matches_text_terms_and_accessions(env):
    from aiscientist.gateway import metadata_store
    alice = env["ids"]["alice"]
    a = _dataset(alice)
    _dataset(alice, "variants.vcf")
    metadata_store.save_form(alice, str(a), {"organism": "mouse", "accessions": "GSE123456"})
    hits = lambda **kw: [h["name"] for h in metadata_store.search(alice, **kw)]  # noqa: E731
    assert hits(organism="NCBITaxon:10090") == ["retina_counts.csv"]
    assert hits(organism="homo sapiens") == ["variants.vcf"]                 # drafted from GRCh38
    assert hits(accession="gse123456") == ["retina_counts.csv"]
    assert hits(query="mean_expr") == ["retina_counts.csv"]                  # a column name
    assert hits(query="vcf") == ["variants.vcf"]
    assert metadata_store.search(env["ids"]["bob"], query="retina") == []


# --- MCP ----------------------------------------------------------------------------------------------------


def _rpc(client, method: str, params: dict | None = None, *, token: str = "", msg_id: int = 1, **headers):
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, **({"params": params} if params else {})}
    return client.post("/mcp", json=body, headers=headers)


def test_mcp_lists_searches_reads_and_previews_with_a_token(env):
    from aiscientist.gateway import db, models
    alice = env["ids"]["alice"]
    dataset_id = _dataset(alice)
    with db.session_scope() as s:
        uuid = s.get(models.Dataset, dataset_id).uuid
    _result(env, "run000000003", [{"uuid": uuid, "name": "retina_counts.csv", "kind": "dataset"}])
    made = env["client"]("alice").post("/api/account/tokens", json={"name": "my laptop"}).json()
    token = made["token"]
    assert made["prefix"] == token[:12] and token.startswith("aisci_")
    client = env["client"](None)                                      # no cookie: the token alone

    init = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "0"}}, token=token).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"]["tools"] and init["result"]["serverInfo"]["name"] == "aiscientist"
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                       headers={"Authorization": f"Bearer {token}"})
    assert note.status_code == 202
    tools = {t["name"] for t in _rpc(client, "tools/list", token=token).json()["result"]["tools"]}
    assert tools == {"list_datasets", "search_datasets", "get_dataset_metadata", "get_provenance", "preview_records"}

    def call(name, **args):
        out = _rpc(client, "tools/call", {"name": name, "arguments": args}, token=token).json()["result"]
        return out
    listed = call("list_datasets")
    assert not listed["isError"] and {i["kind"] for i in listed["structuredContent"]["items"]} == {"dataset", "result"}
    found = call("search_datasets", query="retina", kind="dataset")["structuredContent"]["items"]
    assert [i["uuid"] for i in found] == [uuid]
    meta = call("get_dataset_metadata", id=f"urn:uuid:{uuid}")["structuredContent"]
    assert meta["croissant"]["@id"] == f"urn:uuid:{uuid}" and meta["provenance"]
    prov = call("get_provenance", id=uuid, direction="downstream")["structuredContent"]
    assert prov["edges"][0]["relation"] == "input"
    from aiscientist import metadata as md
    preview = call("preview_records", id=md.result_uuid("run000000003"), limit=2)["structuredContent"]
    assert [str(r["gene"]) for r in preview["rows"]] == ["RHO", "GNAT1"]
    missing = call("get_dataset_metadata", id="00000000-0000-0000-0000-000000000000")
    assert missing["isError"] and "No dataset" in missing["content"][0]["text"]

    res = _rpc(client, "resources/list", token=token).json()["result"]["resources"]
    uris = {r["uri"] for r in res}
    assert f"aiscientist://datasets/{uuid}/croissant.json" in uris
    read = _rpc(client, "resources/read", {"uri": f"aiscientist://datasets/{uuid}/croissant.json"},
                token=token).json()["result"]["contents"][0]
    assert read["mimeType"] == "application/ld+json" and json.loads(read["text"])["@id"] == f"urn:uuid:{uuid}"
    assert _rpc(client, "nope/nope", token=token).json()["error"]["code"] == -32601


def test_mcp_applies_the_web_interfaces_permission_checks(env):
    from aiscientist.gateway import db, models
    alice = env["ids"]["alice"]
    dataset_id = _dataset(alice)
    with db.session_scope() as s:
        uuid = s.get(models.Dataset, dataset_id).uuid
    bob_token = env["client"]("bob").post("/api/account/tokens", json={"name": "b"}).json()["token"]
    client = env["client"](None)
    listed = _rpc(client, "tools/call", {"name": "list_datasets", "arguments": {}}, token=bob_token).json()
    assert listed["result"]["structuredContent"]["items"] == []
    peek = _rpc(client, "tools/call", {"name": "get_dataset_metadata", "arguments": {"id": uuid}},
                token=bob_token).json()["result"]
    assert peek["isError"]
    read = _rpc(client, "resources/read", {"uri": f"aiscientist://datasets/{uuid}/croissant.json"},
                token=bob_token).json()
    assert read["error"]["code"] == -32002
    # no credentials, a made-up token, a revoked token, a disabled account: 401
    assert _rpc(client, "tools/list").status_code == 401
    assert _rpc(client, "tools/list", token="aisci_madeup_example_token_value").status_code == 401
    tokens = env["client"]("bob").get("/api/account/tokens").json()["tokens"]
    assert env["client"]("bob").delete(f"/api/account/tokens/{tokens[0]['id']}").status_code == 200
    assert _rpc(client, "tools/list", token=bob_token).status_code == 401
    alice_token = env["client"]("alice").post("/api/account/tokens", json={}).json()["token"]
    with db.session_scope() as s:
        s.get(models.User, alice).is_active = False
        s.commit()
    assert _rpc(client, "tools/list", token=alice_token).status_code == 401
    # the console's own session works too, but never from another site's page
    assert _rpc(env["client"]("bob"), "tools/list").status_code == 200
    assert _rpc(env["client"]("bob"), "tools/list", Origin="https://evil.example").status_code == 403
    assert client.get("/mcp").status_code == 405


def test_saving_a_result_to_my_data_drafts_metadata_linked_to_its_run(env):
    from aiscientist import metadata as md
    gw = env["gw"]
    run_id = "run000000004"
    owner_dir = env["tmp"] / "runs" / "alice"
    result = owner_dir / run_id / "result"
    result.mkdir(parents=True)
    name = f"retina.processed.20261016-1000.{run_id}.h5ad"
    (result / name).write_bytes(b"\x89HDF\r\n\x1a\n" + b"0" * 64)
    pointer = gw._result_pointer_path("alice", run_id)
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"name": name, "path": str(result / name), "size_bytes": 72,
                                   "n_cells": 900, "n_genes": 300, "obs_columns": ["cell_type"],
                                   "layers": ["counts"], "location": "gateway"}))
    status, body = gw._save_run_result("alice", env["ids"]["alice"], run_id, "dataset", None)
    assert status == 200, body
    (rec,) = _records()
    doc = json.loads(rec.document)
    assert doc["prov:wasDerivedFrom"] == [{"@id": f"urn:uuid:{md.result_uuid(run_id)}"}]
    assert "900 observations × 300 variables" in doc["distribution"][0]["description"]
    assert json.loads(rec.validation)["ok"], rec.validation


# --- the end of a run, and the planner --------------------------------------------------------------------


def test_a_finished_run_writes_its_croissant_json_into_the_bundle_and_the_registry(env):
    from types import SimpleNamespace

    from aiscientist import metadata as md
    from aiscientist.gateway import db, metadata_store, models
    gw, conn, alice = env["gw"], env["conn"], env["ids"]["alice"]
    dataset_id = _dataset(alice)
    with db.session_scope() as s:
        d = s.get(models.Dataset, dataset_id)
        path, uuid = d.path, d.uuid
    run_id = "run000000005"
    inputs = gw._metadata_inputs(conn, [{"path": path, "name": "retina_counts.csv", "role": None}],
                                 {"run_id": "run000000004"}, True)
    assert [(i["kind"], i["uuid"]) for i in inputs] == [("result", md.result_uuid("run000000004")),
                                                        ("dataset", uuid)]
    art = conn.workspace / run_id / "artifacts"
    (art / "tables").mkdir(parents=True)
    (art / "tables" / "de.csv").write_bytes((FIXTURES / "retina_counts.csv").read_bytes())
    req = SimpleNamespace(question="Compare rods in /data/Users/alice/x.h5ad", route="research", mode="single",
                          planner="dag", presets=["scrna_standard"], skills=[], plan_mode=False, autonomous=False)
    rounds = [{"step": "DE", "verdict": {"verdict": "accept"}, "scientist_result": {"steps": [
        {"tool": "run_de", "ok": True, "args": {"groupby": "cell_type", "code": "secret()"}}]}}]
    gw._write_result_metadata(conn, run_id, art, req, {"metadata_inputs": inputs}, "done", rounds, ["qwen-test"])
    doc = json.loads((art / "croissant.json").read_text())
    assert doc == metadata_store.document_for(alice, run_id)
    act = doc["prov:wasGeneratedBy"]
    assert act["aiscientist:parameters"] == [{"step": "DE", "tool": "run_de", "args": {"groupby": "cell_type"}}]
    assert any("qwen-test" in a["prov:label"] for a in act["prov:wasAssociatedWith"])
    assert [x["@id"] for x in doc["prov:wasDerivedFrom"]] == [f"urn:uuid:{md.result_uuid('run000000004')}",
                                                               f"urn:uuid:{uuid}"]
    assert "/data/" not in json.dumps(doc) and doc["distribution"][0]["contentUrl"] == "tables/de.csv"
    assert md.validate(doc)["ok"]
    # accounts off: the bundle still gets its croissant.json
    env["gw"]._AUTH_ENABLED = False
    (art / "croissant.json").unlink()
    gw._write_result_metadata(conn, "run000000006", art, req, {}, "incomplete")
    assert json.loads((art / "croissant.json").read_text())["prov:wasGeneratedBy"]["aiscientist:status"] == "incomplete"


def test_the_planner_reads_the_bound_datasets_metadata_marked_reviewed_or_drafted():
    from types import SimpleNamespace

    from aiscientist.agents.research_lab import ResearchLab
    notes = [{"name": "retina.h5ad", "uuid": "u-1", "reviewed": True, "description": "Mouse retina, P14.",
              "about": ["Mus musculus", "retina"], "assay": ["10x 3' v3"], "terms_source": "owner",
              "accessions": ["GSE123456"], "citation": "Doe 2026", "record_sets": []},
             {"name": "panel.csv", "uuid": "u-2", "reviewed": False, "description": "", "about": ["Homo sapiens"],
              "assay": [], "terms_source": "drafted", "accessions": [], "citation": "", "record_sets": ["panel"]}]
    fake = SimpleNamespace(ctx=SimpleNamespace(decisions={"dataset_metadata": notes}))
    text = ResearchLab._metadata_context(fake)
    assert "retina.h5ad (UUID u-1; reviewed by its owner)" in text
    assert "Mus musculus, retina, 10x 3' v3 (owner)" in text and "accessions: GSE123456" in text
    assert "panel.csv (UUID u-2; drafted automatically, not reviewed)" in text
    assert "Homo sapiens (drafted — verify against the data)" in text
    assert ResearchLab._metadata_context(SimpleNamespace(ctx=SimpleNamespace(decisions={}))) == ""
