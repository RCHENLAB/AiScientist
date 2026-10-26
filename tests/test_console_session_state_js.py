"""The console shows the compute session's state, its copies and its failures (issue 6), using the
actual console functions under node."""
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
    block = source[source.index("// ---- the compute session on HPC3"):source.index("// ---- My account → Storage")]
    size = source[source.index("function fmtSize(n)"):source.index("// Human label + sort order")]
    prelude = r'''
const assert = require("assert");
const els = {};
const el = (id) => els[id] || (els[id] = { id, innerHTML: "", textContent: "", title: "", className: "",
  _hidden: false, dataset: {},
  classList: { add(c) { if (c === "hidden") this._o._hidden = true; },
               remove(c) { if (c === "hidden") this._o._hidden = false; },
               toggle(c, on) { if (c === "hidden") this._o._hidden = !!on; },
               contains(c) { return c === "hidden" && this._o._hidden; } } });
const $ = (id) => { const e = el(id); e.classList._o = e; return e; };
const state = { connectionId: "c1" };
let shown = false;                      // My account → Storage on screen
const storageShown = () => shown;
const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const relTime = () => "just now";
const toast = (m) => { toasts.push(m); }, toasts = [];
const isUnreachable = () => false;
let fetched = [];
'''
    script = prelude + size + block + harness
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr or out.stdout


def test_a_copy_in_flight_shows_its_progress_in_the_card_and_the_chip():
    _run(r'''
renderSessionState({ id: "s1", state: "staging-in", transfers: [], unresolved: [],
  active: [{ label: "Sample2 to HPC3", status: "running", kind: "stage-in", bytes_done: 512 * 1024 * 1024,
             bytes_total: 2 * 1024 ** 3, files_done: 1, files_total: 4, percent: 25 }] });
const card = $("connSession").innerHTML, chip = $("sessChip");
assert(card.includes("Staging in"), card);
assert(card.includes("Sample2 to HPC3: 512.0 MB of 2.00 GB (25%) · file 2 of 4"), card);
assert(card.includes('style="width:25%"'), card);
assert(!chip._hidden && chip.className.includes("staging-in"), chip.className);
assert(chip.innerHTML === '<span class="sc-long">Staging in</span><span class="sc-short">In</span> 25%', chip.innerHTML);
renderSessionState({ id: "s1", state: "open", transfers: [], active: [], unresolved: [] });
assert(chip._hidden, "an idle session has no chip");
assert($("connSession").innerHTML.includes("nothing is being copied"));
''')


def test_a_failed_save_says_what_failed_and_that_it_is_still_on_hpc3():
    _run(r'''
const t = { label: "Saving x.h5ad to your data", status: "failed", kind: "stage-out", error: "OSError: link dropped" };
renderSessionState({ id: "s1", state: "failed", transfers: [t], active: [], unresolved: [t] });
const card = $("connSession").innerHTML;
assert(card.includes("Failed") && card.includes("Saving x.h5ad to your data failed: OSError: link dropped"), card);
assert(card.includes("still on HPC3: try again from Runs"), card);
assert($("sessChip").innerHTML.includes('<span class="sc-long">Failed</span>'));
const ended = sessionHeadline({ state: "failed", closed_at: "2026-10-18T20:00:00", unresolved: [t] }, 3);
assert(ended.includes("kept for 3 days: connect and save it again from Runs"), ended);
''')


def test_how_an_ended_session_went_is_said_in_words():
    _run(r'''
const say = (c) => sessionHeadline({ state: "closed", closed_at: "x", cleanup: c, unresolved: [] }, 3);
assert.strictEqual(say({ status: "queued", job_id: "8123456" }), "Cleanup job 8123456 queued on HPC3.");
assert.strictEqual(say({ status: "done", job_id: "8123456", removed: 4 }), "Cleanup job 8123456 finished: 4 folder(s) removed.");
assert.strictEqual(say({ status: "nothing" }), "Nothing of it was left on HPC3.");
assert(say({ status: "skipped", reason: "sweep" }).startsWith("Ended without a disconnect"));
assert(say({ status: "error", error: "sbatch failed" }).includes("sbatch failed"));
assert(say({ status: "queued", job_id: "1", kept_unsaved: ["r1"] }).includes("run r1"));
assert.strictEqual(transferText({ label: "x to HPC3", status: "done", bytes_total: 0 }), "x to HPC3: done, already there");
assert.strictEqual(transferText({ label: "x", status: "cancelled" }), "x: stopped");
''')


def test_the_sessions_list_puts_the_live_one_first():
    _run(r'''
shown = true;
renderComputeSessions([{ id: "old", state: "closed", closed_at: "x", hpc_user: "u", cleanup: { status: "nothing" }, unresolved: [] }]);
renderSessionState({ id: "new", connection_id: "c1", state: "running", hpc_user: "u", opened_at: "y", transfers: [], active: [], unresolved: [] });
const html = $("computeSessionList").innerHTML;
assert(html.indexOf("Running") < html.indexOf("Closed"), html);
assert(html.includes("this session"), html);
''')


def test_the_originals_box_offers_removal_only_after_the_dry_run():
    _run(r'''
const plan = { id: "p1", candidates: [{ path: "/u/a" }], kept: [{ path: "/p/x", reason: "in your personal lab folder" }],
  check: { job_id: "901", status: "queued" }, remove: null, would_remove: 0 };
renderOriginals({ plan });
let html = $("originalsBox").innerHTML;
assert(html.includes("Checking 1 original(s) on HPC3 (job 901, queued). Nothing is removed."), html);
assert(!html.includes('data-orig="remove"'), "no removal before the check finished");
assert(html.includes("1 not considered") && html.includes("in your personal lab folder"), html);
renderOriginals({ plan: { ...plan, check: { job_id: "901", status: "done",
  results: [{ verdict: "would_remove", path: "/u/a", bytes: 6 }, { verdict: "skip", path: "/u/b", reason: "size 5 here, 6 on the server" }] },
  would_remove: 1, would_remove_bytes: 6 } });
html = $("originalsBox").innerHTML;
assert(html.includes('data-orig="remove" data-plan="p1"') && html.includes("Remove 1 original(s), 6 B"), html);
assert(html.includes("kept: size 5 here, 6 on the server"), html);
renderOriginals({ plan: { ...plan, check: { status: "done", results: [] }, remove: { job_id: "902", status: "done",
  results: [{ verdict: "removed", path: "/u/a", bytes: 6 }] }, removed: 1, removed_bytes: 6, would_remove: 1 } });
html = $("originalsBox").innerHTML;
assert(html.includes("Removed 1 original(s), 6 B.") && !html.includes('data-orig="remove"'), html);
''')
