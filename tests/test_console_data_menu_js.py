"""The "＋ Data" button: Files or Folder, and files and folders dropped onto the chat (issue 21).

"＋ Data" used to open a menu of five items plus the account's recent uploads and a search of them.
RCHENLAB/AiScientist issue 21 replaced it with an upload that opens directly: a two-item list, Files
and Folder (a browser's file dialog picks either files or one folder), and several files and folders
dropped onto the chat at once. Reusing an earlier upload is Datasets → Use. This checks the page's
markup, and runs the drop functions of ``frontend/console/app.js`` under node with the browser's
file-system entries faked. Skipped where node is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "frontend" / "console"
APP_JS = ROOT / "app.js"
INDEX = ROOT / "index.html"


def _function(source: str, name: str) -> str:
    """The whole text of ``function name(…) { … }`` (or ``async function``), by matching braces."""
    start = source.index(f"function {name}(")
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    depth, i = 0, source.index("{", source.index(")", start))
    while True:
        ch = source[i]
        depth += ch == "{"
        depth -= ch == "}"
        i += 1
        if depth == 0:
            return source[start:i] + "\n"


def test_the_composer_has_only_the_message_box_data_and_send():
    html = INDEX.read_text(encoding="utf-8")
    composer = html[html.index('<div class="chat-controls">'):html.index("</form>", html.index('id="chatForm"'))]
    # Gone: the engine and mode menus, Pipelines & skills, the three boxes, the server path, the case
    # note, the recent uploads and their search.
    for gone in ("routeSelect", "modeSelect", "methodsBtn", "planMode", "autonomousMode", "saveResults",
                 "serverDataItem", "serverPathRow", "caseNote", "dataMenuRecent", "dataMenuSearch"):
        assert gone not in html, gone
    assert re.findall(r'data-act="(\w+)"', composer) == ["file", "folder"]
    assert 'id="datasetFile"' in composer and "multiple" in composer.split('id="datasetFile"')[1].split(">")[0]
    assert "webkitdirectory" in composer
    assert {"chatInput", "chatSend", "dataMenuBtn", "datasetChips"} <= set(re.findall(r'id="(\w+)"', composer))
    assert 'id="dropHint"' in html


HARNESS = r"""
const els = {};
const $ = (id) => els[id] || (els[id] = { id, classes: new Set(id === "dataMenu" ? ["hidden"] : []),
  get classList() { const c = this.classes; return { add: (k) => c.add(k), remove: (k) => c.delete(k),
    contains: (k) => c.has(k), toggle: (k, on) => { if (on === undefined ? !c.has(k) : on) c.add(k); else c.delete(k); } }; } });
const state = { connectionId: "conn-1" };
const toasts = [], uploaded = { files: [], folders: [] };
const toast = (m) => toasts.push(m);
const uploadDataset = (f) => uploaded.files.push(f.name);
const uploadFolder = (fl) => uploaded.folders.push(Array.from(fl).map((f) => f.webkitRelativePath));
// The browser's entries: a file entry hands over a File, a directory reads its children in batches.
class FakeFile { constructor(name) { this.name = name; this.size = 1; } get webkitRelativePath() { return ""; } }
const fileEntry = (path) => ({ isFile: true, isDirectory: false, name: path.split("/").pop(), fullPath: "/" + path,
  file(ok) { ok(new FakeFile(path.split("/").pop())); } });
const dirEntry = (path, children) => ({ isFile: false, isDirectory: true, name: path.split("/").pop(), fullPath: "/" + path,
  createReader() { let i = 0; return { readEntries(ok) { const b = children.slice(i, i + 2); i += 2; ok(b); } }; } });
const item = (entry) => ({ kind: "file", webkitGetAsEntry: () => entry });
const out = {};
(async () => {
  toggleDataMenu(true);
  out.open = !$("dataMenu").classList.contains("hidden");
  toggleDataMenu(false);
  out.closed = $("dataMenu").classList.contains("hidden");
  // Two loose files and two folders, one of them nested, dropped at once.
  const s1 = dirEntry("Sample1_WT", [fileEntry("Sample1_WT/matrix.mtx.gz"), fileEntry("Sample1_WT/barcodes.tsv.gz"),
    dirEntry("Sample1_WT/outs", [fileEntry("Sample1_WT/outs/features.tsv.gz"), fileEntry("Sample1_WT/outs/.DS_Store")])]);
  const s2 = dirEntry("Sample2_KO", [fileEntry("Sample2_KO/matrix.mtx.gz")]);
  const dt = { items: [item(fileEntry("panel.bed")), item(s1), item(fileEntry("cohort.vcf.gz")), item(s2), { kind: "string" }],
               files: [] };
  await onDataDrop(dropEntries(dt));
  out.uploaded = JSON.parse(JSON.stringify(uploaded));
  // A browser without the entry API: the dropped files only.
  uploaded.files = []; uploaded.folders = [];
  await onDataDrop(dropEntries({ items: [{ kind: "file" }], files: [new FakeFile("pbmc.h5ad")] }));
  out.plain = { ...uploaded };
  // Nothing usable, and no session.
  await onDataDrop(dropEntries({ items: [], files: [] }));
  state.connectionId = null;
  await onDataDrop(dropEntries(dt));
  out.toasts = toasts;
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def dropped():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = "".join(_function(source, n) for n in ("toggleDataMenu", "dropEntries", "entryFile", "readDirAll",
                                                  "folderFiles", "droppedUploads", "onDataDrop"))
    done = subprocess.run([node, "-e", code + HARNESS], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_data_opens_a_list_of_files_and_folder(dropped):
    assert dropped["open"] and dropped["closed"]


def test_several_files_and_folders_dropped_at_once_each_go_up(dropped):
    assert dropped["uploaded"]["files"] == ["panel.bed", "cohort.vcf.gz"]
    # Each folder is one folder upload, with every file's path under the dropped folder (the shape the
    # folder dialog gives), nested folders walked, the Finder's .DS_Store left out.
    assert dropped["uploaded"]["folders"] == [
        ["Sample1_WT/matrix.mtx.gz", "Sample1_WT/barcodes.tsv.gz", "Sample1_WT/outs/features.tsv.gz"],
        ["Sample2_KO/matrix.mtx.gz"]]


def test_without_the_entry_api_the_dropped_files_still_go_up(dropped):
    assert dropped["plain"] == {"files": ["pbmc.h5ad"], "folders": []}


def test_an_empty_drop_or_no_session_says_so(dropped):
    assert dropped["toasts"] == ["Nothing to upload in what was dropped.", "Connect to HPC3 first."]
