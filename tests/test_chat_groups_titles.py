"""Chat groups in the sidebar and titles named by intent — SQLite, offline.

Groups: create/rename/delete, filing a chat, per-user ownership, and that deleting a group keeps its
chats. Titles: who may replace a title (the page's own, the model's, the user's), and the cleaning
of what a model replies.
"""

from __future__ import annotations

import pytest

from tests.test_chat_history import _login, app_ctx  # noqa: F401 - the fixture


def _chat(client, title="New chat"):
    return client.post("/api/conversations", json={"title": title}).json()["conversation"]


def test_groups_file_chats_and_deleting_a_group_keeps_them(app_ctx):  # noqa: F811
    client = app_ctx
    _login(client, "root", "rootpass1")
    a, b = _chat(client)["id"], _chat(client)["id"]
    assert client.get("/api/conversations").json()["groups"] == []
    assert _chat(client)["group_id"] is None

    gid = client.post("/api/conversation-groups", json={"name": "  DDX41   project "}).json()["group"]["id"]
    other = client.post("/api/conversation-groups", json={"name": "Variants"}).json()["group"]["id"]
    assert client.post("/api/conversation-groups", json={"name": "   "}).status_code == 400

    moved = client.patch(f"/api/conversations/{a}", json={"group_id": gid}).json()["conversation"]
    assert moved["group_id"] == gid
    # A patch that does not name the group leaves it alone.
    assert client.patch(f"/api/conversations/{a}", json={"preset_key": "x"}).json()["conversation"]["group_id"] == gid
    assert client.patch(f"/api/conversations/{b}", json={"group_id": 9999}).status_code == 404

    listing = client.get("/api/conversations").json()
    assert [g["name"] for g in listing["groups"]] == ["DDX41 project", "Variants"]
    assert {c["id"]: c["group_id"] for c in listing["conversations"]}[a] == gid

    assert client.patch(f"/api/conversation-groups/{gid}", json={"name": "Ddx41"}).json()["group"]["name"] == "Ddx41"
    assert client.delete(f"/api/conversation-groups/{gid}").json() == {"status": "ok", "ungrouped": 1}
    listing = client.get("/api/conversations").json()
    assert [g["id"] for g in listing["groups"]] == [other]
    assert {c["id"]: c["group_id"] for c in listing["conversations"]}[a] is None      # the chat is kept
    # null takes a chat out of its group.
    client.patch(f"/api/conversations/{b}", json={"group_id": other})
    assert client.patch(f"/api/conversations/{b}", json={"group_id": None}).json()["conversation"]["group_id"] is None


def test_a_group_belongs_to_one_user(app_ctx):  # noqa: F811
    client = app_ctx
    _login(client, "root", "rootpass1")
    client.post("/api/admin/users", json={"username": "mallory", "password": "mallory1"})
    gid = client.post("/api/conversation-groups", json={"name": "Mine"}).json()["group"]["id"]
    client.post("/api/auth/logout")
    _login(client, "mallory", "mallory1")
    theirs = _chat(client)["id"]
    assert client.get("/api/conversations").json()["groups"] == []
    assert client.patch(f"/api/conversations/{theirs}", json={"group_id": gid}).status_code == 404
    assert client.patch(f"/api/conversation-groups/{gid}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/conversation-groups/{gid}").status_code == 404
    client.post("/api/auth/logout")
    assert client.post("/api/conversation-groups", json={"name": "x"}).status_code == 401


def test_who_may_replace_a_title(app_ctx):  # noqa: F811
    from aiscientist.gateway import chat_titles
    from aiscientist.gateway.db import session_scope
    from aiscientist.gateway.models import User

    client = app_ctx
    _login(client, "root", "rootpass1")
    with session_scope() as s:
        uid = s.query(User).filter_by(username="root").one().id
    cid = _chat(client)["id"]
    assert _chat(client)["title_source"] == "prompt"

    # The page's own title, then the model's reading of the request, with the data's name in front.
    client.patch(f"/api/conversations/{cid}", json={"title": "Run VEP and ClinVar on the variants in the", "title_source": "prompt"})
    assert chat_titles.wants_title(uid, cid)
    assert chat_titles.store(uid, cid, "Variant annotation with SpliceAI", ["/data/demo_variants.vcf.gz", "b.vcf"]) \
        == "demo_variants +1 · Variant annotation with SpliceAI"
    conv = client.get(f"/api/conversations/{cid}").json()["conversation"]
    assert conv["title_source"] == "intent"
    # Named once: neither a second reading nor a late title from the page replaces it.
    assert not chat_titles.wants_title(uid, cid)
    assert chat_titles.store(uid, cid, "Something else", []) == ""
    late = client.patch(f"/api/conversations/{cid}", json={"title": "Run VEP and", "title_source": "prompt"}).json()
    assert late["conversation"]["title"] == conv["title"]
    # The user's own title wins over both and stays.
    typed = client.patch(f"/api/conversations/{cid}", json={"title": "My VCF", "title_source": "user"}).json()
    assert typed["conversation"]["title"] == "My VCF" and typed["conversation"]["title_source"] == "user"
    assert chat_titles.store(uid, cid, "Variant annotation", []) == ""

    # Another user's chat, and a chat that does not exist, are not named.
    assert chat_titles.store(uid + 1000, cid, "x y", []) == "" and chat_titles.store(uid, 987654, "x y", []) == ""
    assert not chat_titles.wants_title(uid, 987654)

    # The same title twice in one account is told apart by the chat's time.
    one, two = _chat(client)["id"], _chat(client)["id"]
    assert chat_titles.store(uid, one, "QC of two samples", []) == "QC of two samples"
    second = chat_titles.store(uid, two, "QC of two samples", [])
    assert second.startswith("QC of two samples · ") and second != "QC of two samples"

    # Past its first request a chat keeps the title it has.
    old = _chat(client)["id"]
    for text in ("first request", "yes, go on"):
        client.post(f"/api/conversations/{old}/messages", json={"role": "user", "content": text})
    assert not chat_titles.wants_title(uid, old)


@pytest.mark.parametrize("raw, title", [
    ("Variant annotation with SpliceAI", "Variant annotation with SpliceAI"),
    ('"CellQC on two samples."', "CellQC on two samples"),
    ("Title: **DE between mutant and WT**\n\nBecause the request asks…", "DE between mutant and WT"),
    ("标题：两个样本的质量控制。", "两个样本的质量控制"),
    ("one two three four five six seven eight nine ten", "one two three four five six seven eight"),
    ("", ""), ("   \n ", ""),
    (" ".join(["word"] * 40), ""),          # the model answered the request instead of naming it
])
def test_clean_title(raw, title):
    from aiscientist.gateway.chat_titles import clean_title
    assert clean_title(raw) == title


def test_intent_title_asks_once_and_survives_a_failing_model():
    from aiscientist.gateway.chat_titles import intent_title
    seen = []

    def complete(messages):
        seen.append(messages)
        return "Variant annotation\n"

    assert intent_title(complete, "  Run VEP\non the VCF ") == "Variant annotation"
    assert seen[0][1] == {"role": "user", "content": "Run VEP on the VCF"}
    assert intent_title(None, "Run VEP") == "" and intent_title(complete, "  ") == ""

    def broken(_messages):
        raise RuntimeError("model down")

    assert intent_title(broken, "Run VEP") == ""


def test_dataset_stem():
    from aiscientist.gateway.chat_titles import dataset_stem
    assert dataset_stem([]) == "" and dataset_stem(["", " "]) == ""
    assert dataset_stem(["/x/Ddx41_DEG1.h5ad"]) == "Ddx41_DEG1"
    assert dataset_stem(["sample.vcf.gz", "b", "c"]) == "sample +2"
    assert dataset_stem(["/data/Sample1_WT/"]) == "Sample1_WT"


def test_a_request_names_its_chat_and_tells_the_page(app_ctx, monkeypatch):  # noqa: F811
    """The gateway's own step: the session model's reading is stored and pushed as `chat_title`;
    with no model up, or for a chat the user named, nothing is asked or pushed."""
    from types import SimpleNamespace

    from aiscientist.gateway import app as gw
    from aiscientist.gateway.db import session_scope
    from aiscientist.gateway.models import User

    client = app_ctx
    _login(client, "root", "rootpass1")
    with session_scope() as s:
        uid = s.query(User).filter_by(username="root").one().id
    cid = _chat(client)["id"]
    pushed, asked = [], []
    conn = SimpleNamespace(owner="root", push=pushed.append)
    monkeypatch.setattr(gw, "_owner_user_id", lambda owner: uid if owner == "root" else None)
    monkeypatch.setattr(gw, "_select_bound_datasets", lambda req: [{"name": "Sample1_WT"}, {"name": "Sample2_MUT"}])

    def session_llm(_conn, role):
        return (lambda messages: asked.append(role) or "CellQC on two samples"), "model"

    req = SimpleNamespace(conversation_id=str(cid), question="Do CellQC quality control on these two samples")
    monkeypatch.setattr(gw, "_session_llm", lambda _conn, role: (None, ""))
    assert gw._name_chat(conn, req) == "" and pushed == []          # no model up: the next message tries again

    monkeypatch.setattr(gw, "_session_llm", session_llm)
    assert gw._name_chat(conn, req) == "Sample1_WT +1 · CellQC on two samples"
    assert asked == ["classify"]
    assert pushed == [{"type": "chat_title", "conversation_id": str(cid),
                       "title": "Sample1_WT +1 · CellQC on two samples"}]
    assert gw._name_chat(conn, req) == "" and asked == ["classify"]  # named once; the model is not asked again

    typed = _chat(client)["id"]
    client.patch(f"/api/conversations/{typed}", json={"title": "Mine", "title_source": "user"})
    assert gw._name_chat(conn, SimpleNamespace(conversation_id=str(typed), question="x y z")) == ""
    # A chat with no account id (a signed-out browser's local chat) is left alone.
    assert gw._name_chat(conn, SimpleNamespace(conversation_id="k3j2h1", question="x y z")) == ""
    assert len(pushed) == 1
