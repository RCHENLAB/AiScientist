"""A run reads a shared dataset in place (RCHENLAB/AiScientist issue 100, section 3).

``latest`` is resolved once at run start; the verified HPC3 copy is bound where it is (nothing lands in
sessions/<id>/inputs/); a version with no verified copy falls back to the per-session copy and the run
says so; remote paths are admitted by allowlisted prefix only; each run records what it read in
``artifacts/process/inputs.json``; a run never writes a shared dataset's metadata; the planner sees the
version and each file's facets; the atlas bridge accepts a shared file as its primary atlas.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")
pytest.importorskip("fastapi")

import test_shared_datasets as _store_tests  # noqa: E402

_publish = _store_tests._publish
env = _store_tests.env  # the store fixture, used by name below

HPC_ROOT = "/dfs3b/lab/shared"


@pytest.fixture()
def gw(env, monkeypatch):
    from aiscientist.gateway import app as gw_app
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setenv("AISCIENTIST_HPC_SHARED_ROOT", HPC_ROOT)
    return gw_app


def _conn(tmp_path, *, on_hpc=True):
    settings = SimpleNamespace(shared_root=HPC_ROOT, lab_storage="/dfs3b/lab", analysis_on_hpc=on_hpc,
                               run_code_on_hpc=on_hpc)
    return SimpleNamespace(settings=settings, executor=object(), mock=False, workspace=tmp_path / "ws",
                           owner="bob", id="sess1", app_user_id=2)


def _req(gw, refs):
    return gw.LabRequest(connection_id="c", question="q", shared_datasets=refs)


def test_latest_is_resolved_once_and_the_hpc3_path_appears_only_when_verified(gw, env, tmp_path):
    _publish(env)
    conn = _conn(tmp_path)
    first = gw._resolve_shared_refs(_req(gw, [{"shared": "heca"}]), conn)[0]
    assert (first["requested"], first["version"], first["whole"]) == ("latest", "v2026.11", True)
    assert first["entries"][0]["hpc"] is None
    env["sd"].mark_hpc_verified("heca", "v2026.11")
    again = gw._resolve_shared_refs(_req(gw, [{"shared": "heca"}]), conn)[0]
    assert again["entries"][0]["hpc"] == f"{HPC_ROOT}/datasets/heca/v2026.11"
    one = gw._resolve_shared_refs(_req(gw, [{"shared": "heca", "files": ["Lens_scRNA_allcells.h5ad"]}]), conn)[0]
    assert one["entries"][0]["hpc"] == f"{HPC_ROOT}/datasets/heca/v2026.11/Lens_scRNA_allcells.h5ad"
    assert [f["name"] for f in one["files"]] == ["Lens_scRNA_allcells.h5ad"] and one["files"][0]["sha256"]


def _pin_chat(env, user_id, version):
    """A chat of this account pinned to ``version`` of heca, attached the way the console does it."""
    models = env["models"]
    with env["db"].session_scope() as s:
        conv = models.Conversation(user_id=user_id, title="pinned")
        s.add(conv)
        s.flush()
        env["sd"].replace_attachments(s, conv.id, [{"shared": "heca", "version": version}])
        s.commit()


def test_a_withdrawn_version_is_not_reached_by_latest_but_a_pinned_chat_still_runs(gw, env, tmp_path):
    from fastapi import HTTPException
    _publish(env)
    (env["source"] / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v2 " * 1000)
    _publish(env, label="v2026.12", changelog="Lens.")
    conn = _conn(tmp_path)
    _pin_chat(env, conn.app_user_id, "v2026.12")
    env["sd"].withdraw("heca", "v2026.12", by="jin")
    assert gw._resolve_shared_refs(_req(gw, [{"shared": "heca"}]), conn)[0]["version"] == "v2026.11"
    env["sd"].withdraw("heca", "v2026.11", by="jin")
    with pytest.raises(HTTPException):
        gw._resolve_shared_refs(_req(gw, [{"shared": "heca"}]), conn)
    pinned = gw._resolve_shared_refs(_req(gw, [{"shared": "heca", "version": "v2026.12"}]), conn)[0]
    assert pinned["version"] == "v2026.12" and pinned["status"] == "withdrawn"
    # No chat of this account is pinned to v2026.11, and no chat of another account to v2026.12:
    # the request body alone does not reach a withdrawn version.
    other = SimpleNamespace(**{**vars(conn), "app_user_id": conn.app_user_id + 1})
    for who, version in ((conn, "v2026.11"), (other, "v2026.12")):
        with pytest.raises(HTTPException, match="withdrawn"):
            gw._resolve_shared_refs(_req(gw, [{"shared": "heca", "version": version}]), who)
    for bad in ({"shared": "nope"}, {"shared": "heca", "version": "v2026.12", "files": ["x.h5ad"]}):
        with pytest.raises(HTTPException):
            gw._resolve_shared_refs(_req(gw, [bad]), conn)


def test_the_request_body_cannot_set_the_resolved_paths(gw):
    req = gw.LabRequest(connection_id="c", question="q",
                        **{"_shared_resolved": [{"entries": [{"path": "/etc", "name": "x"}]}]})
    assert getattr(req, "_shared_resolved") is None
    assert gw._select_bound_datasets(req) == []


def test_a_whole_version_is_one_attachment_toward_the_limit(gw, env, tmp_path):
    from fastapi import HTTPException
    _publish(env)
    conn = _conn(tmp_path)
    req = _req(gw, [{"shared": "heca"}])
    req._shared_resolved = gw._resolve_shared_refs(req, conn)
    assert len(gw._select_bound_datasets(req)) == 1
    req.datasets = [{"path": f"/data/d{i}.h5ad"} for i in range(gw._MAX_BOUND_DATASETS)]
    with pytest.raises(HTTPException):
        gw._select_bound_datasets(req)
    req.datasets = req.datasets[:-1]
    assert len(gw._select_bound_datasets(req)) == gw._MAX_BOUND_DATASETS


def _admit(gw, conn, req, monkeypatch, copies):
    monkeypatch.setattr(gw, "_sync_server_path",
                        lambda c, p, root, say: copies.append(str(p)) or ("/sessions/inputs/x", {"a"}))
    monkeypatch.setattr(gw, "_kept_data_root", lambda c: None)
    said, warned, refused = [], [], []
    bound = gw._select_bound_datasets(req)
    admitted, mirrors = asyncio.run(gw._admit_bound_paths(
        conn, bound, lambda text, level="info": said.append(text), lambda *a: warned.append(a), refused,
        gw._shared_by_path(req._shared_resolved)))
    return admitted, mirrors, said, refused


def test_a_verified_copy_is_read_in_place_and_nothing_is_copied_into_the_session(gw, env, tmp_path, monkeypatch):
    _publish(env)
    env["sd"].mark_hpc_verified("heca", "v2026.11")
    conn = _conn(tmp_path)
    req = _req(gw, [{"shared": "heca"}])
    req._shared_resolved = gw._resolve_shared_refs(req, conn)
    copies = []
    admitted, mirrors, said, refused = _admit(gw, conn, req, monkeypatch, copies)
    assert copies == [] and refused == [] and len(admitted) == 1
    path = admitted[0]["path"]
    assert mirrors[path][0] == f"{HPC_ROOT}/datasets/heca/v2026.11"
    assert "Iris_snRNA_allcells.h5ad" in mirrors[path][1]
    assert any("read in place" in line for line in said)
    # The folder's primary file maps to its verified copy, the way a bound server folder's does.
    from pathlib import Path
    assert gw._mirror_of(mirrors, path, Path(path) / "Iris_snRNA_allcells.h5ad") == (
        f"{HPC_ROOT}/datasets/heca/v2026.11/Iris_snRNA_allcells.h5ad")


def test_a_version_with_no_verified_copy_falls_back_to_the_session_copy_and_says_so(gw, env, tmp_path, monkeypatch):
    _publish(env)
    conn = _conn(tmp_path)
    req = _req(gw, [{"shared": "heca", "files": ["Lens_scRNA_allcells.h5ad"]}])
    req._shared_resolved = gw._resolve_shared_refs(req, conn)
    copies = []
    admitted, mirrors, said, _ = _admit(gw, conn, req, monkeypatch, copies)
    assert len(copies) == 1 and copies[0].endswith("Lens_scRNA_allcells.h5ad")
    assert any("no verified copy on HPC3" in line for line in said)
    assert len(admitted) == 1


def test_a_run_that_computes_here_reads_the_store_in_place_with_no_copy(gw, env, tmp_path, monkeypatch):
    _publish(env)
    conn = _conn(tmp_path, on_hpc=False)
    req = _req(gw, [{"shared": "heca"}])
    req._shared_resolved = gw._resolve_shared_refs(req, conn)
    copies = []
    admitted, mirrors, _, _ = _admit(gw, conn, req, monkeypatch, copies)
    assert copies == [] and mirrors == {} and admitted[0]["path"].startswith(str(env["store"]))


def test_remote_paths_are_admitted_by_allowlisted_prefix_only(gw, tmp_path):
    from aiscientist.gateway import session_state
    conn = _conn(tmp_path)
    session_state.save_record(conn.workspace, {"id": "rec0", "connection_id": "sess0"})
    ok = [f"/dfs3b/lab/{gw._hpc_user(conn)}/uploads/a.h5ad", f"{HPC_ROOT}/datasets/heca/v1/x.h5ad",
          f"{HPC_ROOT}/sessions/sess1/inputs/x.h5ad", f"{HPC_ROOT}/sessions/sess0/runs/r1/result/x.csv"]
    for path in ok:
        assert gw._remote_path_admitted(conn, path), path
    # Another account's session folder is not this account's, though it sits in the same sessions/.
    for path in ("/dfs3b/lab/someone_else/uploads/a.h5ad", "/dfs3b/lab/x.h5ad",
                 f"{HPC_ROOT}/sessions/someone_elses/inputs/x.h5ad", f"{HPC_ROOT}/sessions/sess1x/x.h5ad",
                 f"{HPC_ROOT}/sessions", f"{HPC_ROOT}/datasets/../../other/x.h5ad", ""):
        assert not gw._remote_path_admitted(conn, path), path


def test_inputs_json_records_the_version_and_checksums_and_a_newer_version_is_said(gw, env, tmp_path):
    _publish(env)
    conn = _conn(tmp_path)
    req = _req(gw, [{"shared": "heca"}])
    resolved = gw._resolve_shared_refs(req, conn)
    art, decisions, said = tmp_path / "art", {}, []
    asyncio.run(gw._record_shared_inputs(conn, "run1", art, decisions, resolved, None, lambda t, level="info": said.append(t)))
    data = json.loads((art / "process" / "inputs.json").read_text())
    rec = data["shared_inputs"][0]
    assert (rec["dataset"], rec["requested"], rec["version"]) == ("heca", "latest", "v2026.11")
    assert rec["version_uuid"] and rec["manifest_sha256"]
    assert {"name", "size", "sha256"} <= set(rec["files"][0])
    assert str(env["store"]) not in json.dumps(data)
    assert any("uses" in t and "v2026.11" in t for t in said)
    # A later run in the same chat, after a release: the reply says which version it used.
    (env["source"] / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v2 " * 1000)
    _publish(env, label="v2026.12", changelog="Lens updated.")
    prior_dir = conn.workspace / "run1" / "artifacts" / "process"
    prior_dir.mkdir(parents=True)
    (prior_dir / "inputs.json").write_text(json.dumps(data))
    resolved = gw._resolve_shared_refs(req, conn)
    said.clear()
    asyncio.run(gw._record_shared_inputs(conn, "run2", tmp_path / "art2", {}, resolved, {"run_id": "run1"},
                                         lambda t, level="info": said.append(t)))
    assert any("newer version" in t and "v2026.12" in t and "v2026.11" in t for t in said)
    assert json.loads((tmp_path / "art2" / "process" / "inputs.json").read_text())["shared_inputs"][0]["version"] == "v2026.12"


def test_a_run_never_writes_a_shared_datasets_metadata(gw, env, tmp_path):
    _publish(env)
    conn = _conn(tmp_path)
    bound = [{"path": str(env["store"] / "heca" / "v2026.11" / "Iris_snRNA_allcells.h5ad"), "name": "x", "role": None}]
    assert gw._is_shared_path(bound[0]["path"])
    assert gw._dataset_description_input(conn, bound, False) is None
    assert not gw._is_shared_path("/data/Users/other/x.h5ad")


def test_the_planner_sees_the_version_each_files_facets_and_the_memory_note():
    from aiscientist.agents.research_lab import ResearchLab
    decisions = {"shared_inputs": [{"dataset": "heca", "title": "HECA", "requested": "latest", "version": "v2026.11",
        "files": [{"name": "HRCA_snRNA_allcells.h5ad", "size": 1, "sha256": "a" * 64,
                   "facets": {"tissue": "retina", "modality": "snRNA", "n_obs": 3180000, "n_vars": 36000,
                              "obs_columns": ["celltype", "donor"]}}]}]}
    stub = SimpleNamespace(ctx=SimpleNamespace(decisions=decisions))
    stub._shared_inputs_context = lambda: ResearchLab._shared_inputs_context(stub)
    text = ResearchLab._attached_files_context(stub)
    assert "HECA v2026.11 (latest at the start of this run)" in text
    assert "retina; snRNA; 3,180,000 cells; 36,000 genes; obs: celltype, donor" in text
    assert "218 GiB" in text and "3.18M nuclei" in text


def test_the_atlas_bridge_accepts_a_shared_file_with_no_atlas_files_entry(gw, env, tmp_path, monkeypatch):
    from aiscientist.agents import atlas_expression as ae
    _publish(env)
    monkeypatch.delenv("AISCIENTIST_ATLAS_FILES", raising=False)
    local = str(env["store"] / "heca" / "v2026.11" / "Iris_snRNA_allcells.h5ad")
    hpc = f"{HPC_ROOT}/datasets/heca/v2026.11/Iris_snRNA_allcells.h5ad"
    assert ae.atlases_available()
    assert ae.shared_release(local) == "heca/v2026.11" and ae.shared_release(hpc) == "heca/v2026.11"
    assert ae.shared_release(f"{HPC_ROOT}/datasets/heca/v2026.11/other.h5ad") is None
    assert ae.shared_release("/data/elsewhere/Iris_snRNA_allcells.h5ad") is None
    call = ae.AtlasAdapter(planner_name=lambda n: n).prepare({}, SimpleNamespace(dataset_path=hpc, dataset_source=local))
    assert "heca/v2026.11" in call.setup


# --- Croissant and MCP (section 4) -----------------------------------------------------------------


def test_publish_writes_a_croissant_json_that_mlcroissant_accepts(env):
    mlc = pytest.importorskip("mlcroissant")
    result = _publish(env)
    path = env["store"] / "heca" / "v2026.11" / "croissant.json"
    assert path.is_file() and (path.stat().st_mode & 0o222) == 0          # frozen with the version
    doc = json.loads(path.read_text())
    manifest = json.loads((env["store"] / "heca" / "v2026.11" / "manifest.json").read_text())
    assert doc["version"] == "v2026.11" and doc["@id"] == f"urn:uuid:{result['uuid']}"
    assert doc["aiscientist:manifestSha256"] == result["manifest_sha256"]
    sums = {f["name"]: f["sha256"] for f in manifest["files"]}
    objects = {d["name"]: d for d in doc["distribution"]}
    assert set(objects) == set(sums)
    assert all(d["@type"] == "cr:FileObject" and d["sha256"] == sums[n] for n, d in objects.items())
    assert objects["Iris_snRNA_allcells.h5ad"]["aiscientist:facets"]["tissue"] == "iris"
    assert objects["Iris_snRNA_allcells.h5ad"]["aiscientist:facets"]["n_obs"] == 3
    assert str(env["store"]) not in json.dumps(doc)
    assert "croissant.json" not in sums                                     # a reserved name, not a data file
    ds = mlc.Dataset(jsonld=str(path))                                       # raises on an invalid document
    assert ds.metadata.version == "v2026.11"
    from aiscientist.metadata import validate
    if hasattr(validate, "validate_document"):
        assert validate.validate_document(doc).get("ok", True)


def test_the_api_serves_the_croissant_of_every_version_and_a_result_links_its_uuid(env, gw, tmp_path):
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    result = _publish(env)
    app = FastAPI()
    app.include_router(env["auth_routes"].router)
    app.include_router(env["shared_routes"].router)
    client = TestClient(app)
    assert client.get("/api/shared-datasets/heca/versions/latest/croissant.json").status_code == 401
    client.post("/api/auth/login", json={"username": "bob", "password": "bobpass1"})
    got = client.get("/api/shared-datasets/heca/versions/latest/croissant.json")
    assert got.status_code == 200
    on_disk = json.loads((env["store"] / "heca" / "v2026.11" / "croissant.json").read_text())
    assert got.json()["@id"] == on_disk["@id"] and got.json()["distribution"] == on_disk["distribution"]
    assert client.get("/api/shared-datasets/heca/versions/v9/croissant.json").status_code == 404
    # The result of a run that read it names the version's uuid as an input.
    conn = _conn(tmp_path)
    req = _req(gw, [{"shared": "heca", "role": "primary"}])
    resolved = gw._resolve_shared_refs(req, conn)
    decisions = {}
    asyncio.run(gw._record_shared_inputs(conn, "run1", tmp_path / "a", decisions, resolved, None, lambda *a, **k: None))
    inputs = gw._metadata_inputs(conn, [], None, False, decisions["shared_inputs"])
    assert inputs == [{"uuid": result["uuid"], "kind": "dataset", "shared": True,
                       "name": "Human Eye Cell Atlas (HECA) v2026.11", "role": "primary"}]
    from aiscientist import metadata as md
    built = md.build_result("r" * 8, {"run_id": "run1", "inputs": inputs}).document
    assert {"@id": f"urn:uuid:{result['uuid']}"} in built["prov:wasDerivedFrom"]


def test_an_mcp_token_lists_and_reads_the_shared_dataset_read_only(env):
    from aiscientist.gateway import mcp_server
    result = _publish(env)
    with env["db"].session_scope() as s:
        bob = s.query(env["models"].User).filter_by(username="bob").one()
        s.expunge(bob)
    reads = frozenset({mcp_server.READ})                        # a read-only token
    listed = mcp_server.call_tool(bob, "list_datasets", {}, reads)
    body = json.loads(listed["content"][0]["text"])
    shared = [i for i in body["items"] if i.get("shared")]
    assert [i["uuid"] for i in shared] == [result["uuid"]] and shared[0]["slug"] == "heca"
    assert shared[0]["tissue"] and "iris" in shared[0]["tissue"] and shared[0]["file_count"] == 3
    assert str(env["store"]) not in json.dumps(body)
    assert [i["uuid"] for i in json.loads(mcp_server.call_tool(
        bob, "search_datasets", {"query": "eye atlas"}, reads)["content"][0]["text"])["items"]] == [result["uuid"]]
    assert json.loads(mcp_server.call_tool(bob, "search_datasets", {"tissue": "iris"}, reads)["content"][0]["text"])["count"] == 1
    assert json.loads(mcp_server.call_tool(bob, "search_datasets", {"tissue": "liver"}, reads)["content"][0]["text"])["count"] == 0
    meta = json.loads(mcp_server.call_tool(bob, "get_dataset_metadata", {"id": f"urn:uuid:{result['uuid']}"}, reads)["content"][0]["text"])
    assert meta["shared"] is True and meta["croissant"]["version"] == "v2026.11"
    only_results = json.loads(mcp_server.call_tool(bob, "list_datasets", {"kind": "result"}, reads)["content"][0]["text"])
    assert not [i for i in only_results["items"] if i.get("shared")]
    resource = mcp_server._resources_read(bob, {"uri": f"aiscientist://datasets/{result['uuid']}/croissant.json"})
    assert json.loads(resource["contents"][0]["text"])["version"] == "v2026.11"


# --- publishing from the console (section 5) -------------------------------------------------------


def _login(env, user):
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(env["auth_routes"].router)
    app.include_router(env["shared_routes"].router)
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": user, "password": f"{user}pass1"}).status_code == 200
    return client


def _wait(client, slug, job):
    import time
    for _ in range(200):
        view = client.get(f"/api/shared-datasets/{slug}/publish-jobs/{job}").json()
        if view["state"] != "running":
            return view
        time.sleep(0.05)
    raise AssertionError("the publish job did not finish")


def test_a_non_maintainer_gets_403_and_a_maintainer_publishes_from_the_console(env):
    body = {"source": str(env["source"]), "label": "v2026.11", "changelog": "First release."}
    bob = _login(env, "bob")                       # an account, not a maintainer
    assert bob.post("/api/shared-datasets/heca/publish", json=body).status_code == 403
    assert bob.post("/api/shared-datasets/heca/versions/v2026.11/withdraw").status_code == 403
    listing = bob.get("/api/shared-datasets").json()
    assert listing["datasets"][0]["can_publish"] is False and "source_roots" not in listing
    jin = _login(env, "jin")                       # a maintainer of heca
    listing = jin.get("/api/shared-datasets").json()
    assert listing["datasets"][0]["can_publish"] is True and listing["source_roots"]
    started = jin.post("/api/shared-datasets/heca/publish", json=body)
    assert started.status_code == 202
    done = _wait(jin, "heca", started.json()["job"])
    assert done["state"] == "done" and done["result"]["label"] == "v2026.11"
    assert any("Iris_snRNA_allcells.h5ad" in line for line in done["lines"])
    assert (env["store"] / "heca" / "v2026.11" / "croissant.json").is_file()
    assert [v["label"] for v in bob.get("/api/shared-datasets").json()["datasets"][0]["versions"]] == ["v2026.11"]
    assert bob.get(f"/api/shared-datasets/heca/publish-jobs/{started.json()['job']}").status_code == 403
    # An unreadable source or a repeated label ends the job with the reason, and nothing half-published shows.
    bad = jin.post("/api/shared-datasets/heca/publish", json={**body, "source": "/etc"}).json()
    assert _wait(jin, "heca", bad["job"])["state"] == "error"
    again = _wait(jin, "heca", jin.post("/api/shared-datasets/heca/publish", json=body).json()["job"])
    assert again["state"] == "error" and "never replaced" in again["error"]
    for broken in ({**body, "label": "bad label!"}, {**body, "changelog": " "}, {**body, "source": ""}):
        assert jin.post("/api/shared-datasets/heca/publish", json=broken).status_code == 400


def test_a_maintainer_withdraws_a_version_and_it_is_no_longer_offered(env):
    _publish(env)
    (env["source"] / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v2 " * 1000)
    _publish(env, label="v2026.12", changelog="Lens updated.")
    bob, jin = _login(env, "bob"), _login(env, "jin")
    out = jin.post("/api/shared-datasets/heca/versions/v2026.12/withdraw")
    assert out.status_code == 200 and out.json()["latest"] == "v2026.11"
    versions = {v["label"]: v["status"] for v in bob.get("/api/shared-datasets").json()["datasets"][0]["versions"]}
    assert versions == {"v2026.12": "withdrawn", "v2026.11": "published"}
    assert jin.post("/api/shared-datasets/heca/versions/v2026.12/withdraw").status_code == 400
    assert jin.post("/api/shared-datasets/zzz/versions/v1/withdraw").status_code in (400, 403, 404)
