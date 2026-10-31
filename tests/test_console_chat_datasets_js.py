"""Saved dataset multi-selection and account-backed chat attachment state, under node."""
from pathlib import Path
import shutil
import subprocess

import pytest

APP = Path(__file__).resolve().parents[1] / "frontend/console/app.js"


def test_saved_datasets_batch_roles_limit_and_new_sign_in():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    blocks = [
        source[source.index("const chats = new Map();"):source.index("function toast(")],
        source[source.index("const _PRIMARY_SUFFIXES"):source.index("// A dataset row's kind")],
        source[source.index("function hydrateChatDatasets("):source.index("// A deleted dataset")],
        source[source.index("function toggleBound("):source.index("// The composer's upload line")],
        source[source.index("async function onDatasetsClick("):source.index("// Delete the dataset a button")],
        source[source.index("function planDatasetsHtml("):source.index("function planEntryHtml(")],
        source[source.index("async function ensureMessages("):source.index("async function loadSessions(")],
    ]
    harness = r'''
const assert = require("assert");
const state = { activeId: "1", sessions: [{ id: "1", cid: 1 }, { id: "2", cid: 2 }],
  datasets: [{id: 10, path: "/a.h5ad", name: "A", kind: "file"},
             {id: 11, path: "/b.csv", name: "B", kind: "file"},
             {id: 12, path: "/c", name: "C", kind: "file"}],
  selectedDatasetIds: new Set(), maxBoundDatasets: 2 };
const stored = {}, localStorage = { getItem: k => stored[k] || null,
  setItem: (k,v) => stored[k] = v, removeItem: k => delete stored[k] };
const COMPOSER_KEY = "composer", RUNOWNER_KEY = "owners", STREAMS_KEY = "streams";
const els = {}, $ = id => els[id] || (els[id] = {value:"", innerHTML:"", textContent:""});
const escapeHtml = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const datasetIcon = () => "draft", notices = [], toast = s => notices.push(s);
let opened = null, fail = false, server = [], puts = [];
const openChat = sid => opened = sid;
const authed = () => true, msgFromServer = x => x;
const api = async (method, url, body) => {
  if (fail) throw Error("Could not save");
  if (method === "PUT") {
    puts.push(body);
    server = body.datasets.map(d => ({...state.datasets.find(x => x.id === d.id), role:d.role}));
  }
  return { datasets: server, max_bound_datasets: 2, conversation: {title:"Study"}, messages:[] };
};
'''
    checks = r'''
(async () => {
  // Checkbox selection survives filtering: it belongs to the page, not its visible cards.
  const choose = (id, checked) => onDatasetsClick({target:{closest: selector =>
    selector === "[data-select-dataset]" ? {checked, dataset:{selectDataset:id}} : null }});
  await choose("10", true);
  await choose("11", true);
  assert.equal($("datasetsUseSelected").textContent, "Use selected (2)");
  assert.equal($("datasetsLimit").textContent, "Up to 2 datasets per run");
  await useSelectedDatasets();
  assert.equal(puts.length, 1);
  assert.deepStrictEqual(puts[0].datasets, [{id:10, role:null}, {id:11, role:null}]);
  assert.equal(opened, "1");
  assert.equal(state.selectedDatasetIds.size, 0);
  assert.equal(primaryBoundPath(), "/a.h5ad");
  assert($("datasetChips").innerHTML.includes('data-main-path="/b.csv"'));
  await makeMainDataset("/b.csv");
  assert.equal(primaryBoundPath(), "/b.csv");
  assert.equal(server.find(d => d.id === 11).role, "primary");
  await toggleReference("/a.h5ad");
  assert.equal(server.find(d => d.id === 10).role, "reference");
  const count = puts.length;
  await choose("12", true);
  await useSelectedDatasets();
  assert.equal(puts.length, count, "over-limit batch makes no request");
  assert.equal(state.selectedDatasetIds.size, 1, "refusal keeps selection");
  assert(notices.pop().includes("Up to 2"));
  assert.deepStrictEqual(activeChat().bound, ["/a.h5ad", "/b.csv"]);
  // A new sign-in has no in-memory chats; even a stale local cache cannot resurrect data.
  chats.clear();
  stored.composer = JSON.stringify({"1": {draft:"saved words",bound:["/deleted"],roles:{"/deleted":"primary"}}});
  const session = state.sessions[0];
  session.loaded = false;
  await ensureMessages(session);
  assert.deepStrictEqual(activeChat().bound, ["/a.h5ad", "/b.csv"]);
  assert.equal(activeChat().roles["/b.csv"], "primary");
  assert.equal(activeChat().draft, "saved words");
  assert(!stored.composer.includes("bound"));
  server = server.filter(d => d.id !== 10);
  session.loaded = false;
  await ensureMessages(session);
  assert.deepStrictEqual(activeChat().bound, ["/b.csv"]);
  fail = true;
  await toggleBound("/b.csv");
  assert.deepStrictEqual(activeChat().bound, ["/b.csv"], "failed save leaves attachment unchanged");
  assert(notices.pop().includes("Could not save"));
  const html = planDatasetsHtml({datasets:[{name:"A",use:"main"},{name:"B",use:"sample"},
    {name:"C",use:"reference"},{name:"D",use:"extra input"}], unusable_inputs:[{name:"<bad>",reason:"missing & unreadable"}]});
  for (const word of ["main", "sample (QC", "reference", "extra input", "&lt;bad&gt;", "missing &amp; unreadable"]) assert(html.includes(word), html);
})().catch(e => {console.error(e.stack); process.exit(1)});
'''
    result = subprocess.run([node, "-e", harness + "\n".join(blocks) + checks],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
