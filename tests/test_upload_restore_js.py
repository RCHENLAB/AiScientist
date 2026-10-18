"""A browser that never saw an upload shows the account's unfinished ones as "Interrupted" cards.

A page found unfinished uploads only in its own storage, so on another computer or in a private
window nothing said the server held part of a file: the user had to remember to choose it again
(POST /api/tus/find then continued it). Now the page also asks the server (GET /api/tus/unfinished)
and adds a card for each upload it does not already show. These run the page's own card code from
frontend/console/app.js under node, with a scripted server and browser storage. Skipped where node
is not installed.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"

HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const src = fs.readFileSync(process.env.APP_JS, "utf8");
const between = (a, b) => src.slice(src.indexOf(a), src.indexOf(b));
const line = (re) => src.match(re)[0];
const code = [line(/^const UPLOAD_ACTIVE = .*$/m), line(/^function tusId\(.*$/m), line(/^function hashStr[\s\S]*?\n}\n/m),
  between("// -- resuming over tus", "// Send one file over tus"),
  between("// -- the cards", "// -- a folder: one card"),
  between("// -- after a page reload", "// ---- dataset selection")].join("\n");

function scenario({ stored = [], folders = {}, listing, owner = "alice" }) {
  const calls = [];
  // the browser's tus storage
  const items = new Map(stored.map((e) => [e.urlStorageKey, e]));
  const urlStorage = { findAllUploads: async () => [...items.values()], removeUpload: async (k) => { items.delete(k); } };
  // localStorage: Object.keys lists only the stored keys
  const local = {};
  for (const m of ["getItem", "setItem", "removeItem"]) Object.defineProperty(local, m, { enumerable: false,
    value: m === "getItem" ? (k) => (k in local ? local[k] : null) : m === "setItem" ? (k, v) => { local[k] = String(v); } : (k) => { delete local[k]; } });
  Object.assign(local, folders);
  const answer = (status, body = {}, headers = {}) => ({ ok: status < 300, status, json: async () => body,
    headers: { get: (k) => headers[k] ?? null } });
  const fetch = async (url, init = {}) => {
    const method = init.method || "GET";
    calls.push(`${method} ${url.replace("http://test", "")}`);
    if (method === "HEAD") {
      const e = [...items.values()].find((x) => x.uploadUrl === url);
      return e && e.head ? answer(200, {}, { "Upload-Offset": String(e.head[0]), "Upload-Length": String(e.head[1]) }) : answer(404);
    }
    if (method === "DELETE") return answer(204);
    if (url.startsWith("/api/tus/unfinished")) {
      if (listing === "down") throw new TypeError("Failed to fetch");
      return typeof listing === "number" ? answer(listing) : answer(200, listing);
    }
    return answer(404);
  };
  const ctx = { window: { tus: { defaultOptions: { urlStorage } }, location: { href: "http://test/console" } },
    state: { connectionId: "c1", uploads: [] }, localStorage: local, fetch, console, URL, crypto: globalThis.crypto,
    tusHeaders: () => ({ "Tus-Resumable": "1.0.0" }), apiKeyOwner: () => owner, UPLOAD_CHUNK: 4,
    noteUploadPace: () => {}, uploadPaces: new Map(), uploadLines: new Map(), $: () => null, uploadPaceText: () => "",
    fmtUploadSize: String };
  const api = vm.runInNewContext(code + "\n;({ restorePendingCards, discardUpload, dropPendingCardFor, uploadCardView })", ctx);
  const cards = () => ctx.state.uploads.map((u) => ({ id: u.id, kind: u.kind, name: u.name, size: u.size, sent: u.sent,
    status: u.status, note: u.note, tusUrl: u.tusUrl || null, tusUrls: u.tusUrls || null, partsFolder: u.partsFolder || null,
    buttons: api.uploadCardView(u).buttons }));
  return { api, ctx, calls, cards };
}
const hash = (s) => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h).toString(36); };
const file = (id, name, size, offset) => ({ id, url: `/api/tus/${id}`, name, rel_path: "", folder: "", size, offset, created_at: 1 });
const part = (id, folder, rel, size, offset) => ({ id, url: `/api/tus/${id}`, name: rel.split("/").pop(), rel_path: `${folder}/${rel}`,
  folder, size, offset, created_at: 1 });
const folder = (name, files) => ({ folder: name, size: files.reduce((s, f) => s + f.size, 0),
  offset: files.reduce((s, f) => s + f.offset, 0), created_at: 1, files });

(async () => {
  const out = { hash: { study: hash("study") } };

  // A browser with nothing stored
  const fresh = scenario({ listing: { files: [file("b1", "mydata.bam", 1000, 400)],
    folders: [folder("study", [part("s1", "study", "raw/a.bin", 12, 4), part("s2", "study", "b.bin", 12, 8)])] } });
  await fresh.api.restorePendingCards();
  out.fresh = { cards: fresh.cards(), calls: [...fresh.calls] };
  await fresh.api.restorePendingCards();                       // a second connection: nothing twice
  out.fresh_again = fresh.cards().map((c) => c.id);

  // Discard deletes what the server holds; a card whose upload continues goes
  await fresh.api.discardUpload(`sf${hash("study")}`);
  out.discard_folder = { calls: fresh.calls.filter((c) => c.startsWith("DELETE")), cards: fresh.cards().map((c) => c.id) };
  fresh.api.dropPendingCardFor("http://test/api/tus/other");
  out.drop_other = fresh.cards().map((c) => c.id);
  fresh.api.dropPendingCardFor("http://test/api/tus/b1");        // absolute, as tus-js-client stores it
  out.drop_b1 = fresh.cards().map((c) => c.id);
  const single = scenario({ listing: { files: [file("b2", "x.bin", 20, 8)], folders: [] } });
  await single.api.restorePendingCards();
  await single.api.discardUpload("sb2");
  out.discard_file = { calls: single.calls.filter((c) => c.startsWith("DELETE")), cards: single.cards().length };

  // This browser's own cards speak for what it stores
  const key = "upload-folder:alice:kept:3:123";
  const mine = scenario({
    stored: [
      { urlStorageKey: "tus::tus:alice:own bin.dat:10:1::0", uploadUrl: "http://test/api/tus/x1", size: 10,
        metadata: { filename: "own bin.dat" }, head: [5, 10] },
      { urlStorageKey: "tus::tus:alice:mine/a.bin:12:1::0", uploadUrl: "http://test/api/tus/y1", size: 12,
        metadata: { filename: "a.bin", rel_path: "mine/a.bin" }, head: [4, 12] },
    ],
    folders: { [key]: "kept" },
    listing: { files: [file("x1", "ownbin.dat", 10, 5),                // stored here
                       file("z1", "own bin.dat", 10, 8),               // another upload of a file that has a card
                       file("w1", "new.bin", 40, 10)],
               folders: [folder("kept", [part("k1", "kept", "a.bin", 12, 4)]),   // a card of that folder is here
                         folder("mine", [part("y1", "mine", "a.bin", 12, 4)]),   // all of it stored here
                         folder("both", [part("y2", "both", "a.bin", 12, 4), part("y3", "both", "b.bin", 12, 8)])] } });
  mine.ctx.state.uploads.push({ id: "running", kind: "folder", name: "both/", status: "uploading" });
  await mine.api.restorePendingCards();
  out.mine = mine.cards().map((c) => [c.id, c.note]);

  // No list: the cards this browser keeps are all there are
  for (const [name, listing] of [["down", "down"], ["error", 500], ["signed_out", 401]]) {
    const s = scenario({ stored: [{ urlStorageKey: "tus::tus:alice:own.dat:10:1::0", uploadUrl: "http://test/api/tus/x1",
      size: 10, metadata: { filename: "own.dat" }, head: [5, 10] }], listing });
    await s.api.restorePendingCards();
    out[name] = s.cards().map((c) => c.id);
  }
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def runs():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "-e", HARNESS], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "APP_JS": str(APP_JS)})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_new_browser_shows_the_servers_unfinished_file(runs):
    card = {c["id"]: c for c in runs["fresh"]["cards"]}["sb1"]
    assert card == {"id": "sb1", "kind": "file", "name": "mydata.bam", "size": 1000, "sent": 400, "status": "pending",
                    "note": "Interrupted at 40% in another browser — choose the file again to continue",
                    "tusUrl": "/api/tus/b1", "tusUrls": None, "partsFolder": None,
                    "buttons": [["pickfile", "Choose file"], ["discard", "Discard"]]}
    assert runs["fresh"]["calls"] == ["GET /api/tus/unfinished?connection_id=c1"]


def test_a_folder_is_one_card_that_says_how_many_files_are_part_way(runs):
    assert [c["id"] for c in runs["fresh"]["cards"]] == ["sf" + runs["hash"]["study"], "sb1"]   # newest on top
    card = runs["fresh"]["cards"][0]
    assert card == {"id": "sf" + runs["hash"]["study"], "kind": "folder", "name": "study/", "size": 24, "sent": 12,
                    "status": "pending",
                    "note": "Interrupted in another browser — 2 files part-way; choose the folder again to continue",
                    "tusUrl": None, "tusUrls": ["/api/tus/s1", "/api/tus/s2"], "partsFolder": "study",
                    "buttons": [["pickdir", "Choose folder"], ["discard", "Discard"]]}


def test_asking_again_adds_nothing_twice(runs):
    assert runs["fresh_again"] == [c["id"] for c in runs["fresh"]["cards"]]


def test_discard_deletes_the_uploads_the_server_holds(runs):
    assert runs["discard_folder"]["calls"] == ["DELETE /api/tus/s1", "DELETE /api/tus/s2"]
    assert runs["discard_folder"]["cards"] == ["sb1"]
    assert runs["discard_file"] == {"calls": ["DELETE /api/tus/b2"], "cards": 0}


def test_the_card_goes_once_its_upload_continues(runs):
    """Choosing the file again continues the upload through POST /api/tus/find (test_tus_resume_js):
    the server's card, which has the server's cleaned name, is matched by its upload."""
    assert runs["drop_other"] == ["sb1"]
    assert runs["drop_b1"] == []


def test_what_this_browser_stores_or_shows_gets_no_second_card(runs):
    assert runs["mine"] == [
        ["sw1", "Interrupted at 25% in another browser — choose the file again to continue"],
        ["F" + _hash("upload-folder:alice:kept:3:123"), "Interrupted — 0 of 3 files uploaded; choose the folder again to continue"],
        ["p" + _hash("tus:alice:own bin.dat:10:1"), "Interrupted at 50% — choose the file again to continue"],
        ["running", None],
    ]


@pytest.mark.parametrize("case", ("down", "error", "signed_out"))
def test_without_the_list_the_browsers_own_cards_still_come_back(runs, case):
    assert runs[case] == ["p" + _hash("tus:alice:own.dat:10:1")]


def test_the_list_is_asked_for_by_the_route_the_server_serves():
    from aiscientist.gateway import tus_upload
    assert "/api/tus/unfinished" in {getattr(r, "path", "") for r in tus_upload.router.routes}
    assert "/api/tus/unfinished?connection_id=" in APP_JS.read_text()


def _hash(s: str) -> str:
    """app.js hashStr, base 36 of its absolute value: the ids the page gives restored cards."""
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    h = h - (1 << 32) if h >= 1 << 31 else h
    digits, n, out = "0123456789abcdefghijklmnopqrstuvwxyz", abs(h), ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out
