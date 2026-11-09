"""Files → Shared and attaching a shared dataset to a chat (issue 100), under node."""
from pathlib import Path
import shutil
import subprocess

import pytest

APP = Path(__file__).resolve().parents[1] / "frontend/console/app.js"
HTML = Path(__file__).resolve().parents[1] / "frontend/console/index.html"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_shared_tab_sits_between_datasets_and_outputs():
    html = HTML.read_text(encoding="utf-8")
    assert html.index('data-files-tab="datasets"') < html.index('data-files-tab="shared"') < html.index('data-files-tab="outputs"')
    assert "Datasets the lab maintains for every account. They are read in place, never count toward your storage, and are never removed." in html
    source = APP.read_text(encoding="utf-8")
    assert 'tab === "outputs" || tab === "shared"' in source


def test_shared_cards_chips_and_attaching():
    node = _node()
    source = APP.read_text(encoding="utf-8")
    block = source[source.index("// ---- Shared datasets: attaching"):source.index("async function onSharedClick(")]
    harness = r'''
const assert = require("assert");
const escapeHtml = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const fmtSize = n => n + "B", fmtDate = d => d, icon = n => "", datasetIcon = () => "folder";
const notices = [], toast = s => notices.push(s);
const state = { activeId: "1", maxBoundDatasets: 3, sessions: [{ id: "1", cid: 7 }] };
const c = { sid: "1", bound: ["/a", "/b"], roles: {}, shared: [], datasetsLoaded: true };
const activeChat = () => c, chatOf = () => c, chatVisible = () => true;
let chips = 0; const renderDatasetChips = () => chips++;
const hydrateChatDatasets = () => {};
const datasetLimitAllows = (paths, ch) => {
  if (paths.length + ((ch && ch.shared) || []).length > state.maxBoundDatasets) { toast("Up to " + state.maxBoundDatasets + " datasets per run."); return false; }
  return true;
};
const puts = []; let refuse = null;
const api = async (method, url, body) => {
  if (refuse) throw Error(refuse);
  puts.push({ method, url, body });
  return { shared_datasets: body.shared_datasets.map(a => ({ shared: a.shared, name: "HECA", requested: a.version,
    version: "v2026.11", files: a.files, role: a.role, n_files: 13, withdrawn: false })), max_bound_datasets: 3 };
};
const $ = id => ({});
'''
    checks = r'''
(async () => {
  assert.equal(sharedChipLabel({ shared: "heca", name: "HECA", requested: "latest", version: "v2026.11", files: null }), "HECA · latest (v2026.11)");
  assert.equal(sharedChipLabel({ shared: "heca", name: "HECA", requested: "v2026.10", files: ["Iris_snRNA.h5ad"] }), "HECA · Iris_snRNA.h5ad · v2026.10");
  assert.ok(sharedChipLabel({ name: "HECA", requested: "v1", withdrawn: true }).endsWith("withdrawn"));
  // Whole dataset, latest: a reference goes to the server, never a path.
  assert.equal(await attachSharedDataset("heca", "latest", null), true);
  assert.deepEqual(puts[0].body, { shared_datasets: [{ shared: "heca", version: "latest", files: null, role: "reference" }] });
  assert.equal(puts[0].url, "/api/conversations/7/shared-datasets");
  assert.ok(!JSON.stringify(puts[0]).includes("path"));
  assert.equal(c.shared.length, 1);
  assert.ok(sharedChipsHtml(c.shared).includes("HECA · latest (v2026.11)"));
  assert.ok(sharedChipsHtml(c.shared).includes('data-clear-shared="heca"'));
  // One file, pinned: replaces the entry for the same dataset.
  await attachSharedDataset("heca", "v2026.11", ["Iris_snRNA_allcells.h5ad"]);
  assert.deepEqual(puts[1].body.shared_datasets, [{ shared: "heca", version: "v2026.11", files: ["Iris_snRNA_allcells.h5ad"], role: "reference" }]);
  // A whole version is one attachment: 2 own + 1 shared = 3 is allowed, a 4th is not.
  await attachSharedDataset("other", "latest", null);
  assert.equal(puts.length, 2);
  assert.ok(notices.some(n => n.includes("Up to 3 datasets")));
  // Remove.
  await detachSharedDataset("heca");
  assert.deepEqual(puts[2].body.shared_datasets, []);
  assert.deepEqual(c.shared, []);
  // A refusal (withdrawn version, say) leaves the attachments as they were.
  await attachSharedDataset("heca", "latest", null);
  refuse = "heca has no version 'v1' (it was withdrawn).";
  assert.equal(await attachSharedDataset("heca", "v1", null), false);
  assert.equal(c.shared.length, 1);
  assert.ok(notices.some(n => n.includes("withdrawn")));
  console.log("ok");
})().catch(e => { console.error(e); process.exit(1); });
'''
    out = subprocess.run([node, "-e", harness + block + checks], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_shared_files_grid_by_tissue_and_modality_with_size_and_preview():
    source = APP.read_text(encoding="utf-8")
    block = source[source.index("const sharedUi ="):source.index("function sharedPanelHtml(")]
    harness = r'''
const assert = require("assert");
const escapeHtml = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const fmtSize = n => n + "B";
'''
    checks = r'''
const ds = { slug: "heca" };
const f = (name, tissue, modality, size) => ({ name, size_bytes: size, facets: { tissue, modality } });
const view = { version: { label: "v2026.11", files: [
  f("Iris_snRNA_allcells.h5ad", "iris", "snRNA", 11), f("Iris_scRNA_allcells.h5ad", "iris", "scRNA", 12),
  f("ON_snRNA_allcells.h5ad", "on", "snRNA", 13), { name: "README.txt", size_bytes: 5, facets: {} }] } };
const html = sharedFilesHtml(ds, view);
assert.ok(html.includes("<table"));
assert.ok(/<th>scRNA<\/th>/.test(html) && /<th>snRNA<\/th>/.test(html));
assert.equal((html.match(/data-shared-preview=/g) || []).length, 4);       // every file has a Preview
assert.ok(html.includes("11B") && html.includes("13B"));
assert.ok(html.includes("README.txt"));                                       // files without facets are listed
assert.ok(html.includes("—"));                                               // ON has snRNA only
assert.ok(!/(\/data\/|\/store)/.test(html));                                 // no server path
console.log("ok");
'''
    out = subprocess.run([_node(), "-e", harness + block + checks], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_the_at_list_gets_a_shared_group_with_the_whole_dataset_and_its_files():
    source = APP.read_text(encoding="utf-8")
    start = source.index("async function sharedMentionItems(")
    block = source[start:source.index("\n}\n", start) + 3]
    harness = r'''
const assert = require("assert");
const api = async (m, url) => url === "/api/shared-datasets"
  ? { datasets: [{ slug: "heca", name: "HECA", latest: "v2026.11" }, { slug: "empty", name: "Empty", latest: null }] }
  : { version: { files: [{ name: "Iris_snRNA.h5ad", size_bytes: 9 }, { name: "Lens_scRNA.h5ad", size_bytes: 8 }] } };
'''
    checks = r'''
(async () => {
  const items = await sharedMentionItems();
  assert.deepEqual(items.map(i => i.label), ["HECA", "HECA · Iris_snRNA.h5ad", "HECA · Lens_scRNA.h5ad"]);
  assert.ok(items.every(i => i.shared && i.slug === "heca" && !("path" in i)));
  assert.equal(items[0].file, undefined);
  assert.equal(items[1].file, "Iris_snRNA.h5ad");
  console.log("ok");
})();
'''
    out = subprocess.run([_node(), "-e", harness + block + checks], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_publish_and_withdraw_are_offered_to_maintainers_only_and_run_as_a_job():
    source = APP.read_text(encoding="utf-8")
    block = source[source.index("const sharedUi ="):source.index("async function onSharedClick(")]
    harness = r'''
const assert = require("assert");
const escapeHtml = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const fmtSize = n => n + "B", fmtDate = d => d, icon = n => "[" + n + "]";
const notices = [], toast = s => notices.push(s);
const activeChat = () => ({ shared: [] });
const calls = []; let polls = 0, confirmed = true;
const api = async (method, url, body) => {
  calls.push({ method, url, body });
  if (method === "POST" && url.endsWith("/publish")) return { job: "j1", state: "running", label: body.label, lines: [] };
  if (url.includes("/publish-jobs/")) { polls++; return polls < 2 ? { state: "running", label: "v2", lines: ["a.h5ad: 1.00 GB copied"] } : { state: "done", label: "v2", lines: ["Published."] }; }
  if (url.endsWith("/withdraw")) return { status: "withdrawn" };
  if (url === "/api/shared-datasets") return { enabled: true, datasets: [], source_roots: [] };
  return {};
};
globalThis.window = { confirm: () => confirmed };
globalThis.setTimeout = (f) => f();
globalThis.document = { querySelector: () => null };
globalThis.CSS = { escape: s => s };
var $ = id => ({ innerHTML: "" });
'''
    checks = r'''
(async () => {
  const mk = (can) => ({ slug: "heca", name: "HECA", maintainers: ["jin"], latest: "v2026.11", can_publish: can,
    versions: [{ label: "v2026.11", status: "published" }] });
  const els = {}; globalThis.$ = id => els[id] || (els[id] = { innerHTML: "" });
  sharedUi.datasets = [mk(false)]; renderShared();
  assert.ok(!els.sharedList.innerHTML.includes("Publish new version") && !els.sharedList.innerHTML.includes("data-shared-withdraw"));
  sharedUi.datasets = [mk(true)]; sharedUi.roots = ["/data/Users/shared"]; sharedUi.open.heca = "publish"; renderShared();
  const html = els.sharedList.innerHTML;
  assert.ok(html.includes("Publish new version") && html.includes("data-shared-withdraw"));
  assert.ok(html.includes("data-pub-source") && html.includes("data-pub-changelog") && html.includes("/data/Users/shared"));
  // Publish: a POST, then polled until done, then the list is reloaded.
  const val = { "[data-pub-source]": " /data/Users/shared/jin/HECA ", "[data-pub-label]": "v2", "[data-pub-changelog]": "More cells" };
  const card = { querySelector: sel => ({ value: val[sel] }) };
  const ds = sharedUi.datasets[0];
  await publishShared(ds, card);
  assert.deepEqual(calls[0].body, { source: "/data/Users/shared/jin/HECA", label: "v2", changelog: "More cells" });
  assert.equal(calls[0].url, "/api/shared-datasets/heca/publish");
  assert.equal(sharedUi.jobs.heca.state, "done", JSON.stringify(sharedUi.jobs.heca));
  assert.ok(notices.some(n => n.includes("published")));
  assert.ok(calls.some(c => c.url === "/api/shared-datasets" && c.method === "GET"));      // reloaded
  assert.ok(sharedJobHtml({ state: "error", error: "no", label: "v2" }).includes("no"));
  // Withdraw asks first, then posts for the selected version (latest = its label).
  confirmed = false; await withdrawShared(ds);
  assert.ok(!calls.some(c => c.url.endsWith("/withdraw")));
  confirmed = true; await withdrawShared(ds);
  assert.ok(calls.some(c => c.url === "/api/shared-datasets/heca/versions/v2026.11/withdraw" && c.method === "POST"));
  console.log("ok");
})().catch(e => { console.error(e); process.exit(1); });
'''
    out = subprocess.run([_node(), "-e", harness + block + checks], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
