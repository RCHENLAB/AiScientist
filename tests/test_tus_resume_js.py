"""The console continues an interrupted upload; it never starts the file again while the server has it.

Prod, 2026-10-13 (a 14.57 GiB BAM): the resume HEAD got a 504 from the proxy, and
tus-js-client 4.3.1 takes any answer but 2xx or 423 for "cannot resume": it created a second upload
and the file started again from byte 0. On a 5xx it keeps the first upload in the browser's storage,
and so never stores the second: after a reload the page continued the first, abandoned one, throwing
away the 4 GiB the second held. These run the real tus-js-client bundle under node, with the page's
hooks from frontend/console/app.js and a scripted server. Skipped where node is not installed.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE = Path(__file__).resolve().parents[1] / "frontend" / "console"

HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const [appPath, tusPath] = JSON.parse(process.env.TUS_TEST_PATHS);
const tus = require(tusPath);
const src = fs.readFileSync(appPath, "utf8");
const block = src.slice(src.indexOf("// -- resuming over tus"), src.indexOf("// Send one file over tus"));
const api = vm.runInNewContext(block + "\n;({ tusShouldRetry, tusResumeGuard, tusResponseOf, pickPreviousUpload, findServerUpload, previousUploadFor })",
  { window: { tus, location: { href: "http://test/console" } }, tusHeaders: () => ({ "Tus-Resumable": "1.0.0" }), console,
    UPLOAD_CHUNK: 4, state: { connectionId: "c1" }, crypto: globalThis.crypto, URL,
    fetch: async () => { throw new TypeError("the page asked the server unexpectedly"); } });

// A scripted tus server: `answer(request)` gives [status, headers] for each request.
function stack(answer) {
  const log = [];
  return { log, getName: () => "scripted", createRequest(method, url) {
    const headers = {};
    return { getMethod: () => method, getURL: () => url, setHeader: (k, v) => { headers[k] = v; },
      getHeader: (k) => headers[k], setProgressHandler() {}, abort: () => Promise.resolve(),
      getUnderlyingObject: () => null,
      async send(body) {
        const offset = headers["Upload-Offset"] === undefined ? undefined : String(headers["Upload-Offset"]);
        const req = { method, url: url.replace("http://test", ""), offset, size: body ? body.size : 0 };
        log.push(req);
        const [status, h = {}] = answer(req);
        return { getStatus: () => status, getHeader: (k) => h[k], getBody: () => "", getUnderlyingObject: () => null };
      } };
  } };
}
// The browser's tus storage, holding `entries` for the file's fingerprint "fp".
function storage(entries) {
  const items = new Map(entries.map((e, i) => [`tus::fp::${i}`, e]));
  const listed = (prefix) => [...items].filter(([k]) => k.startsWith(prefix)).map(([k, v]) => ({ ...v, urlStorageKey: k }));
  return { items, findAllUploads: async () => listed("tus::"), findUploadsByFingerprint: async (fp) => listed(`tus::${fp}::`),
    removeUpload: async (k) => { items.delete(k); },
    addUpload: async (fp, v) => { const k = `tus::${fp}::n${items.size}`; items.set(k, v); return k; } };
}
const stored = (s) => [...s.items.values()].map((v) => v.uploadUrl.replace("http://test", ""));
// The page's way of continuing a file (tusUpload): the stored upload, else the one the server holds,
// resumed, started.
function upload(answer, { guard = true, entries = [{ uploadUrl: "http://test/api/tus/old", size: 10 }], probe,
                          file = new Blob([Buffer.alloc(10)]) } = {}) {
  return new Promise((resolve) => {
    const http = stack(answer), urls = storage(entries);
    const done = (ok, err) => resolve({ ok, log: http.log, stored: stored(urls),
      status: err && api.tusResponseOf(err) ? api.tusResponseOf(err).getStatus() : null });
    const options = { endpoint: "http://test/api/tus", chunkSize: 10, retryDelays: [0, 0, 0, 0, 0],
      httpStack: http, urlStorage: urls, fingerprint: async () => "fp",
      onShouldRetry: api.tusShouldRetry, onSuccess: () => done(true), onError: (err) => done(false, err) };
    if (guard) options.onAfterResponse = api.tusResumeGuard;
    const up = new tus.Upload(file, options);
    api.previousUploadFor(up, file, {}, probe)
      .then((previous) => { if (previous) up.resumeFromPreviousUpload(previous); up.start(); });
  });
}
// The resume HEAD answers `heads` in turn (the last one from then on); the server holds 5 of 10 bytes.
function server(heads) {
  let n = 0;
  return (req) => {
    if (req.method === "HEAD") { const s = heads[Math.min(n++, heads.length - 1)];
      return s === 200 ? [200, { "Upload-Offset": "5", "Upload-Length": "10" }] : [s]; }
    if (req.method === "PATCH") return [204, { "Upload-Offset": String(Number(req.offset) + req.size) }];
    if (req.method === "POST") return [201, { Location: "http://test/api/tus/new" }];
    return [405];
  };
}
// What each stored upload's HEAD says, for pickPreviousUpload.
function probe(states) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push(`${init.method} ${url.replace("http://test", "")}`);
    const s = states[url.replace("http://test", "")];
    if (!s) throw new TypeError("Failed to fetch");
    return { status: s[0], headers: { get: (k) => (s[1] || {})[k] ?? null } };
  };
  return { fetch, calls };
}
const at = (offset, length = 10) => ({ "Upload-Offset": String(offset), "Upload-Length": String(length) });
// The server's answer to POST /api/tus/find: [status, body], or "down" for no answer at all.
function finder(answer) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ method: init.method, url, body: init.body ? JSON.parse(init.body) : null });
    if (answer === "down") throw new TypeError("Failed to fetch");
    return { ok: answer[0] < 300, status: answer[0], json: async () => answer[1] };
  };
  return { fetch, calls };
}
const picked = () => new File([Buffer.alloc(10)], "data.bin", { lastModified: 1760400000000 });
const two = [{ uploadUrl: "http://test/api/tus/old", size: 10, creationTime: "Tue Oct 13 2026 21:00:10 GMT-0700" },
             { uploadUrl: "http://test/api/tus/new", size: 10, creationTime: "Tue Oct 13 2026 21:03:00 GMT-0700" }];

(async () => {
  const out = {};
  out.proxy_504 = await upload(server([504, 504, 200]));
  out.proxy_504_unguarded = await upload(server([504, 504, 200]), { guard: false });
  out.proxy_504_for_good = await upload(server([504]));
  out.locked_423 = await upload(server([423, 423, 200]));
  out.gone_404 = await upload(server([404]));
  out.signed_out_401 = await upload(server([401]));

  const pick = async (states) => {
    const p = probe(states), urls = storage(two);
    const chosen = await api.pickPreviousUpload(await urls.findUploadsByFingerprint("fp"), urls, p.fetch);
    await new Promise((r) => setTimeout(r, 0));
    return { chosen: chosen && chosen.uploadUrl.replace("http://test", ""), stored: stored(urls), calls: p.calls };
  };
  out.pick_further = await pick({ "/api/tus/old": [200, at(528, 14570)], "/api/tus/new": [200, at(4550, 14570)] });
  out.pick_busy = await pick({ "/api/tus/old": [200, at(528, 14570)], "/api/tus/new": [423, at(4550, 14570)] });
  out.pick_unsure = await pick({ "/api/tus/old": [200, at(528, 14570)], "/api/tus/new": [504] });
  out.pick_gone = await pick({ "/api/tus/old": [404], "/api/tus/new": [200, at(5)] });
  out.pick_done = await pick({ "/api/tus/old": [200, at(10)], "/api/tus/new": [200, at(10)] });
  const single = probe({}), one = storage([two[0]]);
  out.pick_one = { chosen: (await api.pickPreviousUpload(await one.findUploadsByFingerprint("fp"), one, single.fetch)).uploadUrl,
                   calls: single.calls };
  // The whole page, two stored uploads: the further one continues, nothing is created.
  out.page_two_stored = await upload(server([200]), { entries: two,
    probe: probe({ "/api/tus/old": [200, at(2)], "/api/tus/new": [200, at(5)] }).fetch });

  // Nothing stored in this browser: the server's upload of the file continues from its offset.
  const elsewhere = async (answer) => {
    const f = finder(answer);
    const run = await upload(server([200]), { entries: [], probe: f.fetch, file: picked() });
    return { ...run, finds: f.calls };
  };
  out.server_found = await elsewhere([200, { upload: { url: "/api/tus/srv", offset: 5, length: 10 } }]);
  out.server_none = await elsewhere([200, { upload: null }]);
  out.server_error = await elsewhere([500, {}]);
  out.server_down = await elsewhere("down");
  const asked = finder([200, { upload: { url: "/api/tus/srv", offset: 5, length: 10 } }]);
  out.stored_first = { ...(await upload(server([200]), { probe: asked.fetch, file: picked() })), finds: asked.calls };
  const quiet = finder([200, { upload: null }]);
  out.not_asked = {
    one_chunk: await api.findServerUpload(new File([Buffer.alloc(4)], "s.csv", { lastModified: 1 }), {}, quiet.fetch),
    no_time: await api.findServerUpload(new Blob([Buffer.alloc(10)]), {}, quiet.fetch),
    calls: quiet.calls.length };
  const folder = finder([200, { upload: null }]);
  await api.findServerUpload(picked(), { folder: "study", rel_path: "study/raw/data.bin" }, folder.fetch);
  out.folder_body = folder.calls[0].body;
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def runs():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    paths = json.dumps([str(CONSOLE / "app.js"), str(CONSOLE / "tus.min.js")])
    done = subprocess.run([node, "-e", HARNESS], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "TUS_TEST_PATHS": paths})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _methods(run):
    return [f"{r['method']} {r['url']}" for r in run["log"]]


def test_a_5xx_on_the_resume_head_is_retried_and_never_creates_a_new_upload(runs):
    run = runs["proxy_504"]
    assert run["ok"] is True
    assert _methods(run) == ["HEAD /api/tus/old"] * 3 + ["PATCH /api/tus/old"]
    assert run["log"][-1]["offset"] == "5"                      # continued where the server was


def test_without_the_guard_the_library_starts_the_file_again(runs):
    """What happened on prod: the same answers, with tus-js-client's own rule only."""
    run = runs["proxy_504_unguarded"]
    assert _methods(run) == ["HEAD /api/tus/old", "POST /api/tus", "PATCH /api/tus/new"]
    assert run["log"][-1] == {"method": "PATCH", "url": "/api/tus/new", "offset": "0", "size": 10}
    assert run["stored"] == ["/api/tus/old"]       # a reload continues the abandoned upload


def test_a_5xx_that_lasts_ends_in_an_error_and_keeps_the_upload(runs):
    run = runs["proxy_504_for_good"]
    assert run["ok"] is False and run["status"] == 504
    assert _methods(run) == ["HEAD /api/tus/old"] * 6           # the first try and five retries
    assert run["stored"] == ["/api/tus/old"]                    # Resume continues it later


def test_a_locked_upload_is_waited_for(runs):
    run = runs["locked_423"]
    assert run["ok"] is True
    assert _methods(run) == ["HEAD /api/tus/old"] * 3 + ["PATCH /api/tus/old"]


def test_only_an_upload_the_server_does_not_have_is_started_again(runs):
    run = runs["gone_404"]
    assert run["ok"] is True
    assert _methods(run) == ["HEAD /api/tus/old", "POST /api/tus", "PATCH /api/tus/new"]
    assert run["stored"] == ["/api/tus/new"]


def test_signed_out_stops_at_once_and_keeps_the_upload(runs):
    run = runs["signed_out_401"]
    assert run["ok"] is False and run["status"] == 401
    assert _methods(run) == ["HEAD /api/tus/old"]
    assert run["stored"] == ["/api/tus/old"]


def test_of_two_stored_uploads_the_one_further_along_continues(runs):
    """Prod, 23:10: previous[0] was the older upload at 528 MiB; the newer one held 4.55 GiB."""
    run = runs["pick_further"]
    assert run["chosen"] == "/api/tus/new"
    assert run["stored"] == ["/api/tus/new"]
    assert sorted(run["calls"]) == ["DELETE /api/tus/old", "HEAD /api/tus/new", "HEAD /api/tus/old"]


def test_a_busy_upload_still_counts_by_the_offset_the_server_holds(runs):
    run = runs["pick_busy"]
    assert run["chosen"] == "/api/tus/new" and run["stored"] == ["/api/tus/new"]
    assert "DELETE /api/tus/old" in run["calls"]


def test_nothing_is_given_up_while_one_upload_did_not_answer(runs):
    run = runs["pick_unsure"]
    assert run["chosen"] == "/api/tus/old"
    assert run["stored"] == ["/api/tus/old", "/api/tus/new"]
    assert not any(c.startswith("DELETE") for c in run["calls"])


def test_an_upload_gone_from_the_server_is_forgotten_without_a_delete(runs):
    run = runs["pick_gone"]
    assert run["chosen"] == "/api/tus/new" and run["stored"] == ["/api/tus/new"]
    assert not any(c.startswith("DELETE") for c in run["calls"])


def test_a_complete_upload_is_never_deleted(runs):
    run = runs["pick_done"]
    assert run["chosen"] == "/api/tus/new"                      # both complete: the newer one
    assert run["stored"] == ["/api/tus/new"]
    assert not any(c.startswith("DELETE") for c in run["calls"])


def test_a_single_stored_upload_is_continued_without_asking_first(runs):
    assert runs["pick_one"] == {"chosen": "http://test/api/tus/old", "calls": []}


def test_the_page_continues_the_further_of_two_stored_uploads(runs):
    run = runs["page_two_stored"]
    assert run["ok"] is True
    assert _methods(run) == ["HEAD /api/tus/new", "PATCH /api/tus/new"]
    assert run["stored"] == ["/api/tus/new"]


# --- a browser with nothing stored continues the server's upload of the file ------------------------

def test_the_server_upload_of_the_file_continues_from_its_offset(runs):
    """Another computer, a private window, cleared site data: nothing stored for the file."""
    import hashlib
    run = runs["server_found"]
    assert run["ok"] is True
    assert _methods(run) == ["HEAD /api/tus/srv", "PATCH /api/tus/srv"]     # no POST: nothing created
    assert run["log"][-1]["offset"] == "5"
    assert run["stored"] == ["/api/tus/srv"]          # now stored here too: a reload brings it back
    assert run["finds"] == [{"method": "POST", "url": "/api/tus/find", "body": {
        "connection_id": "c1", "name": "data.bin", "rel_path": "", "size": 10, "last_modified": 1760400000000,
        "head_sha256": hashlib.sha256(bytes(4)).hexdigest()}}]


@pytest.mark.parametrize("case", ("server_none", "server_error", "server_down"))
def test_without_a_server_upload_the_file_starts_as_before(runs, case):
    run = runs[case]
    assert run["ok"] is True
    assert _methods(run) == ["POST /api/tus", "PATCH /api/tus/new"]
    assert run["log"][-1]["offset"] == "0"
    assert len(run["finds"]) == 1


def test_an_upload_stored_in_this_browser_is_not_looked_up(runs):
    run = runs["stored_first"]
    assert _methods(run) == ["HEAD /api/tus/old", "PATCH /api/tus/old"]
    assert run["finds"] == []


def test_a_file_that_cannot_be_left_part_way_is_not_looked_up(runs):
    assert runs["not_asked"] == {"one_chunk": None, "no_time": None, "calls": 0}


def test_a_folder_file_is_looked_up_by_its_path(runs):
    body = runs["folder_body"]
    assert body["rel_path"] == "study/raw/data.bin" and body["name"] == "data.bin"
