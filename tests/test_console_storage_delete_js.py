"""My account → Storage says a deletion on HPC3 is queued and reads the folders again once its job has
ended, using the actual console functions under node."""
from pathlib import Path
import shutil
import subprocess

import pytest

APP = Path(__file__).resolve().parents[1] / "frontend/console/app.js"


def _run(harness: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    block = source[source.index("// ---- My account → Storage"):source.index("async function stopGpu()")]
    size = source[source.index("function fmtSize(n)"):source.index("// Human label + sort order")]
    prelude = r'''
const assert = require("assert");
const els = {};
const el = (id) => {
  if (!els[id]) {
    const cls = new Set(id === "accountPane-storage" ? ["hidden"] : []);
    els[id] = { id, innerHTML: "", textContent: "", dataset: {}, open: false,
      classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
                   toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)) } };
  }
  return els[id];
};
const $ = (id) => el(id);
const showStorage = () => { $("view-account").classList.add("active"); $("accountPane-storage").classList.remove("hidden"); };
const state = { connectionId: "c1" };
const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const toasts = [], toast = (m) => { toasts.push(m); };
const isUnreachable = () => false;
const confirm = () => true;
const timers = [];
const setTimeout = (f, ms) => { timers.push(ms); return timers.length; };
const clearTimeout = () => {};
const fetched = [];
let replies = {};
const fetch = async (url, opts) => {
  fetched.push([url, opts && opts.body ? JSON.parse(opts.body) : null]);
  const r = replies[url];
  return { ok: r.ok !== false, json: async () => r.body };
};
const UPLOADS = "/r/uploads/tester";
const listing = (items) => ({ quota: null, ttl_days: 3, items, areas: [
  { key: "temp", label: "Temporary run files", note: "Removed when your session ends.", path: "/r/Temp/tester", bytes: 0, items: [] },
  { key: "uploads", label: "Uploads on HPC3", note: "Kept until you delete them.", path: UPLOADS,
    bytes: items.reduce((n, i) => n + i.bytes, 0), items }] });
'''
    script = prelude + size + block + harness
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr or out.stdout


def test_a_delete_says_it_is_queued_and_the_list_is_read_again_when_its_job_ended():
    _run(r'''
(async () => {
  showStorage();
  const p = `${UPLOADS}/My Sample 2`;
  renderStorage(listing([{ name: "My Sample 2", bytes: 1288490189, path: p, area: "uploads" }]));
  assert($("storeHpcBody").innerHTML.includes(`data-path="${p}"`), "a Delete button first");

  const queued = { id: "a", path: p, job_id: "901", status: "queued" };
  replies["/api/storage/delete"] = { body: { status: "queued", job: queued, deletes: [queued] } };
  await deleteStorageItem(p);
  assert.deepStrictEqual(fetched[0], ["/api/storage/delete", { connection_id: "c1", path: p }]);
  assert(toasts[0].includes("Deletion queued on HPC3 (job 901)"), toasts[0]);
  const row = $("storeHpcBody").innerHTML;
  assert(row.includes("Deletion queued (job 901)") && !row.includes("data-path"), row);
  assert($("storageDeletes").innerHTML.includes("deleting, job 901 queued"), $("storageDeletes").innerHTML);
  assert.deepStrictEqual(timers, [8000], "it asks again while the job is queued");

  // Still running: no new listing.
  replies["/api/storage/c1/deletes"] = { body: { deletes: [{ ...queued, slurm_state: "RUNNING" }] } };
  await loadStorageDeletes();
  assert($("storageDeletes").innerHTML.includes("job 901 running"));
  assert(!fetched.some(([u]) => u === "/api/storage/c1"), "the folders are not read while it runs");

  // Ended: the folders are read again, and the row is gone (an empty Uploads area is not shown).
  replies["/api/storage/c1/deletes"] = { body: { deletes: [{ ...queued, status: "done" }] } };
  replies["/api/storage/c1"] = { body: listing([]) };
  await loadStorageDeletes();
  await new Promise((r) => setImmediate(r));
  assert($("storageDeletes").innerHTML.includes("deleted"), $("storageDeletes").innerHTML);
  assert(fetched.some(([u]) => u === "/api/storage/c1"), "the folders are read again");
  const after = $("storeHpcBody").innerHTML;
  assert(!after.includes("My Sample 2") && !after.includes("Uploads on HPC3") && after.includes("Nothing here now."), after);
  assert.strictEqual(timers.length, 2, "nothing queued: no more asking");
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_a_failed_deletion_says_why_and_the_mock_deletes_at_once():
    _run(r'''
(async () => {
  showStorage();
  renderStorageDeletes([{ id: "b", path: "/r/Temp/tester/x", job_id: "902", status: "failed",
                          error: "a symbolic link on the way to it" }]);
  assert($("storageDeletes").innerHTML.includes("not deleted: a symbolic link on the way to it"));

  replies["/api/storage/delete"] = { body: { status: "deleted", job: { job_id: "mock", status: "done" }, ...listing([]) } };
  await deleteStorageItem("/r/Temp/tester/y");
  assert.strictEqual(toasts[0], "Deleted.");
  assert($("storeHpcBody").innerHTML.includes("Nothing here now."));

  // A listing read while a job is queued shows the row as queued (e.g. the tab opened again).
  renderStorage(listing([{ name: "z", bytes: 1024, path: `${UPLOADS}/z`, deleting: "903" }]));
  assert($("storeHpcBody").innerHTML.includes("Deletion queued (job 903)"));
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_the_deletions_are_not_polled_while_the_tab_is_closed():
    _run(r'''
(async () => {
  const queued = { id: "a", path: `${UPLOADS}/x`, job_id: "901", status: "queued" };
  replies["/api/storage/c1/deletes"] = { body: { deletes: [queued] } };
  await loadStorageDeletes();                       // the tab is not on screen
  assert.deepStrictEqual(timers, [], "no polling behind a closed tab");
  showStorage();
  await loadStorageDeletes();
  assert.deepStrictEqual(timers, [8000]);
})().catch((e) => { console.error(e); process.exit(1); });
''')
