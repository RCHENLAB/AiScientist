"""Dataset @ mentions exercise the real composer functions under node (issue 73)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_console_data_menu_js import _function


ROOT = Path(__file__).resolve().parents[1] / "frontend" / "console"

HARNESS = r"""
const els = {};
const $ = (id) => els[id] ||= { value: '', selectionStart: 0, selectionEnd: 0,
  innerHTML: '', attrs: {}, hidden: true,
  classList: { add() {}, remove() {} },
  setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
  focus() {}, scrollIntoView() {} };
const c = { bound: [], roles: {}, draft: '' };
const state = { activeId: 'A', maxBoundDatasets: 10, datasets: [], boundPaths: c.bound, boundRoles: c.roles,
  sessions: [{id: 'A', title: 'Study A'}, {id: 'B', title: 'Study B'}] };
const chatOf = () => c, activeChat = () => c, chatVisible = () => true;
let saves = 0, syncs = 0, requests = 0, pending = null;
const saveComposer = () => saves++, syncDatasetSelection = () => syncs++;
const updateComposerButton = () => {};
const toast = () => {};
const escapeHtml = (s) => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const primaryBoundPath = () => c.bound[0], datasetIcon = () => 'file';
const fmtSize = (s) => `${s} B`, fmtDate = (s) => s || '—';
const isFolderKind = (k) => k.includes('folder');
const rows = [
  {id: 1, name: 'Atlas', path: '/secret/older', kind: 'file', size_bytes: 10, uploaded_at: '2026-01-01', conversation_id: 'A'},
  {id: 2, name: 'Atlas', path: '/secret/newer', kind: 'folder', size_bytes: 20, uploaded_at: '2026-02-01', conversation_id: 'B'},
  {id: 3, name: 'Counts & cells', path: '/secret/counts', kind: 'file', size_bytes: 30, uploaded_at: '2026-01-15', conversation_id: 'A'}
];
const fetch = async (url) => { requests++; if (url !== '/api/datasets') throw Error(url);
  if (pending) return await pending;
  return {ok: true, json: async () => ({datasets: rows.slice()})}; };
function positionDatasetMentions() {} // Layout is supplied by the browser; behavior runs here.
const input = $('chatInput');
const type = async (text, caret = text.length) => { input.value = text; input.setSelectionRange(caret, caret); await updateDatasetMentions(); };
const key = (key) => { const e = {key, prevented: false, preventDefault() {this.prevented = true;}};
  const handled = datasetMentionKeydown(e); return {handled, prevented: e.prevented}; };
(async () => {
  const out = {};
  await type('person@example.org');
  out.email = {requests, open: input.attrs['aria-expanded']};
  await type('@');
  out.open = {requests, html: $('datasetMentions').innerHTML, expanded: input.attrs['aria-expanded'],
    active: input.attrs['aria-activedescendant'], order: datasetMention.rows.map(d => d.id)};
  await type('@aTl');
  out.filter = datasetMention.rows.map(d => d.id);
  out.down = key('ArrowDown'); out.activeAfterDown = input.attrs['aria-activedescendant'];
  out.up = key('ArrowUp'); out.activeAfterUp = input.attrs['aria-activedescendant'];
  key('ArrowDown'); out.enter = key('Enter');
  out.selected = {text: input.value, bound: [...c.bound], draft: c.draft, expanded: input.attrs['aria-expanded'], caret: input.selectionStart};
  await type(input.value + ' and @CoUnTs');
  out.tab = key('Tab');
  out.multiple = {text: input.value, bound: [...c.bound]};
  // Run the actual sendChat attachment serialization below.
  SEND_DATASETS
  out.payload = datasets;
  await type('');
  out.afterDeletingText = [...c.bound];
  await type('Compare @Atlas');
  selectDatasetMention(0); // The same function the delegated click handler calls.
  out.click = {text: input.value, bound: [...c.bound]};
  await type(''); await type('@missing');
  out.noMatch = {html: $('datasetMentions').innerHTML, active: input.attrs['aria-activedescendant'] || null};
  out.space = key(' '); await type('@missing ');
  out.afterSpace = {text: input.value, open: input.attrs['aria-expanded']};
  await type(''); await type('@');
  out.escape = key('Escape'); await type('@Atlas');
  out.afterEscape = {text: input.value, open: input.attrs['aria-expanded']};
  await type(''); await type('@Counts ');
  out.nameWithSpace = datasetMention.rows.map(d => d.id);
  await type(''); await type('Use\n@Atlas');
  out.afterWhitespace = datasetMention.rows.map(d => d.id);
  await type(''); await type('x@Atlas'); out.insideWord = input.attrs['aria-expanded'];
  state.datasets[0].gist = 'Short description';
  renderDatasetChips(); out.chips = $('datasetChips').innerHTML;
  // A response arriving after dismissal cannot reopen the picker.
  await type('');
  let finish;
  pending = new Promise(resolve => { finish = resolve; });
  input.value = '@'; input.setSelectionRange(1, 1);
  const loading = updateDatasetMentions(); key('Escape');
  finish({ok: true, json: async () => ({datasets: rows})}); await loading;
  out.stale = input.attrs['aria-expanded'];
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def mentions():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = (ROOT / "app.js").read_text(encoding="utf-8")
    start = source.index("const datasetMention =")
    code = source[start:source.index("\n", start)] + "\n"
    code += "".join(_function(source, name) for name in (
        "datasetMentionQuery", "closeDatasetMentions", "renderDatasetMentions", "updateDatasetMentions",
        "selectDatasetMention", "datasetMentionKeydown", "addDataset", "chatLabel", "renderDatasetChips"))
    # Files → Use may share the multi-attachment helper with issue 72.
    if "function addDatasets(" in source:
        code += _function(source, "addDatasets") + _function(source, "canAttachDatasets")
    start = source.index("const datasets = c.bound.map")
    payload = source[start:source.index("\n  });", start) + len("\n  });")]
    done = subprocess.run([node, "-e", code + HARNESS.replace("SEND_DATASETS", payload)],
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_opens_and_filters_display_names_case_insensitively(mentions):
    assert mentions["open"]["order"] == [2, 3, 1]
    assert mentions["open"]["expanded"] == "true"
    assert mentions["filter"] == [2, 1]
    assert mentions["afterWhitespace"] == [2, 1]
    assert mentions["nameWithSpace"] == [3]


def test_email_or_at_inside_a_word_does_not_open(mentions):
    assert mentions["email"] == {"requests": 0, "open": "false"}
    assert mentions["insideWord"] == "false"


def test_same_names_have_kind_size_date_and_source_chat_without_paths(mentions):
    html = mentions["open"]["html"]
    for detail in ("folder", "file", "20 B", "10 B", "2026-02-01", "2026-01-01", "Study A", "Study B"):
        assert detail in html
    assert html.count("<strong>Atlas</strong>") == 2
    assert "/secret/" not in html
    assert "Counts &amp; cells" in html


def test_keyboard_selection_attaches_via_use_path_and_preserves_draft(mentions):
    assert mentions["activeAfterDown"] == "datasetMention-1"
    assert mentions["activeAfterUp"] == "datasetMention-0"
    assert mentions["enter"] == {"handled": True, "prevented": True}
    assert mentions["selected"] == {"text": "@Atlas", "bound": ["/secret/older"], "draft": "@Atlas",
                                     "expanded": "false", "caret": 6}
    assert mentions["click"] == {"text": "Compare @Atlas", "bound": ["/secret/older", "/secret/counts", "/secret/newer"]}


def test_multiple_refs_and_deleting_text_keeps_attachments_and_payload_names(mentions):
    assert mentions["tab"] == {"handled": True, "prevented": True}
    assert mentions["multiple"] == {"text": "@Atlas and @Counts & cells", "bound": ["/secret/older", "/secret/counts"]}
    assert mentions["afterDeletingText"] == ["/secret/older", "/secret/counts"]
    assert mentions["payload"] == [{"path": "/secret/older", "name": "Atlas", "role": None},
                                    {"path": "/secret/counts", "name": "Counts & cells", "role": None}]


def test_no_match_escape_and_space_leave_typed_text_and_close(mentions):
    assert "No matching datasets" in mentions["noMatch"]["html"]
    assert "+ Data" in mentions["noMatch"]["html"]
    assert mentions["noMatch"]["active"] is None
    assert mentions["space"] == {"handled": False, "prevented": False}
    assert mentions["afterSpace"] == {"text": "@missing ", "open": "false"}
    assert mentions["afterEscape"] == {"text": "@Atlas", "open": "false"}
    assert mentions["stale"] == "false"


def test_chip_tooltip_uses_name_and_description(mentions):
    assert 'title="Atlas — Short description"' in mentions["chips"]
    assert 'title="/secret/' not in mentions["chips"]


def test_markup_and_events_connect_accessible_picker_to_composer():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "app.js").read_text(encoding="utf-8")
    assert 'role="combobox"' in html and 'aria-controls="datasetMentions"' in html
    assert 'role="listbox" aria-label="Your datasets"' in html
    assert 'if (datasetMentionKeydown(e)) return;' in source
    assert 'if (row) selectDatasetMention(Number(row.dataset.mentionIndex));' in source
