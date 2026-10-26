"""The console's header names the session's real model, the Model row is gone, and My account → Storage
draws the server and HPC3 area by area (RCHENLAB/AiScientist issue 32), using the actual console
functions under node."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

CONSOLE = Path(__file__).resolve().parents[1] / "frontend/console"
APP = CONSOLE / "app.js"


def _run(harness: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    header = source[source.index("function sessionModelLine(summary)"):source.index("function applyStatus(summary)")]
    block = source[source.index("// ---- My account → Storage"):source.index("async function stopGpu()")]
    size = source[source.index("function fmtSize(n)"):source.index("// Human label + sort order")]
    day = source[source.index("function fmtDay(iso"):source.index("// The chat header counter")]
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
const state = { connectionId: null, ttlDays: 3 };
const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const toast = () => {}, isUnreachable = () => false, confirm = () => true;
const setTimeout = () => 0, clearTimeout = () => {};
const runTitle = (x) => (x.question || "").slice(0, 90) || `Run ${x.run_id}`;
const keptBesideRun = (r) => !!(r && r.name);
const loadResultHandoffs = async () => ({ run2: { name: "processed.h5ad" } });
let opened = null;
const openAccount = (tab) => { opened = tab; };
const fetched = [];
let replies = {};
const fetch = async (url) => { fetched.push(url); const r = replies[url] || { body: {} }; return { ok: r.ok !== false, json: async () => r.body }; };
let sessionsLoaded = 0, originalsLoaded = 0;
const loadComputeSessions = () => { sessionsLoaded++; };
const loadOriginals = () => { originalsLoaded++; };
const at = (y, m, d) => Math.floor(new Date(y, m - 1, d, 12).getTime() / 1000);   // noon, local time
'''
    script = prelude + size + day + header + block + harness
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr or out.stdout


def test_the_header_names_the_model_and_where_it_runs():
    _run(r'''
const line = (s) => sessionModelLine({ status: "ready", ...s });
assert.strictEqual(line({ llm_endpoint: { kind: "shared", model: "deepseek-ai/DeepSeek-V4-Flash" } }),
                   "DeepSeek-V4-Flash · lab's shared server");
assert.strictEqual(line({ llm_endpoint: { kind: "credential", model: "claude-sonnet-4-5", label: "My key" } }),
                   "claude-sonnet-4-5 · your API key");
assert.strictEqual(line({ selected_model: "/dfs3b/x/qwen38-int4/", llm_endpoint: { kind: "cluster", cluster_label: "Qwen3.8-27B INT4" } }),
                   "Qwen3.8-27B INT4 · your GPU on HPC3");
assert.strictEqual(line({ selected_model: "/dfs3b/x/qwen38-int4/", llm_endpoint: { kind: "cluster", cluster_label: "" } }),
                   "qwen38-int4 · your GPU on HPC3");
assert.strictEqual(line({ llm_endpoint: { kind: "missing", label: "Saved API endpoint (deleted)" } }), "Saved API endpoint (deleted)");
assert.strictEqual(line({ mock: true, selected_model: "m", llm_endpoint: { kind: "cluster" } }), "m · your GPU on HPC3 · mock");
assert.strictEqual(sessionModelLine({ status: "disconnected", model: "" }), "");
assert.strictEqual(sessionModelLine({ status: "error", llm_endpoint: { kind: "shared", model: "x" } }), "");
assert.strictEqual(sessionModelLine({ status: "provisioning", llm_endpoint: { kind: "shared", model: "x" } }), "x · lab's shared server");
''')


SERVER = r'''
const server = { total_bytes: 3 * 2 ** 30, datasets_bytes: 2 * 2 ** 30, outputs_bytes: 2 ** 30, linked: 1, on_hpc3: 2,
  datasets: Array.from({ length: 8 }, (_, i) => ({ id: i + 1, name: `ds${i + 1}`, kind: "h5ad", bytes: (8 - i) * 2 ** 20,
                                                   uploaded_at: "2026-10-18T10:00:00", path: `/data/users/u/datasets/${i + 1}` })),
  outputs: [{ run_id: "run1", question: "Cluster the retina", status: "done", created_at: "2026-10-19T09:00:00", bytes: 2 ** 30, running: false },
            { run_id: "run2", question: "QC only", status: "done", created_at: "2026-10-20T09:00:00", bytes: 5, running: false },
            { run_id: "run3", question: "Still going", status: "running", created_at: "2026-10-20T10:00:00", bytes: 1, running: true }] };
'''


def test_the_server_part_shows_sizes_a_bar_and_a_delete_per_row():
    _run(SERVER + r'''
renderServerStorage(server, { run2: { name: "processed.h5ad" } });
assert.strictEqual($("storeServerTotal").textContent, "3.00 GB");
let html = $("storeServerBody").innerHTML;
assert(html.includes('style="flex-grow:2147483648"') && html.includes('style="flex-grow:1073741824"'), "the bar's share per kind");
assert(html.includes("Datasets <b>2.00 GB</b>") && html.includes("Run outputs <b>1.00 GB</b>"), html);
assert(html.includes("ds6") && !html.includes("ds7"), "six rows until Show all");
assert(html.includes('data-store-more="datasets">Show all 8<'), html);
assert(html.includes('data-del="1" data-name="ds1" data-kind="h5ad" data-location="server" data-path="/data/users/u/datasets/1"'), html);
assert(html.includes("h5ad · added Oct 18") && html.includes("8.0 MB"), html);
assert(html.includes('data-run-del="run1" data-name="Cluster the retina" data-kept=""'), html);
assert(html.includes('data-run-del="run2" data-name="QC only" data-kept="1"'), "the processed data beside it is named in the ask");
assert(!html.includes('data-run-del="run3"') && html.includes("running"), "a running run cannot be deleted");
assert(html.includes("1 of your datasets only point at a shared folder") && html.includes("2 of your datasets are still kept on HPC3"), html);
_storeShowAll.datasets = true;
renderServerStorage();
html = $("storeServerBody").innerHTML;
assert(html.includes("ds8") && html.includes(">Show fewer<"), html);
''')


def test_the_hpc3_part_goes_area_by_area():
    _run(r'''
const d = { ttl_days: 3, quota: { group: "ruic20_hpc", pool: "dfs3b", used: "574.58 TiB", limit: "700.00 TiB", percent: 82.1 },
  areas: [
    { key: "temp", label: "Temporary run files", note: "Removed when your session ends, or 3 days after they were last used.",
      path: "/r/Temp/u", bytes: 5 * 2 ** 30, items: [{ name: "analysis/run1", bytes: 5 * 2 ** 30, path: "/r/Temp/u/analysis/run1",
      changed: at(2026, 10, 20), clean_after: at(2099, 10, 23) }, { name: "reports/old", bytes: 10, path: "/r/Temp/u/reports/old",
      changed: at(2020, 1, 1), clean_after: at(2020, 1, 4) }] },
    { key: "uploads", label: "Uploads on HPC3", note: "Kept until you delete them.", path: "/r/uploads/u", bytes: 0, items: [] },
    { key: "personal", label: "Your own lab folder", note: "Your personal folder.", path: "/l/u", bytes: 2048,
      items: [{ name: "old.h5ad", bytes: 2048, path: "/l/u/old.h5ad", changed: at(2026, 1, 1) }] }] };
state.connectionId = "c1";
renderStorage(d);
const html = $("storeHpcBody").innerHTML;
assert.strictEqual($("storeHpcTotal").textContent, "5.00 GB");
assert(html.includes("Lab quota on dfs3b, shared by everyone in ruic20_hpc") && html.includes("574.58 TiB of 700.00 TiB · 82.1%"), html);
assert(html.includes('style="width:82.1%"'), html);
assert(html.includes("last used Oct 20 · removed after Oct 23"), html);
assert(html.includes("removed at the next cleanup"), "a folder past its day");
assert(html.includes('title="/r/Temp/u/analysis/run1"') && html.includes('data-path="/r/Temp/u/analysis/run1"'), html);
assert(!html.includes("Uploads on HPC3"), "an empty older area is not shown");
assert(/<details class="store-group store-fold"><summary class="store-group-head"><strong>Your own lab folder<\/strong>/.test(html), "the lab folder is folded");
assert(!html.slice(html.indexOf("old.h5ad") - 200).includes("removed after"), "the lab folder is never swept");
''')


def test_without_an_hpc3_session_the_server_part_still_loads():
    _run(r'''
(async () => {
  $("view-account").classList.add("active"); $("accountPane-storage").classList.remove("hidden");
  replies["/api/storage/server"] = { body: { total_bytes: 0, datasets: [], outputs: [], datasets_bytes: 0, outputs_bytes: 0 } };
  await loadStorageTab();
  await new Promise((r) => setImmediate(r));
  assert.deepStrictEqual(fetched, ["/api/storage/server"], "no HPC3 listing without a session");
  assert.strictEqual(sessionsLoaded, 1);
  assert.strictEqual(originalsLoaded, 0);
  assert($("storeHpcBody").innerHTML.includes("Connect to HPC3 on the Research page") &&
         $("storeHpcBody").innerHTML.includes("or 3 days after they were last used"), $("storeHpcBody").innerHTML);
  assert($("storeOriginals").classList.contains("hidden"));
  assert($("storeServerBody").innerHTML.includes("No datasets yet.") && $("storeServerBody").innerHTML.includes("No run has written anything yet."));
  openStorageSessions();
  assert.strictEqual(opened, "storage");
  assert.strictEqual($("storeSessions").open, true, "the session chip opens the sessions");
})().catch((e) => { console.error(e); process.exit(1); });
''')


def test_the_page_has_no_fixed_model_no_model_row_and_no_storage_dialog():
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    js = APP.read_text(encoding="utf-8")
    assert "<title>AiScientist Console</title>" in html
    assert "Qwen3" not in re.sub(r'placeholder="[^"]*"', "", html), "only the cluster form's example label"
    for gone in ('id="modelSelect"', 'id="pullModelBtn"', 'id="storageBtn"', 'id="storageModal"', "Manage HPC3 storage"):
        assert gone not in html, gone
    assert 'data-account-tab="storage"' in html and 'id="accountPane-storage"' in html
    for gone in ("Pull a model tag from Ollama", '"/api/model"', "Qwen3 GPU job", 'getElementById("storageModal")'):
        assert gone not in js, gone
