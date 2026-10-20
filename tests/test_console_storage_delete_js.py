"""The storage panel says a deletion is queued on HPC3 and reads the folders again once its job has
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
    block = source[source.index("async function loadStorageListing()"):source.index("async function stopGpu()")]
    prelude = r'''
const assert = require("assert");
const els = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: "", textContent: "", _hidden: id === "storageModal",
  classList: { contains(c) { return c === "hidden" && els[id]._hidden; } } });
const $ = (id) => el(id);
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
const listing = (items) => ({ base: "/r/Temp/tester", used: "1G", quota_raw: "", items });
'''
    script = prelude + block + harness
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr or out.stdout


def test_a_delete_says_it_is_queued_and_the_list_is_read_again_when_its_job_ended():
    _run(r'''
(async () => {
  $("storageModal")._hidden = false;
  const p = "/r/uploads/tester/My Sample 2";
  renderStorage(listing([{ name: "My Sample 2", size: "1.2G", path: p }]));
  assert($("storageItems").innerHTML.includes(`data-path="${p}"`), "a Delete button first");

  const queued = { id: "a", path: p, job_id: "901", status: "queued" };
  replies["/api/storage/delete"] = { body: { status: "queued", job: queued, deletes: [queued] } };
  await deleteStorageItem(p);
  assert.deepStrictEqual(fetched[0], ["/api/storage/delete", { connection_id: "c1", path: p }]);
  assert(toasts[0].includes("Deletion queued on HPC3 (job 901)"), toasts[0]);
  const row = $("storageItems").innerHTML;
  assert(row.includes("Deletion queued (job 901)") && !row.includes("data-path"), row);
  assert($("storageDeletes").innerHTML.includes("deleting, job 901 queued"), $("storageDeletes").innerHTML);
  assert.deepStrictEqual(timers, [8000], "it asks again while the job is queued");

  // Still running: no new listing.
  replies["/api/storage/c1/deletes"] = { body: { deletes: [{ ...queued, slurm_state: "RUNNING" }] } };
  await loadStorageDeletes();
  assert($("storageDeletes").innerHTML.includes("job 901 running"));
  assert(!fetched.some(([u]) => u === "/api/storage/c1"), "the folders are not read while it runs");

  // Ended: the folders are read again, and the row is gone.
  replies["/api/storage/c1/deletes"] = { body: { deletes: [{ ...queued, status: "done" }] } };
  replies["/api/storage/c1"] = { body: listing([]) };
  await loadStorageDeletes();
  await new Promise((r) => setImmediate(r));
  assert($("storageDeletes").innerHTML.includes("deleted"), $("storageDeletes").innerHTML);
  assert(fetched.some(([u]) => u === "/api/storage/c1"), "the folders are read again");
  assert($("storageItems").innerHTML.includes("No items"), $("storageItems").innerHTML);
  assert.strictEqual(timers.length, 2, "nothing queued: no more asking");
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_a_failed_deletion_says_why_and_the_mock_deletes_at_once():
    _run(r'''
(async () => {
  $("storageModal")._hidden = false;
  renderStorageDeletes([{ id: "b", path: "/r/Temp/tester/x", job_id: "902", status: "failed",
                          error: "a symbolic link on the way to it" }]);
  assert($("storageDeletes").innerHTML.includes("not deleted: a symbolic link on the way to it"));

  replies["/api/storage/delete"] = { body: { status: "deleted", job: { job_id: "mock", status: "done" }, ...listing([]) } };
  await deleteStorageItem("/r/Temp/tester/y");
  assert.strictEqual(toasts[0], "Deleted.");
  assert($("storageItems").innerHTML.includes("No items"));

  // A listing read while a job is queued shows the row as queued (e.g. the panel reopened).
  renderStorage(listing([{ name: "z", size: "1K", path: "/r/Temp/tester/z", deleting: "903" }]));
  assert($("storageItems").innerHTML.includes("Deletion queued (job 903)"));
})().catch((e) => { console.error(e); process.exit(1); });
''')
