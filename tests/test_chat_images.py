"""Images in the chat (RCHENLAB/AiScientist issue 115): kept under the account, shown with the message
after a reload, carried to a model that accepts images, and refused out loud when it does not.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("bcrypt")
pytest.importorskip("itsdangerous")

from aiscientist.gateway import chat_images as ci  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64


# --- the store ---------------------------------------------------------------------------------------


def test_the_bytes_decide_what_an_image_is(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path))
    assert [ci.sniff(b) for b in (PNG, JPG, GIF, WEBP)] == ["png", "jpg", "gif", "webp"]
    for fake in (b"<html><script>alert(1)</script></html>", b"<svg onload=alert(1)>", b"%PDF-1.7", b"MZ\x90\x00", b""):
        assert ci.sniff(fake) is None
    with pytest.raises(ci.ChatImageError, match="Only PNG"):
        ci.save("alice", 1, b"<svg onload=alert(1)></svg>", "x.png")          # a name does not make it an image
    with pytest.raises(ci.ChatImageError, match="empty"):
        ci.save("alice", 1, b"", "x.png")
    monkeypatch.setattr(ci, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(ci.ChatImageError, match="limit"):
        ci.save("alice", 1, PNG + b"x" * 100, "big.png")


def test_an_image_is_kept_under_the_account_and_only_there(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path))
    ref = ci.save("alice", 7, PNG, "../../etc/passwd <b>.png")
    assert ref["mime"] == "image/png" and ref["size"] == len(PNG)
    assert "/" not in ref["name"] and "<" not in ref["name"]
    assert ref["url"] == f"/api/chat-images/7/{ref['id']}.png"
    path, media = ci.path_of("alice", 7, f"{ref['id']}.png")
    assert media == "image/png" and path.read_bytes() == PNG and path.is_relative_to(tmp_path / "alice" / "chat-images" / "7")
    # Another account, another chat, a made-up or traversing name: nothing.
    assert ci.path_of("bob", 7, f"{ref['id']}.png") is None
    assert ci.path_of("alice", 8, f"{ref['id']}.png") is None
    for bad in ("../x.png", f"{ref['id']}.svg", f"{ref['id']}.png/../../x", "0123456789abcdef.png", ""):
        assert ci.path_of("alice", 7, bad) is None, bad
    assert ci.remove_conversation("alice", 7) == 2 and ci.path_of("alice", 7, f"{ref['id']}.png") is None


def test_a_message_names_at_most_five_of_this_chats_own_images(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path))
    refs = [ci.save("alice", 3, PNG, f"{i}.png") for i in range(6)]
    got = ci.resolve("alice", 3, [{"id": r["id"], "url": "/etc/passwd", "name": "evil"} for r in refs[:5]])
    assert [g["id"] for g in got] == [r["id"] for r in refs[:5]] and all(g["url"].startswith("/api/chat-images/3/") for g in got)
    assert got[0]["name"] == "0.png"                          # the server's name, not the one the page sent
    with pytest.raises(ci.ChatImageError, match="At most 5"):
        ci.resolve("alice", 3, [r["id"] for r in refs])
    with pytest.raises(ci.ChatImageError, match="no longer available"):
        ci.resolve("bob", 3, [refs[0]["id"]])                 # someone else's image
    with pytest.raises(ci.ChatImageError, match="no longer available"):
        ci.resolve("alice", 4, [refs[0]["id"]])               # another chat's image
    with pytest.raises(ci.ChatImageError):
        ci.resolve("alice", 3, ["../../x"])
    loaded = ci.load("alice", 3, [refs[1]["id"]])
    assert loaded[0]["data"] == PNG and loaded[0]["media_type"] == "image/png"
    assert ci.data_url(loaded[0]).startswith("data:image/png;base64,iVBORw0KGgo")


def test_the_limit_is_what_the_claude_api_takes_once_base64_encoded():
    import base64
    assert ci.MAX_IMAGE_BYTES == 7_500_000
    assert len(base64.b64encode(b"\0" * ci.MAX_IMAGE_BYTES)) <= 10_000_000      # 10 MB per image, as base64
    assert ci.mb(ci.MAX_IMAGE_BYTES) == "7.5 MB" and ci.mb(ci.MAX_ACCOUNT_BYTES) == "200 MB"


def test_one_image_can_be_removed_and_only_that_one(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path))
    a, b = ci.save("alice", 3, PNG, "a.png"), ci.save("alice", 3, JPG, "b.jpg")
    assert not ci.remove("bob", 3, a["id"]) and not ci.remove("alice", 4, a["id"])
    assert not ci.remove("alice", 3, "../../x") and not ci.remove("alice", 3, "0123456789abcdef")
    assert ci.remove("alice", 3, a["id"]) and not ci.remove("alice", 3, a["id"])
    assert ci.path_of("alice", 3, f"{a['id']}.png") is None and ci.path_of("alice", 3, f"{b['id']}.jpg") is not None
    assert sorted(p.name for p in (tmp_path / "alice" / "chat-images" / "3").iterdir()) == [f"{b['id']}.jpg", f"{b['id']}.json"]


def test_images_no_message_names_go_a_day_after_their_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path))
    old_sent, old_unsent, new = (ci.save("alice", 3, PNG, n) for n in ("sent.png", "unsent.png", "new.png"))
    other_chat = ci.save("alice", 4, PNG, "other.png")
    now = ci.time.time() + ci.UNSENT_TTL_S + 60
    sidecar = tmp_path / "alice" / "chat-images" / "3" / f"{new['id']}.json"
    meta = json.loads(sidecar.read_text())
    meta["created"] = now - 60                                 # uploaded a minute before ``now``
    sidecar.write_text(json.dumps(meta))
    stale = ci.unsent("alice", now=now)
    assert sorted(stale[3]) == sorted([old_sent["id"], old_unsent["id"]]) and stale[4] == [other_chat["id"]]
    # a name in another chat does not count: other.png goes with unsent.png
    assert ci.sweep("alice", {(3, old_sent["id"]), (3, other_chat["id"])}, now=now) == 2
    kept = {r["id"] for cid, r in ((3, old_sent), (3, old_unsent), (3, new), (4, other_chat))
            if ci.path_of("alice", cid, r["url"].rsplit("/", 1)[1])}
    assert kept == {old_sent["id"], new["id"]}
    assert ci.sweep("bob", set(), now=now) == 0 and ci.sweep("alice", set()) == 0       # nothing is a day old yet
    assert ci.named_ids(['{"images": [{"id": "a"}, "b"]}', {"images": [{"id": "c"}]}, "not json", None,
                         '{"items": []}']) == {"a", "b", "c"}


def test_an_account_keeps_at_most_its_quota_of_images(tmp_path, monkeypatch):
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(ci, "MAX_ACCOUNT_BYTES", len(PNG) * 2 + 10)
    first = ci.save("alice", 1, PNG)
    ci.save("alice", 2, PNG)
    assert ci.usage("alice") == len(PNG) * 2
    with pytest.raises(ci.ChatImageQuotaError, match="at most"):
        ci.save("alice", 1, PNG)
    ci.save("bob", 1, PNG)                                     # another account has its own
    assert ci.remove("alice", 1, first["id"])
    ci.save("alice", 1, PNG)                                   # room again


def test_which_models_read_images():
    assert ci.accepts_images("claude-sonnet-5-5", native_anthropic=True)
    assert ci.accepts_images("anything", native_anthropic=True)
    for name in ("gpt-4o-mini", "gpt-4.1", "gpt-5", "gemini-2.5-pro", "qwen2.5-vl-72b", "Qwen/Qwen3-VL-8B", "llava-1.6",
                 "pixtral-12b", "meta/llama-3.2-90b-vision", "gemma-3-27b-it"):
        assert ci.accepts_images(name), name
    for name in ("gpt-3.5-turbo", "deepseek-v4.1-flash", "qwen/qwen3.6-35b-a3b", "llama-3.1-70b", "mistral-7b", ""):
        assert not ci.accepts_images(name), name
    assert ci.accepts_images("deepseek-x", env={"AISCIENTIST_VISION_MODELS": "deepseek"})
    assert not ci.accepts_images("gpt-4o", env={"AISCIENTIST_VISION_MODELS": "none"})
    assert ci.accepts_images("anything", env={"AISCIENTIST_VISION_MODELS": "all"})
    assert not ci.accepts_images("gpt-4o", env={"AISCIENTIST_VISION_MODELS": "claude,qwen"})


# --- the routes --------------------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("AISCIENTIST_DATABASE_URL", url)
    monkeypatch.setenv("AISCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AISCIENTIST_ADMIN_USER", "root")
    monkeypatch.setenv("AISCIENTIST_ADMIN_PASSWORD", "rootpass1")
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path / "results"))
    from aiscientist.gateway import auth, auth_routes, db, models
    importlib.reload(db)
    importlib.reload(models)
    importlib.reload(auth)
    importlib.reload(auth_routes)
    db.reset(url)
    db.init_db()
    auth_routes.ensure_bootstrap_admin()
    with db.session_scope() as s:
        for name in ("alice", "bob"):
            s.add(models.User(username=name, password_hash=auth.hash_password(f"{name}pass1"), role="user"))
        s.commit()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth_routes.router)

    def client(user=None):
        c = TestClient(app)
        if user:
            assert c.post("/api/auth/login", json={"username": user, "password": f"{user}pass1"}).status_code == 200
        return c
    yield SimpleNamespace(client=client, results=tmp_path / "results")
    db.reset()


def _chat(c):
    return c.post("/api/conversations", json={"title": "pics"}).json()["conversation"]["id"]


def _upload(c, cid, data, name="shot.png", ctype="image/png"):
    return c.post(f"/api/conversations/{cid}/images", params={"name": name}, content=data, headers={"Content-Type": ctype})


def test_upload_serve_and_who_may_see_it(api):
    alice, bob, anon = api.client("alice"), api.client("bob"), api.client()
    cid = _chat(alice)
    up = _upload(alice, cid, PNG)
    assert up.status_code == 200
    ref = up.json()["image"]
    got = alice.get(ref["url"])
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff" and "sandbox" in got.headers["content-security-policy"]
    assert bob.get(ref["url"]).status_code == 404 and anon.get(ref["url"]).status_code == 401
    assert _upload(bob, cid, PNG).status_code == 404                      # not bob's chat
    assert _upload(anon, cid, PNG).status_code == 401
    for data, ctype in ((JPG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")):
        assert _upload(alice, cid, data, ctype=ctype).status_code == 200


def test_only_real_images_within_the_limit_are_kept(api, monkeypatch):
    alice = api.client("alice")
    cid = _chat(alice)
    bad = _upload(alice, cid, b"<html><script>alert(1)</script></html>", "x.png", "image/png")
    assert bad.status_code == 400 and "PNG, JPEG, WebP and GIF" in bad.json()["detail"]
    assert _upload(alice, cid, b"<svg onload=alert(1)></svg>", "x.svg", "image/svg+xml").status_code == 400
    monkeypatch.setattr(ci, "MAX_IMAGE_BYTES", 200)
    assert _upload(alice, cid, PNG + b"x" * 500).status_code == 413
    assert _upload(alice, cid, b"").status_code == 400
    assert not any(api.results.rglob("*.png"))                            # nothing was kept


def test_a_messages_images_survive_a_reload_and_the_server_keeps_its_own_references(api):
    alice = api.client("alice")
    cid = _chat(alice)
    refs = [_upload(alice, cid, PNG, f"s{i}.png").json()["image"] for i in range(2)]
    sent = alice.post(f"/api/conversations/{cid}/messages", json={
        "role": "user", "content": "what is this?",
        "meta": {"images": [{"id": refs[0]["id"], "url": "/etc/passwd", "name": "<b>evil</b>"}, {"id": refs[1]["id"]}]}})
    assert sent.status_code == 200
    kept = sent.json()["message"]["meta"]["images"]
    assert [k["id"] for k in kept] == [r["id"] for r in refs]
    assert kept[0]["url"] == refs[0]["url"] and kept[0]["name"] == "s0.png"
    reloaded = alice.get(f"/api/conversations/{cid}").json()["messages"]
    assert reloaded[-1]["content"] == "what is this?" and reloaded[-1]["meta"]["images"] == kept
    assert alice.get(kept[0]["url"]).content == PNG                       # and the image is still there


def test_a_message_cannot_name_another_chats_or_accounts_images_or_more_than_five(api):
    alice, bob = api.client("alice"), api.client("bob")
    a, b = _chat(alice), _chat(bob)
    mine = [_upload(alice, a, PNG).json()["image"] for _ in range(6)]
    theirs = _upload(bob, b, PNG).json()["image"]

    def post(c, cid, ids):
        return c.post(f"/api/conversations/{cid}/messages",
                      json={"role": "user", "content": "x", "meta": {"images": [{"id": i} for i in ids]}})
    assert post(alice, a, [theirs["id"]]).status_code == 400
    assert post(alice, a, [r["id"] for r in mine]).status_code == 400
    assert post(alice, a, ["nope"]).status_code == 400
    assert post(alice, a, [r["id"] for r in mine[:5]]).status_code == 200
    other_chat = _chat(alice)
    assert post(alice, other_chat, [mine[0]["id"]]).status_code == 400


def test_deleting_a_chat_deletes_its_images(api):
    alice = api.client("alice")
    cid = _chat(alice)
    ref = _upload(alice, cid, PNG).json()["image"]
    assert any(api.results.rglob("*.png"))
    assert alice.delete(f"/api/conversations/{cid}").status_code == 200
    assert not any(api.results.rglob("*.png")) and not any(api.results.rglob("*.json"))
    assert alice.get(ref["url"]).status_code == 404


def test_an_image_taken_off_the_message_is_deleted_but_a_sent_one_stays(api):
    alice, bob, anon = api.client("alice"), api.client("bob"), api.client()
    cid = _chat(alice)
    unsent, sent = (_upload(alice, cid, PNG, n).json()["image"] for n in ("unsent.png", "sent.png"))
    assert alice.post(f"/api/conversations/{cid}/messages", json={
        "role": "user", "content": "look", "meta": {"images": [{"id": sent["id"]}]}}).status_code == 200
    assert bob.delete(f"/api/conversations/{cid}/images/{unsent['id']}").status_code == 404     # not bob's chat
    assert anon.delete(f"/api/conversations/{cid}/images/{unsent['id']}").status_code == 401
    kept = alice.delete(f"/api/conversations/{cid}/images/{sent['id']}")
    assert kept.status_code == 409 and "sent message" in kept.json()["detail"]
    assert alice.get(sent["url"]).status_code == 200
    assert alice.delete(f"/api/conversations/{cid}/images/{unsent['id']}").status_code == 200
    assert alice.get(unsent["url"]).status_code == 404
    assert alice.delete(f"/api/conversations/{cid}/images/{unsent['id']}").status_code == 404
    assert alice.delete(f"/api/conversations/{cid}/images/..%2F..%2Fx").status_code in (404, 405)


def test_an_upload_first_clears_the_accounts_day_old_unsent_images(api, monkeypatch):
    alice = api.client("alice")
    a, b = _chat(alice), _chat(alice)
    sent, unsent, other = (_upload(alice, cid, PNG, n).json()["image"] for cid, n in ((a, "s.png"), (a, "u.png"), (b, "o.png")))
    assert alice.post(f"/api/conversations/{a}/messages", json={
        "role": "user", "content": "look", "meta": {"images": [{"id": sent["id"]}]}}).status_code == 200
    _upload(alice, b, PNG, "fresh.png")                        # nothing is a day old: all kept
    assert all(alice.get(r["url"]).status_code == 200 for r in (sent, unsent, other))
    later = ci.time.time() + ci.UNSENT_TTL_S + 60
    monkeypatch.setattr(ci.time, "time", lambda: later)
    assert _upload(alice, b, PNG, "next.png").status_code == 200
    assert alice.get(sent["url"]).status_code == 200            # a sent message names it
    assert alice.get(unsent["url"]).status_code == 404 and alice.get(other["url"]).status_code == 404


def test_an_upload_over_the_accounts_quota_is_refused_and_says_so(api, monkeypatch):
    alice, bob = api.client("alice"), api.client("bob")
    cid = _chat(alice)
    monkeypatch.setattr(ci, "MAX_ACCOUNT_BYTES", len(PNG) * 2)
    assert _upload(alice, cid, PNG).status_code == 200 and _upload(alice, cid, PNG).status_code == 200
    full = _upload(alice, cid, PNG)
    assert full.status_code == 413 and "an account keeps at most" in full.json()["detail"]
    assert _upload(bob, _chat(bob), PNG).status_code == 200    # bob's own room


# --- the model path ----------------------------------------------------------------------------------


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    monkeypatch.setenv("AISCIENTIST_RESULTS_DIR", str(tmp_path))
    from aiscientist.gateway import app as gw_app
    monkeypatch.setattr(gw_app, "_AUTH_ENABLED", True)
    monkeypatch.setattr(gw_app, "_bound_credential", lambda conn: None)
    return gw_app


def _conn(model="gpt-4o"):
    return SimpleNamespace(owner="alice", selected_model=model)


def _req(gw, ids, **kw):
    return gw.LabRequest(connection_id="c", question="what is in this screenshot?", conversation_id="5", images=ids, **kw)


def test_a_session_that_reads_images_sends_them_to_the_fast_chat(gw, tmp_path):
    ref = ci.save("alice", 5, PNG, "err.png")
    req = _req(gw, [ref["id"]])
    assert gw._prepare_images(_conn("gpt-4o"), req) is None
    assert req.route == "chat" and [i["name"] for i in req._images] == ["err.png"] and req._images[0]["data"] == PNG
    assert gw._prepare_images(_conn(), gw.LabRequest(connection_id="c", question="hi")) is None     # no images: untouched


def test_a_session_that_cannot_read_images_refuses_and_says_so_instead_of_dropping_them(gw):
    ref = ci.save("alice", 5, PNG)
    refused = gw._prepare_images(_conn("deepseek-v4.1-flash"), _req(gw, [ref["id"]]))
    body = json.loads(refused.body)
    assert refused.status_code == 409 and body["images_unsupported"] is True
    assert "deepseek-v4.1-flash" in body["error"] and "cannot read images" in body["error"] and "was not sent" in body["error"]


def test_images_with_data_attached_are_refused_because_an_analysis_reads_no_images(gw):
    ref = ci.save("alice", 5, PNG)
    req = _req(gw, [ref["id"]], datasets=[{"path": "/data/a.h5ad"}])
    refused = gw._prepare_images(_conn("gpt-4o"), req)
    assert refused.status_code == 409 and "not images" in json.loads(refused.body)["error"]
    assert gw._prepare_images(_conn("gpt-4o"), _req(gw, [ref["id"]], shared_datasets=[{"shared": "heca"}])).status_code == 409


def test_images_of_another_chat_or_account_are_refused(gw):
    ref = ci.save("bob", 5, PNG)
    refused = gw._prepare_images(_conn("gpt-4o"), _req(gw, [ref["id"]]))
    assert refused.status_code == 400 and "no longer available" in json.loads(refused.body)["error"]
    no_chat = gw.LabRequest(connection_id="c", question="x", images=[ref["id"]])
    assert gw._prepare_images(_conn("gpt-4o"), no_chat).status_code == 400


def test_the_images_ride_on_the_question_message_only():
    from aiscientist.agents import quick_chat
    seen = []

    def stream(messages, schemas):
        seen.append([dict(m) for m in messages])
        yield "content", "It is a plot."

    parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    out = quick_chat.run_quick_chat(stream_fn=stream, tools=[], question="describe it",
                                    history=[{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}],
                                    image_parts=parts)
    assert out.text == "It is a plot."
    last = seen[0][-1]
    assert last["role"] == "user" and last["content"][0] == {"type": "text", "text": "describe it"} and last["content"][1:] == parts
    assert all(isinstance(m["content"], str) for m in seen[0][:-1])           # history stays text
    plain = []
    quick_chat.run_quick_chat(stream_fn=lambda m, s: (plain.append(m), iter([("content", "x")]))[1], tools=[], question="hi")
    assert isinstance(plain[0][-1]["content"], str)


def test_an_anthropic_session_gets_native_image_blocks_and_never_fetches_an_address():
    from aiscientist.gateway import anthropic_client
    messages = [{"role": "system", "content": "be brief"},
                {"role": "user", "content": [{"type": "text", "text": "what is this?"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                                             {"type": "image_url", "image_url": {"url": "https://evil.example/x.png"}},
                                             {"type": "image_url", "image_url": {"url": "data:text/html;base64,AAAA"}}]}]
    system, msgs = anthropic_client.to_anthropic(messages)
    blocks = msgs[0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1:] == [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]
