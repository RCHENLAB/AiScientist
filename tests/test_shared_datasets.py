"""Shared datasets (RCHENLAB/AiScientist issue 92): a dataset the lab maintains for every account.

- Publishing copies a prepared folder into the store while hashing it, freezes it (0444/0555), writes
  manifest.json, SHA256SUMS and CHANGELOG.md, and points ``latest`` at it.
- A later version hard-links the files it shares with the previous one.
- A label is never reused; a file that changes while it is published is refused; only admins and
  the dataset's maintainers publish; the source must sit inside an allowlisted server folder.
- Withdrawing keeps the files and moves ``latest``; the HPC3 path is offered only once verified.
- The read API serves every logged-in account and never shows a server path.
- ``aiscientist-admin shared-dataset`` drives the same code.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")
    shared = (tmp_path / "shared").resolve()
    store = (tmp_path / "store").resolve()
    shared.mkdir()
    monkeypatch.setenv("AISCIENTIST_SERVER_DATA_ROOTS", str(shared))
    monkeypatch.setenv("AISCIENTIST_SHARED_DATASETS_ROOT", str(store))

    from aiscientist.gateway import auth, auth_routes, db, models  # noqa: F401
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth)
    importlib.reload(auth_routes)
    db.reset(url)
    db.init_db()
    auth_routes.ensure_bootstrap_admin()
    from aiscientist.gateway import shared_datasets as sd, shared_routes
    importlib.reload(sd)
    importlib.reload(shared_routes)
    with db.session_scope() as s:
        for name in ("jin", "bob"):
            s.add(models.User(username=name, password_hash=auth.hash_password(f"{name}pass1"), role="user"))
        s.commit()
    sd.create_dataset("heca", "Human Eye Cell Atlas (HECA)", description="Eye atlas.")
    sd.set_maintainer("heca", "jin")
    source = shared / "jinl14" / "HECA"
    source.mkdir(parents=True)
    _write_h5ad(source / "Iris_snRNA_allcells.h5ad")
    (source / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v1 " * 1000)
    (source / "README.txt").write_text("Human Eye Cell Atlas\n")
    (source / ".DS_Store").write_bytes(b"junk")
    (source / "facets.json").write_text(json.dumps({"Iris_snRNA_allcells.h5ad": {"tissue": "iris"}}))
    yield {"sd": sd, "db": db, "models": models, "auth_routes": auth_routes, "shared_routes": shared_routes,
           "store": store, "source": source, "shared": shared, "tmp": tmp_path}
    _unfreeze(store)
    db.reset()


def _write_h5ad(path):
    h5py = pytest.importorskip("h5py")
    import numpy as np
    with h5py.File(path, "w") as f:
        f.create_dataset("X", data=np.zeros((3, 4), dtype="float32"))
        obs = f.create_group("obs")
        obs.create_dataset("_index", data=np.array([b"a", b"b", b"c"]))
        obs.create_dataset("celltype", data=np.array([0, 1, 1]))
        obs.create_dataset("donor", data=np.array([0, 0, 1]))
        f.create_group("var").create_dataset("_index", data=np.array([b"g1", b"g2", b"g3", b"g4"]))


def _unfreeze(folder):
    """Let pytest remove the read-only store."""
    if folder.exists():
        for dirpath, _d, _f in os.walk(folder):
            os.chmod(dirpath, 0o755)


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _publish(e, label="v2026.11", by="jin", **kw):
    return e["sd"].publish("heca", label, str(e["source"]), by=by, changelog=kw.pop("changelog", "First release."),
                           **kw)


def test_publish_copies_hashes_freezes_and_points_latest(env):
    result = _publish(env)
    folder = env["store"] / "heca" / "v2026.11"
    assert result["n_files"] == 3 and result["latest"] == "v2026.11" and result["linked"] == 0
    assert sorted(p.name for p in folder.iterdir()) == [
        "CHANGELOG.md", "Iris_snRNA_allcells.h5ad", "Lens_scRNA_allcells.h5ad", "README.txt", "SHA256SUMS",
        "croissant.json", "manifest.json"]                                      # no .DS_Store, no facets.json
    assert os.readlink(env["store"] / "heca" / "latest") == "v2026.11"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o555
    assert stat.S_IMODE((folder / "README.txt").stat().st_mode) == 0o444

    manifest = json.loads((folder / "manifest.json").read_text())
    files = {f["name"]: f for f in manifest["files"]}
    for name, f in files.items():
        assert f["sha256"] == _sha(env["source"] / name) == _sha(folder / name)
    assert manifest["version"] == "v2026.11" and manifest["published_by"] == "jin"
    assert manifest["previous_version"] is None
    iris = files["Iris_snRNA_allcells.h5ad"]["facets"]
    assert iris["tissue"] == "iris" and iris["modality"] == "snRNA"          # the maintainers' facet wins
    assert iris["n_obs"] == 3 and iris["n_vars"] == 4 and iris["obs_columns"] == ["celltype", "donor"]
    assert files["Lens_scRNA_allcells.h5ad"]["facets"] == {"tissue": "Lens", "modality": "scRNA"}
    assert "facets" not in files["README.txt"]
    sums = (folder / "SHA256SUMS").read_text().splitlines()
    assert f"{files['README.txt']['sha256']}  README.txt" in sums and len(sums) == 3
    assert (folder / "CHANGELOG.md").read_text() == "First release.\n"
    assert result["manifest_sha256"] == _sha(folder / "manifest.json")
    assert not any(p.name.endswith(".partial") for p in (env["store"] / "heca").iterdir())


def test_a_later_version_links_the_files_it_shares(env):
    _publish(env)
    (env["source"] / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v2 " * 1000)
    result = _publish(env, "v2026.12", changelog="Lens: new cell-type labels.")
    v1, v2 = env["store"] / "heca" / "v2026.11", env["store"] / "heca" / "v2026.12"
    assert result["linked"] == 2 and result["previous_version"] == "v2026.11"
    assert (v1 / "Iris_snRNA_allcells.h5ad").stat().st_ino == (v2 / "Iris_snRNA_allcells.h5ad").stat().st_ino
    assert (v1 / "Lens_scRNA_allcells.h5ad").stat().st_ino != (v2 / "Lens_scRNA_allcells.h5ad").stat().st_ino
    assert (v1 / "Lens_scRNA_allcells.h5ad").read_bytes().startswith(b"lens v1")      # v1 never changes
    assert os.readlink(env["store"] / "heca" / "latest") == "v2026.12"
    assert json.loads((v2 / "manifest.json").read_text())["previous_version"] == "v2026.11"


def test_a_label_is_never_reused(env):
    sd = env["sd"]
    _publish(env)
    with pytest.raises(sd.SharedDatasetError, match="already published"):
        _publish(env)


@pytest.mark.parametrize("case", ["not-maintainer", "no-changelog", "outside-allowlist", "symlink", "bad-label"])
def test_publish_refuses(env, case, tmp_path):
    sd = env["sd"]
    kw: dict = {}
    if case == "not-maintainer":
        kw["by"] = "bob"
    elif case == "no-changelog":
        kw["changelog"] = "  "
    elif case == "symlink":
        (env["source"] / "link.h5ad").symlink_to(env["source"] / "README.txt")
    if case == "outside-allowlist":
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "a.txt").write_text("x")
        with pytest.raises(sd.SharedDatasetError, match="outside the folders"):
            sd.publish("heca", "v1", str(outside), by="jin", changelog="x")
    elif case == "bad-label":
        with pytest.raises(sd.SharedDatasetError, match="Invalid version"):
            _publish(env, "latest")
    else:
        with pytest.raises(sd.SharedDatasetError):
            _publish(env, **kw)
    assert not (env["store"] / "heca" / "v2026.11").exists()


def test_the_admin_publishes_without_being_a_maintainer(env):
    assert _publish(env, by="root")["n_files"] == 3


def test_a_file_that_changes_while_published_is_refused_then_republished(env, monkeypatch):
    sd = env["sd"]
    real = sd._copy_hashing

    def copy_then_touch(src, dest):
        digest = real(src, dest)
        if src.name == "README.txt":
            src.write_text("edited during the copy\n")
        return digest

    monkeypatch.setattr(sd, "_copy_hashing", copy_then_touch)
    with pytest.raises(sd.SharedDatasetError, match="changed while"):
        _publish(env)
    heca = env["store"] / "heca"
    assert not (heca / "v2026.11").exists() and not (heca / ".v2026.11.partial").exists()
    assert sd.dataset_views()[0]["versions"] == []          # the 'publishing' row is not shown
    monkeypatch.setattr(sd, "_copy_hashing", real)
    assert _publish(env)["n_files"] == 3                    # the same label publishes once the folder is final


def test_withdraw_keeps_the_files_and_moves_latest(env):
    sd = env["sd"]
    _publish(env)
    _publish(env, "v2026.12", changelog="Second.")
    out = sd.withdraw("heca", "v2026.12", by="jin")
    assert out["latest"] == "v2026.11"
    assert os.readlink(env["store"] / "heca" / "latest") == "v2026.11"
    assert (env["store"] / "heca" / "v2026.12" / "manifest.json").is_file()
    assert sd.version_view("heca", "v2026.12")["version"]["status"] == "withdrawn"
    with pytest.raises(sd.SharedDatasetError, match="withdrawn"):
        sd.resolve("heca", "v2026.12")
    assert sd.resolve("heca", "v2026.12", allow_withdrawn=True)["label"] == "v2026.12"
    assert sd.resolve("heca")["label"] == "v2026.11"
    sd.withdraw("heca", "v2026.11", by="root")
    assert not (env["store"] / "heca" / "latest").is_symlink()
    with pytest.raises(sd.SharedDatasetError, match="no published version"):
        sd.resolve("heca")


def test_resolve_offers_the_hpc3_copy_only_once_verified(env):
    sd = env["sd"]
    _publish(env)
    root = "/dfs3b/ruic20_lab/software/AiScientist"
    info = sd.resolve("heca", hpc_shared_root=root)
    assert info["hpc_path"] is None
    assert info["server_path"] == str(env["store"] / "heca" / "v2026.11")
    sd.mark_hpc_verified("heca", "v2026.11")
    assert sd.resolve("heca", hpc_shared_root=root)["hpc_path"] == f"{root}/datasets/heca/v2026.11"
    assert sd.dataset_views()[0]["versions"][0]["on_hpc3"] is True


def test_verify_finds_a_damaged_file(env):
    sd = env["sd"]
    _publish(env)
    assert sd.verify("heca") == []
    target = env["store"] / "heca" / "v2026.11" / "README.txt"
    os.chmod(target.parent, 0o755)
    os.chmod(target, 0o644)
    target.write_text("Human Eye Cell Atlaz\n")
    assert sd.verify("heca") == ["README.txt"]


def _client(env):
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(env["auth_routes"].router)
    app.include_router(env["shared_routes"].router)
    app.include_router(env["shared_routes"].chat_router)
    return TestClient(app)


def test_every_account_reads_it_and_no_server_path_is_shown(env):
    _publish(env)
    client = _client(env)
    assert client.get("/api/shared-datasets").status_code == 401
    assert client.post("/api/auth/login", json={"username": "bob", "password": "bobpass1"}).status_code == 200

    listing = client.get("/api/shared-datasets")
    assert listing.status_code == 200
    body = listing.json()
    assert body["enabled"] is True
    heca = body["datasets"][0]
    assert heca["slug"] == "heca" and heca["latest"] == "v2026.11" and heca["maintainers"] == ["jin"]
    assert heca["can_publish"] is False

    detail = client.get("/api/shared-datasets/heca")
    assert detail.status_code == 200
    version = detail.json()["version"]
    assert version["label"] == "v2026.11" and version["published_by"] == "jin"
    assert version["changelog"] == "First release."
    assert [f["name"] for f in version["files"]] == ["Iris_snRNA_allcells.h5ad", "Lens_scRNA_allcells.h5ad",
                                                     "README.txt"]
    manifest = client.get("/api/shared-datasets/heca/versions/latest/manifest.json")
    assert manifest.status_code == 200 and manifest.json()["version"] == "v2026.11"
    for text in (listing.text, detail.text, manifest.text):
        assert str(env["store"]) not in text and str(env["shared"]) not in text

    assert client.get("/api/shared-datasets/nope").status_code == 404
    assert client.get("/api/shared-datasets/heca?version=v9").status_code == 404


def test_the_api_says_when_the_feature_is_off(env, monkeypatch):
    monkeypatch.delenv("AISCIENTIST_SHARED_DATASETS_ROOT")
    client = _client(env)
    client.post("/api/auth/login", json={"username": "bob", "password": "bobpass1"})
    assert client.get("/api/shared-datasets").json() == {"enabled": False, "datasets": []}


def test_deleting_an_account_keeps_what_it_published(env):
    _publish(env)
    client = _client(env)
    client.post("/api/auth/login", json={"username": "root", "password": "rootpass1"})
    models, db = env["models"], env["db"]
    with db.session_scope() as s:
        jin = s.query(models.User).filter_by(username="jin").one().id
    assert client.delete(f"/api/admin/users/{jin}").status_code == 200
    view = env["sd"].version_view("heca")
    assert view["version"]["published_by"] is None and env["sd"].maintainers("heca") == []
    assert (env["store"] / "heca" / "v2026.11" / "manifest.json").is_file()


def test_the_admin_cli_publishes_and_lists(env, capsys):
    from aiscientist.gateway import admin_cli
    changelog = env["tmp"] / "CHANGELOG-v1.md"
    changelog.write_text("First release through the CLI.\n")
    assert admin_cli.main(["shared-dataset", "create", "retina", "--name", "Retina atlas"]) == 0
    assert admin_cli.main(["shared-dataset", "maintainer", "retina", "add", "bob"]) == 0
    assert admin_cli.main(["shared-dataset", "publish", "retina", "v1", "--from", str(env["source"]),
                           "--changelog", str(changelog), "--by", "bob"]) == 0
    assert admin_cli.main(["shared-dataset", "publish", "retina", "v1", "--from", str(env["source"]),
                           "--changelog", str(changelog), "--by", "bob"]) == 1      # never reused
    assert admin_cli.main(["shared-dataset", "verify", "retina"]) == 0
    assert admin_cli.main(["shared-dataset", "hpc-verified", "retina", "v1"]) == 0
    capsys.readouterr()
    assert admin_cli.main(["shared-dataset", "list"]) == 0
    out = capsys.readouterr().out
    assert "retina: Retina atlas  latest=v1  maintainers=bob" in out and "on HPC3" in out
    assert "heca: Human Eye Cell Atlas (HECA)  latest=-" in out


# --- attaching to a chat (issue 100) --------------------------------------------------------------


def _chat(env, client, user="bob"):
    assert client.post("/api/auth/login", json={"username": user, "password": f"{user}pass1"}).status_code == 200
    return client.post("/api/conversations", json={"title": "Eye"}).json()["conversation"]["id"]


def test_attach_whole_or_one_file_latest_or_pinned_and_reload_keeps_it(env):
    _publish(env)
    client = _client(env)
    cid = _chat(env, client)

    def put(items):
        return client.put(f"/api/conversations/{cid}/shared-datasets", json={"shared_datasets": items})

    got = put([{"shared": "heca"}])
    assert got.status_code == 200
    first = got.json()["shared_datasets"][0]
    assert (first["requested"], first["version"], first["files"], first["role"]) == (
        "latest", "v2026.11", None, "reference")
    assert first["n_files"] == 3
    pinned = put([{"shared": "heca", "version": "v2026.11", "files": ["Lens_scRNA_allcells.h5ad"],
                   "role": "primary"}]).json()["shared_datasets"][0]
    assert (pinned["requested"], pinned["files"], pinned["role"], pinned["n_files"]) == (
        "v2026.11", ["Lens_scRNA_allcells.h5ad"], "primary", 1)
    reloaded = client.get(f"/api/conversations/{cid}").json()
    assert reloaded["shared_datasets"] == [pinned]
    assert str(env["store"]) not in json.dumps(reloaded)


def test_attach_is_refused_for_unknown_file_version_role_dataset_and_other_chats(env):
    _publish(env)
    client = _client(env)
    cid = _chat(env, client)
    url = f"/api/conversations/{cid}/shared-datasets"
    for item in ({"shared": "heca", "files": ["nope.h5ad"]}, {"shared": "heca", "files": []},
                 {"shared": "heca", "version": "v9"}, {"shared": "heca", "role": "boss"}, {"shared": "zzz"}):
        assert client.put(url, json={"shared_datasets": [item]}).status_code == 400, item
    other = _client(env)
    assert other.post("/api/auth/login", json={"username": "jin", "password": "jinpass1"}).status_code == 200
    assert other.put(url, json={"shared_datasets": [{"shared": "heca"}]}).status_code == 404


def test_a_whole_version_counts_once_toward_the_limit(env):
    _publish(env)
    client = _client(env)
    cid = _chat(env, client)
    models = env["models"]
    from aiscientist.gateway.dataset_limits import MAX_BOUND_DATASETS
    with env["db"].session_scope() as s:
        uid = s.query(models.User).filter_by(username="bob").one().id
        for i in range(MAX_BOUND_DATASETS):
            d = models.Dataset(user_id=uid, name=f"d{i}", path=f"/x/d{i}", kind="file", size_bytes=1)
            s.add(d)
            s.flush()
            s.add(models.ConversationDataset(conversation_id=cid, dataset_id=d.id, position=i))
        s.commit()
    full = client.put(f"/api/conversations/{cid}/shared-datasets", json={"shared_datasets": [{"shared": "heca"}]})
    assert full.status_code == 400
    with env["db"].session_scope() as s:
        s.query(models.ConversationDataset).filter_by(conversation_id=cid, position=0).delete()
        s.commit()
    ok = client.put(f"/api/conversations/{cid}/shared-datasets", json={"shared_datasets": [{"shared": "heca"}]})
    assert ok.status_code == 200 and ok.json()["shared_datasets"][0]["n_files"] == 3   # 3 files, one slot


def test_withdrawn_version_cannot_be_newly_attached_but_a_pinned_chat_keeps_it(env):
    _publish(env)
    (env["source"] / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v2 " * 1000)
    _publish(env, label="v2026.12", changelog="Lens updated.")
    client = _client(env)
    cid = _chat(env, client)
    url = f"/api/conversations/{cid}/shared-datasets"
    assert client.put(url, json={"shared_datasets": [{"shared": "heca", "version": "v2026.11"}]}).status_code == 200
    env["sd"].withdraw("heca", "v2026.11", by="jin")
    again = client.put(url, json={"shared_datasets": [{"shared": "heca", "version": "v2026.11",
                                                       "role": "primary"}]})
    assert again.status_code == 200                                   # already pinned: kept
    kept = again.json()["shared_datasets"][0]
    assert kept["withdrawn"] is True and kept["version"] == "v2026.11"
    cid2 = _chat(env, client)
    refused = client.put(f"/api/conversations/{cid2}/shared-datasets",
                         json={"shared_datasets": [{"shared": "heca", "version": "v2026.11"}]})
    assert refused.status_code == 400 and "withdrawn" in refused.json()["detail"]


def test_following_latest_shows_the_newer_version_after_a_release(env):
    _publish(env)
    client = _client(env)
    cid = _chat(env, client)
    client.put(f"/api/conversations/{cid}/shared-datasets", json={"shared_datasets": [{"shared": "heca"}]})
    (env["source"] / "Lens_scRNA_allcells.h5ad").write_bytes(b"lens v2 " * 1000)
    _publish(env, label="v2026.12", changelog="Lens updated.")
    view = client.get(f"/api/conversations/{cid}").json()["shared_datasets"][0]
    assert (view["requested"], view["version"]) == ("latest", "v2026.12")


def test_a_shared_file_is_previewed_and_downloaded_only_if_the_version_lists_it(env, monkeypatch):
    pytest.importorskip("httpx")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from aiscientist.gateway import app as gw_app
    _publish(env)
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    user = {"u": object()}
    monkeypatch.setattr(gw_app, "_optional_user", lambda request: user["u"])
    client = TestClient(gw_app.app)
    base = "/api/shared-datasets/heca/versions/v2026.11"
    pv = client.get(f"{base}/preview", params={"file": "Iris_snRNA_allcells.h5ad"})
    assert pv.status_code == 200
    body = pv.json()
    assert body["file"]["name"] == "Iris_snRNA_allcells.h5ad" and body["preview"]["type"] == "h5ad"
    assert str(env["store"]) not in json.dumps(body) and "id" not in body["dataset"]
    assert client.get(f"{base}/preview", params={"file": "../../etc/passwd"}).status_code == 404
    assert client.get(f"{base}/preview", params={"file": "manifest.json"}).status_code == 404   # not a listed file
    assert client.get("/api/shared-datasets/heca/versions/v9/preview", params={"file": "README.txt"}).status_code == 404
    dl = client.get(f"{base}/download", params={"file": "README.txt"})
    assert dl.status_code == 200 and dl.content == b"Human Eye Cell Atlas\n"
    user["u"] = None
    assert client.get(f"{base}/preview", params={"file": "README.txt"}).status_code == 401
