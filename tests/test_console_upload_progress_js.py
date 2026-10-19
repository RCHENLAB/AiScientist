"""Exercise upload progress and failed-file retry using the actual console functions."""
from pathlib import Path
import shutil
import subprocess

import pytest

APP = Path(__file__).resolve().parents[1] / "frontend/console/app.js"


def test_folder_progress_retry_and_interruption():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    blocks = [
        source[source.index("function uploadFileCounts"):source.index("// Cards are updated")],
        source[source.index("const STORAGE_ID"):source.index("// -- after a page reload")],
    ]
    harness = r'''
const assert = require("assert");
const state = { connectionId: "c", uploads: [], activeId: "chat" };
const store = new Map(), calls = [], aborted = [];
const localStorage = { getItem: k => store.get(k), setItem: (k,v) => store.set(k,v), removeItem: k => store.delete(k) };
const apiKeyOwner = () => "owner";
const hashStr = s => [...s].reduce((n,c) => (n * 31 + c.charCodeAt(0)) | 0, 0);
const upsertUpload = rec => { const i = state.uploads.findIndex(x => x.id === rec.id); if(i < 0) state.uploads.push(rec); else Object.assign(state.uploads[i], rec); };
const setUploadProgress = () => {}, toast = () => {}, reportUploadError = () => {};
const uploadPaces = new Map(), uploadPaceTextFor = () => "", uploadPaceText = () => "";
const uploadSlot = async () => {}, releaseUploadSlot = () => {}, TUS_CONCURRENCY = 3;
const hashUploadFile = async () => "digest";
const fmtUploadSize = n => `${n || 0} bytes`;
const escapeHtml = s => String(s).replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const UPLOAD_ACTIVE = ["queued", "uploading", "staging", "verifying"];
const uploadFailure = e => ({ text: e.message, detail: e.message, lost: true });
let fail = true, registrations = 0, cancel = false, cleanupFails = false, expired = false;
const cleanupRequests = [], reservations = [];
const serverFiles = new Set();
const tusUpload = async (f, fields, ctrl, hash, progress) => {
  calls.push(f.name);
  ctrl.signal.addEventListener("abort", () => aborted.push(f.name));
  progress(f.size / 2);
  if (cancel) { state.uploads[state.uploads.length - 1].ctrl.abort(); throw new DOMException("cancelled", "AbortError"); }
  if (f.name === "bad" && fail) throw new TypeError("offline");
  progress(f.size);
  serverFiles.add(f.name);
  return { status: "complete" };
};
const postJson = async (url, body) => {
 if (url.includes("/cancel")) { cleanupRequests.push(body); return {ok: !cleanupFails}; }
 return { ok: true, json: async () => {
  if (url.includes("reserve-folder")) {
    reservations.push(body);
    if (expired) { serverFiles.clear(); return {folder:"replacement-uuid",have:[]}; }
    return { folder: "uuid", have: body.files.flatMap(([name],i) => serverFiles.has(name) ? [i] : []) };
  }
  registrations++; return { status: "complete", name: "folder", count: 3, path: "/data/uuid" };
} };
};
const uploadChatField = () => ({ conversation_id: "chat" }), removeUpload = id => { state.uploads = state.uploads.filter(u => u.id !== id); };
const addDataset = () => {}, uploadChat = () => "chat", loadDatasets = () => {}, folderDoneText = () => "done";
const setTimeout = () => {};
(async () => {
 const files = [{name:"good",size:90},{name:"bad",size:10},{name:"last",size:100}].map(f => ({...f,webkitRelativePath:`folder/${f.name}`,lastModified:1}));
 await uploadFolder(files);
 const card = state.uploads[0];
 assert.equal(card.status, "error");
 assert.deepEqual(uploadFileCounts(card), {done:2,remaining:0,failed:1,cancelled:0});
 assert.equal(card.sent,195);
 assert.equal(registrations,0);
 assert.deepEqual(aborted, [], "network failure must retain other files and their partials");
 assert.equal(calls.length,3, "every independent file must be attempted");
 assert(uploadCardView(card).stat.includes("1 failed"));
 assert(uploadCardView(card).buttons.some(b => b[1] === "Retry failed files"));
 assert(uploadFileRows(card).includes("195") === false);
 assert(uploadFileRows(card).includes("50%"));
 assert(uploadFileRows({fileProgress:[{name:"<img>",size:0,sent:0,status:"done"}]}).includes("&lt;img&gt;"));
 fail = false;
 await uploadFolder(files);
 assert.deepEqual(calls.sort(),["bad","bad","good","last"].sort());
 assert.equal(state.uploads[0].status,"done");
 assert.equal(state.uploads[0].sent,200);
 assert.equal(registrations,1);
 assert.equal(reservations[1].folder,"uuid", "resume must validate its saved UUID");
 expired = true;
 const beforeExpired = calls.length;
 await uploadFolder(files);
 assert.equal(reservations[2].folder,"uuid");
 assert.equal(state.uploads[0].reservedFolder,"replacement-uuid");
 assert.equal(calls.length-beforeExpired,3,"expired server data must invalidate every completed bit");
 assert.equal(state.uploads[0].filesDone,3);
 expired = false;
 cancel = true;
 const cancelFiles = [{ name:"cancel", size:50, lastModified:1, webkitRelativePath:"cancel/cancel" }];
 await uploadFolder(cancelFiles);
 assert.equal(state.uploads[1].status,"cancelled");
 assert.equal(state.uploads[1].fileProgress[0].status,"cancelled");
 assert.equal(cleanupRequests[0].folder,"uuid");
 assert.equal(cleanupRequests[0].discard_upload,true);
 assert.equal(store.has(state.uploads[1].folderKey),false);
 cleanupFails = true;
 await uploadFolder([{...cancelFiles[0], webkitRelativePath:"failure/cancel"}]);
 assert.equal(state.uploads[2].status,"error");
 assert(state.uploads[2].error.includes("Cancellation cleanup failed"));
 assert.equal(store.has(state.uploads[2].folderKey),true);
})().catch(e => { console.error(e); process.exitCode=1; });
'''
    result = subprocess.run([node, "-"], input="\n".join(blocks) + "\n" + harness,
                            text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_folder_files_copy_to_hpc3_shows_on_its_row():
    """A folder file handed to the HPC3 copy reads "Copying to HPC3" until the server records the
    folder; a file whose copy failed says so on its own row; the counts during the copy are the
    server's (the review of RCHENLAB/AiScientist PR 12)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    blocks = [
        source[source.index("function uploadFileCounts"):source.index("function uploadCardView")],
        source[source.index("const COPY_FAILED"):source.index("// The server records a folder once")],
    ]
    harness = r'''
const assert = require("assert");
const fmtUploadSize = n => `${n || 0} bytes`;
const escapeHtml = s => String(s).replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const rows = [{name: "study/a.csv", size: 3, sent: 3, status: "copying"},
              {name: "study/x/b.csv", size: 2, sent: 2, status: "copying"}];
const state = { uploads: [{ id: "F1", status: "staging", fileProgress: rows, filesTotal: 2, filesDone: 1, filesFailed: 0 }] };
''' + "\n".join(blocks) + r'''
assert(uploadFileRows(state.uploads[0]).includes("Copying to HPC3"));
assert.deepEqual(uploadFileCounts(state.uploads[0]), {done: 1, failed: 0, cancelled: 0, remaining: 1});
markFolderCopies("F1", { failed_files: ["x/b.csv"], recorded: false });
assert.equal(rows[1].status, "error");
assert.equal(rows[0].status, "copying");
state.uploads[0].status = "error";
assert.deepEqual(uploadFileCounts(state.uploads[0]), {done: 0, failed: 1, cancelled: 0, remaining: 1});
markFolderCopies("F1", { failed_files: [], recorded: false });      // "Retry copy" is running it again
assert.equal(rows[1].status, "copying");
markFolderCopies("F1", { failed_files: [], recorded: true });
assert.deepEqual(rows.map(r => r.status), ["done", "done"]);
'''
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_upload_kept_on_the_server_never_says_hpc3():
    """Issue 6: with a data root the queue only checks an upload on the server (job.dest "server"),
    so its rows, card and failure say so, not "Copying to HPC3"."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    blocks = [
        source[source.index("function uploadFileCounts"):source.index("function uploadCardView")],
        source[source.index("const COPY_FAILED"):source.index("// The server records a folder once")],
    ]
    harness = r'''
const assert = require("assert");
const fmtUploadSize = n => `${n || 0} bytes`;
const escapeHtml = s => String(s).replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const rows = [{name: "study/a.csv", size: 3, sent: 3, status: "copying"}];
const state = { uploads: [{ id: "F1", status: "staging", dest: "server", fileProgress: rows, filesTotal: 1, filesDone: 0 }] };
''' + "\n".join(blocks) + r'''
const html = uploadFileRows(state.uploads[0]);
assert(html.includes("Checking") && !html.includes("HPC3"), html);
markFolderCopies("F1", { dest: "server", failed_files: ["a.csv"], recorded: false });
assert.equal(rows[0].error, "the check on the server failed");
markFolderCopies("F1", { dest: "server", failed_files: [], recorded: false });   // retried
assert.equal(rows[0].status, "copying");
assert.equal(stagingWords({ dest: "hpc3" }).copying, "Copying to HPC3");
assert.equal(stagingWords({}).failed, "the copy to HPC3 failed");               // jobs from before issue 6
'''
    result = subprocess.run([node, "-"], input=harness, text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
