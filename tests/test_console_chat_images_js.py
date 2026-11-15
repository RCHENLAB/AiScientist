"""Images in the composer, in the sent message and in a run's reply (RCHENLAB/AiScientist issue 115), under node:
attach, paste and drop with thumbnails that can be removed, the limits, the notice for a model that cannot
read images, a message's thumbnails (escaped, opening full size) and a run's figures inline.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_console_data_menu_js import _function

APP = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"


def _run_verbose(body: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP.read_text(encoding="utf-8")
    consts = source[source.index("const FIGURES_INLINE"):source.index("function figuresHtml(")]
    block = (consts + _function(source, "figuresHtml") + _function(source, "userImagesHtml")
             + 'const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];\n'
               "const IMAGE_MAX_BYTES = 10 * 1024 * 1024;\nconst IMAGE_MAX_COUNT = 5;\nconst visionCache = { id: null, value: null };\n"
             + _function(source, "sessionVision") + _function(source, "renderImageChips") + _function(source, "paintImageNote")
             + _function(source, "addComposerImages") + _function(source, "clearComposerImages")
             + _function(source, "imageFilesOf") + _function(source, "wireComposerImages") + _function(source, "onDataDrop"))
    run = subprocess.run([node, "-e", _HARNESS + block + "\n(async () => {\n" + body + '\nconsole.log("ok");\n})().catch((e) => { console.error(e); process.exit(1); });'],
                         capture_output=True, text=True, timeout=60)
    return run.stdout.strip() if run.returncode == 0 else run.stderr[-1500:]


_HARNESS = r'''
const assert = require("assert");
const escapeHtml = (v) => String(v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
const fmtSize = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n + " B");
const els = {};
const mk = (id) => els[id] ||= { id, innerHTML: "", value: "", hidden: false, listeners: {}, className: "", textContent: "",
  classList: { toggle(c, on) { els[id].hidden = c === "hidden" ? !!on : els[id].hidden; }, add() {}, remove() {} },
  addEventListener(t, f) { this.listeners[t] = f; }, click() { this.clicked = true; } };
const $ = (id) => mk(id);
const state = { connectionId: "conn1" };
const chat = { sid: "7", images: [] };
const activeChat = () => chat, activeSession = () => ({ cid: 9 }), authed = () => true, chatVisible = () => true;
let seq = 0; const rid = () => "k" + (++seq);
const toasts = []; const toast = (t) => toasts.push(t);
const isUnreachable = () => false;
const revoked = []; globalThis.URL = { createObjectURL: (f) => "blob:" + f.name, revokeObjectURL: (u) => revoked.push(u) };
let uploads = [], vision = { accepts_images: true, model: "gpt-4o" }, failUpload = null;
globalThis.fetch = async (url, opt) => {
  uploads.push({ url, opt });
  if (failUpload) return { ok: false, status: failUpload, json: async () => ({ detail: "Only PNG, JPEG, WebP and GIF images can be attached." }) };
  const name = decodeURIComponent(/name=([^&]*)/.exec(url)[1]);
  const id = "i" + uploads.length;
  return { ok: true, status: 200, json: async () => ({ image: { id, name, mime: opt.headers["Content-Type"], size: opt.body.size, url: `/api/chat-images/9/${id}.png` } }) };
};
const api = async () => vision;
const file = (name, type, size = 100) => ({ name, type, size });
let dropped = { files: [], folders: [] }; const droppedUploads = async () => dropped;
const uploadedDatasets = [], uploadedFolders = [];
const uploadDataset = (f) => uploadedDatasets.push(f.name), uploadFolder = (fl) => uploadedFolders.push(fl.length);
'''


def test_attach_shows_a_thumbnail_uploads_without_hpc3_and_can_be_removed():
    assert _run_verbose(r'''
await addComposerImages([file("error.png", "image/png"), file("fig.jpg", "image/jpeg")]);
assert.equal(uploads.length, 2);
assert.ok(uploads[0].url.startsWith("/api/conversations/9/images?name=error.png"));       // no HPC3 connection involved
assert.equal(uploads[0].opt.headers["Content-Type"], "image/png");
assert.deepEqual(chat.images.map((i) => i.id), ["i1", "i2"]);
const html = els.imageChips.innerHTML;
assert.ok(html.includes('class="image-chip"') && (html.match(/<img /g) || []).length === 2);
assert.ok(html.includes('data-remove-image="') && html.includes("blob:error.png"));
assert.equal(els.imageChips.hidden, false);
// removing one before sending drops it and frees its thumbnail
wireComposerImages();
const key = chat.images[0].key;
els.imageChips.listeners.click({ target: { closest: (sel) => sel === "[data-remove-image]" ? { dataset: { removeImage: key } } : null } });
assert.deepEqual(chat.images.map((i) => i.id), ["i2"]);
assert.ok(revoked.includes("blob:error.png"));
clearComposerImages(chat);
assert.deepEqual(chat.images, []) ; assert.equal(els.imageChips.hidden, true);
''') == "ok"


def test_paste_takes_a_screenshot_but_leaves_text_pasting_alone():
    assert _run_verbose(r'''
wireComposerImages();
let prevented = 0;
const paste = (files) => els.chatInput.listeners.paste({ clipboardData: { files }, preventDefault: () => prevented++ });
paste([]);                                            // text: not intercepted
assert.equal(prevented, 0); assert.equal(uploads.length, 0);
paste([file("", "image/png")]);                       // a screenshot has no name
await new Promise((r) => setTimeout(r, 10));
assert.equal(prevented, 1);
assert.equal(chat.images.length, 1);
assert.equal(chat.images[0].name, "pasted image");
paste([file("notes.pdf", "application/pdf")]);        // not an image: left to the browser
assert.equal(prevented, 1);
''') == "ok"


def test_the_picture_button_and_the_file_chooser_attach_images():
    assert _run_verbose(r'''
wireComposerImages();
els.imageBtn.listeners.click();
assert.equal(els.imageFile.clicked, true);
els.imageFile.files = [file("a.webp", "image/webp"), file("b.csv", "text/csv"), file("c.gif", "image/gif")];
els.imageFile.listeners.change();
await new Promise((r) => setTimeout(r, 10));
assert.deepEqual(chat.images.map((i) => i.name), ["a.webp", "c.gif"]);
assert.equal(els.imageFile.value, "");
''') == "ok"


def test_the_limits_five_images_ten_megabytes_and_only_four_types():
    assert _run_verbose(r'''
await addComposerImages([file("big.png", "image/png", 11 * 1048576)]);
assert.equal(chat.images.length, 0); assert.ok(toasts.at(-1).includes("big.png") && toasts.at(-1).includes("limit"));
await addComposerImages([file("v.svg", "image/svg+xml"), file("p.pdf", "application/pdf"), file("t.tiff", "image/tiff")]);
assert.equal(chat.images.length, 0); assert.equal(uploads.length, 0);
assert.ok(toasts.slice(-3).every((t) => t.includes("not a PNG, JPEG, WebP or GIF")));
await addComposerImages(Array.from({ length: 7 }, (_, i) => file(`s${i}.png`, "image/png")));
assert.equal(chat.images.length, 5); assert.equal(uploads.length, 5);
assert.ok(toasts.at(-1).includes("At most 5 images per message"));
''') == "ok"


def test_an_upload_the_server_refuses_leaves_no_thumbnail_and_says_why():
    assert _run_verbose(r'''
failUpload = 400;
await addComposerImages([file("fake.png", "image/png")]);
assert.equal(chat.images.length, 0);
assert.ok(toasts.at(-1).includes("Image not attached") && toasts.at(-1).includes("PNG, JPEG, WebP and GIF"));
assert.ok(revoked.includes("blob:fake.png"));
''') == "ok"


def test_the_composer_says_when_the_model_cannot_read_images():
    assert _run_verbose(r'''
await addComposerImages([file("a.png", "image/png")]);
await paintImageNote();
assert.ok(els.imageNote.textContent.includes("Sent to the model") && !els.imageNote.className.includes("bad"));
visionCache.value = null;
vision = { accepts_images: false, model: "deepseek-v4.1-flash" };
await paintImageNote();
assert.ok(els.imageNote.className.includes("bad"));
assert.ok(els.imageNote.textContent.includes("deepseek-v4.1-flash") && els.imageNote.textContent.includes("cannot read images") && els.imageNote.textContent.includes("will not be sent"));
''') == "ok"


def test_a_sent_message_shows_its_thumbnails_escaped_and_opening_full_size():
    assert _run_verbose(r'''
assert.equal(userImagesHtml([]), ""); assert.equal(userImagesHtml(undefined), "");
const html = userImagesHtml([{ id: "i1", name: 'a"><script>alert(1)</script>.png', url: "/api/chat-images/9/i1.png" }]);
assert.ok(html.includes('class="msg-images"') && html.includes('src="/api/chat-images/9/i1.png"'));
assert.ok(html.includes('data-fv-open="/api/chat-images/9/i1.png"'));               // a click opens it full size
assert.ok(!/<script/i.test(html) && html.includes("&lt;script&gt;"));
''') == "ok"


def test_a_runs_figures_appear_in_its_reply_a_few_at_a_time_with_the_rest_one_click_away():
    assert _run_verbose(r'''
const item = (path, kind = "image") => ({ name: path.split("/").pop(), kind, url: "/api/file/alice/run1/" + path });
assert.equal(figuresHtml([item("report/report.pdf", "pdf"), item("tables/de.csv", "table")]), "");
const items = [item("data/preview.png"), item("figures/umap.png"), item("figures/dotplot.png"), item("figures/volcano.png"),
               item("figures/heatmap.png"), item("figures/qc.png"), item("tables/de.csv", "table")];
const html = figuresHtml(items);
assert.equal((html.match(/<figure /g) || []).length, 4);                           // a few thumbnails
assert.ok(!html.includes("data/preview.png"));                                     // figures/ first, data/ not shown
assert.ok(html.includes('data-fv-open="/api/file/alice/run1/figures/umap.png"') && html.includes("<figcaption>umap.png</figcaption>"));
assert.ok(html.includes("data-open-results") && html.includes("2 more figures in Results"));
assert.ok(!figuresHtml(items.slice(1, 3)).includes("data-open-results"));          // nothing left over: no link
// only the run's own files: an image URL of another kind never becomes a thumbnail
const hostile = figuresHtml([{ name: 'x"><script>', kind: "image", url: "https://evil.example/p.png" },
                             { name: 'y"><img src=x onerror=alert(1)>', kind: "image", url: "/api/file/a/r/figures/y.png" }]);
assert.ok(!hostile.includes("evil.example") && !/<img src=x/i.test(hostile) && !/<script/i.test(hostile));
''') == "ok"


def test_dropping_pictures_on_the_chat_attaches_images_and_other_files_still_upload_as_data():
    assert _run_verbose(r'''
// With no HPC3 connection a dropped picture still attaches; the data file waits for a connection.
state.connectionId = null;
dropped = { files: [file("plot.png", "image/png"), file("cells.h5ad", "application/octet-stream")], folders: [] };
await onDataDrop({});
await new Promise((r) => setTimeout(r, 10));
assert.deepEqual(chat.images.map((i) => i.name), ["plot.png"]);
assert.deepEqual(uploadedDatasets, []);
assert.ok(toasts.at(-1).includes("Connect to HPC3 first"));
// Connected: the picture is an image, the rest is an upload, a folder stays a folder upload.
state.connectionId = "conn1"; chat.images = [];
dropped = { files: [file("a.jpg", "image/jpeg"), file("b.csv", "text/csv")], folders: [[file("x", "text/plain")]] };
await onDataDrop({});
await new Promise((r) => setTimeout(r, 10));
assert.deepEqual(chat.images.map((i) => i.name), ["a.jpg"]);
assert.deepEqual(uploadedDatasets, ["b.csv"]); assert.deepEqual(uploadedFolders, [1]);
// Only pictures dropped: nothing else is asked for, and no "nothing to upload" complaint.
chat.images = []; uploadedDatasets.length = 0; toasts.length = 0;
dropped = { files: [file("only.png", "image/png")], folders: [] };
await onDataDrop({});
await new Promise((r) => setTimeout(r, 10));
assert.deepEqual(chat.images.map((i) => i.name), ["only.png"]); assert.deepEqual(toasts, []);
''') == "ok"
