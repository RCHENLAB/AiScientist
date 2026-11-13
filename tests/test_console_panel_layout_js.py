"""Resizable console panels and plan review lifecycle, run against app.js under node."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "console" / "app.js"
BLOCKS = [
    ("// ---- collapsible panels", "// ---- auth + operon-style shell"),
    ("function selectSideTab(name)", "// Saved chat text is readable too"),
    ("function wirePlanEntry(el, c)", "// A plan this chat answered"),
    ("function finishPlanApproval(c)", "// ---- uploads"),
    ("function renderPlanPanel()", '// The note under "Change it in words"'),
]

HARNESS = r"""
const vm = require('vm');
const src = require('fs').readFileSync(0, 'utf8');
const store = new Map(), els = {}, requests = [], errors = [], created = [], revisions = [];
const make = (id) => {
  const classes = new Set(), attrs = {}, events = {}, queries = {};
  return { id, style: { setProperty(k,v) { this[k] = v; }, removeProperty(k) { delete this[k]; } },
    dataset: {}, children: [], tabIndex: 0, value: '', hidden: false, disabled: false, isConnected: true, clientWidth: 1400,
    classList: { contains: k => classes.has(k), add: (...ks) => ks.forEach(k => classes.add(k)),
      remove: (...ks) => ks.forEach(k => classes.delete(k)),
      toggle(k,v) { const add = v === undefined ? !classes.has(k) : v; if (add) classes.add(k); else classes.delete(k); return add; } },
    setAttribute(k,v) { attrs[k] = String(v); }, getAttribute(k) { return attrs[k] ?? null; },
    removeAttribute(k) { delete attrs[k]; }, addEventListener(k, fn) { events[k] = fn; },
    removeEventListener(k) { delete events[k]; }, events,
    focus() { document.activeElement = this; }, contains(e) { return e === this || (this.focusables || []).includes(e); },
    querySelectorAll() { return this.focusables || this.children.flatMap(c => c.querySelectorAll()); },
    querySelector(sel) { return queries[sel] || (queries[sel] = make(sel)); },
    appendChild(child) { this.children.push(child); child.parentElement = this; },
    before(child) { document.body.appendChild(child); }, replaceWith(child) {}, remove() {},
    replaceChildren(...children) { this.children = children; },
    getBoundingClientRect() { return { width: this.clientWidth, height: 700, left: 0, right: this.clientWidth }; },
    getClientRects() { return [{}]; }, setPointerCapture() {}, releasePointerCapture() {},
  };
};
const $ = id => els[id] || (els[id] = make(id));
const document = { activeElement: $('toggleRight'), body: make('body'), documentElement: make('html'),
  createComment: () => make('comment'), createElement(tag) { const el = make(tag); created.push(el); return el; },
  addEventListener() {}, removeEventListener() {}, querySelectorAll() { return []; } };
const A = { sid: 'A', plan: { agenda: ['QC'], kind: 'plan', run: 'r1' }, planPending: true };
const B = { sid: 'B', plan: { agenda: ['Cluster'], kind: 'plan', run: 'r2' }, planPending: true };
const state = { activeId: 'A', connectionId: 'conn', sideTab: 'results', planReviewOwner: null };
const sandbox = { $, state, document, UI_KEY: 'ui', console,
  localStorage: { getItem: k => store.get(k) || null, setItem: (k,v) => store.set(k,String(v)) },
  window: { innerWidth: 1400, addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: false }) },
  getComputedStyle: () => ({ paddingLeft: '0px', paddingRight: '0px', columnGap: '0px', gap: '0px', display: 'block', visibility: 'visible' }),
  requestAnimationFrame: fn => fn(), matchMedia: () => ({ matches: false }),
  activeChat: () => state.activeId === 'A' ? A : B, chatVisible: c => !!c && c.sid === state.activeId,
  chatPlanView: c => c && c.plan ? { agenda: c.plan.agenda, mode: 'review' } : null,
  revisePlanInWords: (c, text) => revisions.push({ sid: c.sid, text }),
  planCardHtml: () => '', wirePlanSteps() {}, refreshPlanRevise() {}, wirePlanCardEdits() {}, wirePlanEditor() {},
  fetch: async (url, opts) => { requests.push(JSON.parse(opts.body)); return { ok: true, json: async () => ({}) }; },
  planSavedMessage: p => ({ role: 'assistant', plan: p }), dropPlanDraft() {}, savePlanDraft() {},
  finalizePlanCard() {}, releasePlanPrompt(c) { c.plan = null; c.planPending = false; },
  mountStream() {}, repaintWorking() {}, updateComposerButton() {}, pushToSession() {}, renderChat() {},
  DEFAULT_PLACEHOLDER: 'Ask', toast: s => errors.push(s), isUnreachable: () => false,
  holdPlanPrompt() {}, setPlanStatus() {}, planNotDelivered() {},
};
const api = vm.runInNewContext(src + `\n;({ panelDefaultWidth, panelWidth, clampPanelWidth, resizePanel,
 resetPanelWidth, applyUiPrefs, setPanel, wirePanelResize, openPlanPanel, finishPlanApproval,
 enterPanelFullscreen, exitPanelFullscreen, panelFullscreenKeydown, wirePlanEntry, renderPlanPanel, submitPlan,
 fullscreen: () => panelFullscreen })`, sandbox);
const prefs = () => JSON.parse(store.get('ui') || '{}');
const out = {};
(async () => {
__SCENARIO__
console.log(JSON.stringify(out));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
"""


def _run(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    code = "\n".join(source[source.index(start):source.index(end)] for start, end in BLOCKS)
    done = subprocess.run([node, "-e", HARNESS.replace("__SCENARIO__", scenario)], input=code,
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_widths_clamp_save_restore_and_reopen_at_saved_width():
    out = _run("""
api.resizePanel('left', 100); api.resizePanel('right', 100);
out.min = [api.panelWidth('left'), api.panelWidth('right')];
api.resizePanel('left', 900); out.maxLeft = api.panelWidth('left');
api.resizePanel('left', 360); api.resizePanel('right', 480);
out.saved = prefs();
api.setPanel('right', false); api.setPanel('right', true);
out.reopened = api.panelWidth('right');
api.resizePanel('left', 300, false); api.resizePanel('right', 350, false);
api.applyUiPrefs(); out.restored = [api.panelWidth('left'), api.panelWidth('right')];
""")
    assert out["min"] == [260, 320]
    assert out["maxLeft"] <= 660  # 1400 - 320 right - 420 chat
    assert out["saved"]["leftWidth"] == 360 and out["saved"]["rightWidth"] == 480
    assert out["reopened"] == 480
    assert out["restored"] == [360, 480]


def test_resize_handle_double_click_restores_tab_default_and_keyboard_resizes():
    out = _run("""
api.wirePanelResize(); api.resizePanel('left', 400); api.resizePanel('right', 500);
$('panelResizeLeft').events.dblclick({ preventDefault() {} });
$('panelResizeRight').events.dblclick({ preventDefault() {} });
out.reset = [api.panelWidth('left'), api.panelWidth('right')];
state.sideTab = 'plan'; $('layout').classList.add('side-plan');
api.resizePanel('right', 500); $('panelResizeRight').events.dblclick({ preventDefault() {} });
out.planDefault = api.panelWidth('right');
$('panelResizeLeft').events.keydown({ key: 'ArrowRight', preventDefault() {} });
out.keyboard = api.panelWidth('left'); out.aria = $('panelResizeLeft').getAttribute('aria-valuenow');
""")
    assert out["reset"] == [340, 400]
    assert out["planDefault"] == 460
    assert out["keyboard"] > 340
    assert float(out["aria"]) == out["keyboard"]


def test_pointer_drag_resizes_each_edge_and_stops_on_pointer_release():
    out = _run("""
api.wirePanelResize(); api.resizePanel('left', 340); api.resizePanel('right', 400);
const left = $('panelResizeLeft'), right = $('panelResizeRight');
left.events.pointerdown({ button: 0, clientX: 340, pointerId: 1, preventDefault() {} });
left.events.pointermove({ clientX: 390, pointerId: 1 });
left.events.pointerup({ pointerId: 1 });
left.events.pointermove({ clientX: 500, pointerId: 1 });
right.events.pointerdown({ button: 0, clientX: 1000, pointerId: 2, preventDefault() {} });
right.events.pointermove({ clientX: 940, pointerId: 2 });
right.events.pointercancel({ pointerId: 2 });
out.widths = [api.panelWidth('left'), api.panelWidth('right')]; out.saved = prefs();
out.dragging = $('layout').classList.contains('resizing-panels');
""")
    assert out["widths"] == [390, 460]
    assert out["saved"]["leftWidth"] == 390 and out["saved"]["rightWidth"] == 460
    assert not out["dragging"]


@pytest.mark.parametrize("path", ["entry", "panel", "fullscreen"])
def test_approval_closes_only_the_panel_opened_for_this_plan(path):
    out = _run("""
api.setPanel('right', false); api.openPlanPanel();
const entry = make('entry'); const buttons = {};
entry.querySelector = sel => buttons[sel] || (buttons[sel] = make(sel));
api.wirePlanEntry(entry, A);
if (__PATH__ === 'fullscreen') buttons['[data-plan="fullscreen"]'].events.click();
if (__PATH__ === 'entry') await buttons['[data-plan="approve"]'].events.click();
else { api.renderPlanPanel(); await $('planPanel').querySelector('[data-plan="approve"]').events.click(); }
out.closed = $('layout').classList.contains('collapse-right');
out.request = requests[0]; out.fullscreen = !!api.fullscreen();
out.other = { pending: B.planPending, plan: !!B.plan };
A.plan = { agenda: ['QC'] }; A.planPending = true;
api.setPanel('right', true); api.openPlanPanel(); await api.submitPlan('approve', undefined, undefined, A);
out.manuallyOpen = !$('layout').classList.contains('collapse-right');
out.errors = errors;
""".replace("__PATH__", json.dumps(path)))
    assert out["closed"] and not out["fullscreen"]
    assert out["request"]["action"] == "approve" and out["request"]["conversation_id"] == "A"
    assert out["other"] == {"pending": True, "plan": True}
    assert out["manuallyOpen"]
    assert not any("failed" in error.lower() for error in out["errors"])


def test_cancel_and_background_approval_do_not_close_another_chats_panel():
    out = _run("""
api.setPanel('right', false); api.openPlanPanel();
await api.submitPlan('cancel', undefined, undefined, A);
out.cancelOpen = !$('layout').classList.contains('collapse-right');
state.activeId = 'B'; api.openPlanPanel();
A.plan = { agenda: ['QC'] }; A.planPending = true;
await api.submitPlan('approve', undefined, undefined, A);
out.backgroundOpen = !$('layout').classList.contains('collapse-right');
out.other = { pending: B.planPending, plan: !!B.plan };
""")
    assert out["cancelOpen"] and out["backgroundOpen"]
    assert out["other"] == {"pending": True, "plan": True}


def test_manual_toggle_takes_ownership_of_a_panel_opened_for_plan_review():
    out = _run("""
api.setPanel('right', false); api.openPlanPanel();
api.setPanel('right', false); api.setPanel('right', true);
await api.submitPlan('approve', undefined, undefined, A);
out.open = !$('layout').classList.contains('collapse-right');
""")
    assert out["open"]


def test_fullscreen_traps_focus_and_escape_exits_and_restores_focus():
    out = _run("""
const panel = $('rightPanel'), first = make('first'), last = make('last');
panel.focusables = [first, last];
const original = document.activeElement;
api.enterPanelFullscreen(panel); out.entered = !!api.fullscreen();
document.activeElement = last;
let prevented = false;
api.panelFullscreenKeydown({ key: 'Tab', shiftKey: false, preventDefault() { prevented = true; } });
out.forward = document.activeElement.id; out.prevented = prevented;
document.activeElement = first;
api.panelFullscreenKeydown({ key: 'Tab', shiftKey: true, preventDefault() {} });
out.backward = document.activeElement.id;
api.panelFullscreenKeydown({ key: 'Escape', preventDefault() {}, stopImmediatePropagation() {} });
out.exited = !api.fullscreen(); out.restored = document.activeElement === original;
""")
    assert out["entered"] and out["prevented"]
    assert out["forward"] == "first" and out["backward"] == "last"
    assert out["exited"] and out["restored"]


def test_fullscreen_preview_remains_interactive_and_focus_has_a_fallback():
    out = _run("""
const preview = $('previewModal'), background = $('app');
preview.inert = false; background.inert = false;
document.body.appendChild(background); document.body.appendChild(preview);
const original = document.activeElement;
api.enterPanelFullscreen(preview);
out.previewInert = preview.inert; out.backgroundInert = background.inert;
original.isConnected = false;
api.exitPanelFullscreen();
out.backgroundRestored = !background.inert;
out.fallback = document.activeElement === $('toggleRight');
""")
    assert not out["previewInert"] and out["backgroundInert"]
    assert out["backgroundRestored"] and out["fallback"]


def test_fullscreen_revision_words_survive_redraw_and_submit_for_the_owner():
    out = _run("""
api.enterPanelFullscreen($('rightPanel'));
let form = created.filter(el => el.id === 'form').at(-1);
let input = form.querySelector('textarea');
input.value = 'Use a resolution of 0.8'; input.events.input();
api.renderPlanPanel();
form = created.filter(el => el.id === 'form').at(-1); input = form.querySelector('textarea');
out.restored = input.value;
form.events.submit({ preventDefault() {} }); out.revisions = revisions;
""")
    assert out["restored"] == "Use a resolution of 0.8"
    assert out["revisions"] == [{"sid": "A", "text": "Use a resolution of 0.8"}]
